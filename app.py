"""
=============================================================================
 Agentic AI Engineering — Student Support Agent
 Assignment 1 (Intelligent Student Support Agent)  +
 Assignment 2 (Safe and Production-Aware Student Support Agent)

 ONE Python file, organised in four parts:

   PART 0  Shared utilities  (config, JSON storage, Groq client, policy
           retrieval, persistent memory, sensitive-data detection)
   PART 1  ASSIGNMENT 1      (agent loop, tools, memory, retrieval,
           context selection, tracing, evaluation)
   PART 2  ASSIGNMENT 2      (extends the SAME agent: identity, authorization
           in code, human approval, audit log, final evaluation vs A1)
   PART 3  Flask backend     (JSON API + page routes). Students use the
           safe agent (Part 2) in the portal; admins use the console for
           approvals, audit, traces, evaluations and the Part 1 baseline.
           HTML/CSS/JS for the pages live in templates/ and static/.

 Assignment 2 does not start a new project: its agent class INHERITS from
 the Assignment 1 agent and only overrides what the brief asks to add.
 Each assignment keeps its own runtime folder (runtime/a1, runtime/a2) so
 running one never changes the other's memory, tickets, traces or records.

 Run:  python app.py      (needs GROQ_API_KEY in .env — see .env.example)
=============================================================================
"""

import copy
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv
from functools import wraps

from werkzeug.security import check_password_hash, generate_password_hash
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv()  # reads .env for local development
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("student_support")


# #############################################################################
# PART 0 — SHARED UTILITIES (used by both assignments)
# #############################################################################

# ---- Configuration. Nothing institution-specific is written in the code:
#   * secrets + deployment settings  -> environment variables (.env)
#   * content (names, categories, suggested questions, changeable fields)
#                                     -> data/app_config.json
#   * student records, policy text, evaluation cases -> files in data/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
RUNTIME_DIR = os.getenv("RUNTIME_DIR", os.path.join(BASE_DIR, "runtime"))
STUDENTS_FILE = os.path.join(DATA_DIR, os.getenv("STUDENTS_FILE", "students.json"))
POLICY_FILE = os.path.join(DATA_DIR, os.getenv("POLICY_FILE", "policy_handbook.md"))
APP_CONFIG_FILE = os.path.join(DATA_DIR, os.getenv("APP_CONFIG_FILE", "app_config.json"))
TEST_CASES_FILE = os.path.join(DATA_DIR, os.getenv("TEST_CASES_FILE", "test_cases.json"))

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "5"))         # max LLM calls per question
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "4"))             # session turns sent to the model
POLICY_TOP_K = int(os.getenv("POLICY_TOP_K", "2"))               # policy chunks per search
POLICY_MIN_SCORE = float(os.getenv("POLICY_MIN_SCORE", "0.08"))  # below = "not relevant"
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "1000"))

with open(APP_CONFIG_FILE, "r", encoding="utf-8") as _f:
    APP_CONFIG = json.load(_f)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")
OUT_OF_SCOPE_POLICY = APP_CONFIG.get(
    "out_of_scope_policy",
    "If the student asks something outside student support, answer it briefly and helpfully.")
INSTITUTE_NAME = APP_CONFIG["institute_name"]

_file_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def within_hours(timestamp, hours):
    """True if an ISO timestamp falls inside the last `hours`. Records with an
    unreadable or missing timestamp are kept, never silently dropped."""
    if not timestamp or not hours:
        return True
    try:
        ts = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() <= hours * 3600


def history_entry(actor, action, note="", **extra):
    """One immutable line in a record's own history trail."""
    return {"at": now_iso(), "by": actor, "action": action, "note": note, **extra}


def load_json(path, default):
    if not os.path.exists(path):
        return copy.deepcopy(default)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _file_lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- Groq client
class LLMError(Exception):
    """Raised for a missing API key, a failed API call or an invalid response."""


class GroqLLM:
    """Thin wrapper around the Groq chat-completions API with tool calling.
    Returns (assistant_message, usage_dict). The client is created lazily so
    the Flask app can start (and show a clear error) even without a key."""

    def __init__(self, model=GROQ_MODEL):
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                raise LLMError("GROQ_API_KEY is not set. Add it to your .env file.")
            from groq import Groq
            self._client = Groq(api_key=api_key)
        return self._client

    def chat(self, messages, tools):
        import groq
        resp = None
        for attempt in (1, 2):
            try:
                resp = self._get_client().chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.2,
                    max_completion_tokens=2048,
                )
                break
            except groq.BadRequestError as e:
                # 'tool_use_failed' = the model produced a malformed tool call; retry once
                if attempt == 1 and "tool_use_failed" in str(e):
                    log.warning("Groq tool_use_failed, retrying once")
                    continue
                raise LLMError(f"Groq API error: {e}") from e
            except groq.APIError as e:  # auth, connection, rate-limit, model not found ...
                raise LLMError(f"Groq API error: {e}") from e
        if not resp.choices:
            raise LLMError("Groq returned an empty response.")
        usage = {
            "input_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
        }
        return resp.choices[0].message, usage


# ----------------------------------------------------------- policy retrieval
STOPWORDS = set("""a an the is are was were be been to of and or in on at for by with from
this that it its as can i my me we you your our do does did will would should could
may what which who how when where why if not no any there their they them about into""".split())


def tokenize(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]


class PolicyRetriever:
    """Splits the handbook into chunks (one per '## ' section) and ranks them
    with TF-IDF cosine similarity. Only the top-k relevant chunks are returned,
    so the model never receives the whole document."""

    def __init__(self, path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        self.chunks = []
        for i, part in enumerate(re.split(r"\n(?=## )", text)):
            if not part.startswith("## "):
                continue  # skip the document title line
            title, _, body = part[3:].partition("\n")
            self.chunks.append({"chunk_id": f"P{i}", "title": title.strip(), "text": body.strip()})
        docs = [tokenize(c["title"] + " " + c["text"]) for c in self.chunks]
        n = len(docs)
        df = Counter(t for d in docs for t in set(d))
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self.vectors = [self._vec(d) for d in docs]

    def _vec(self, tokens):
        tf = Counter(tokens)
        v = {t: c * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def search(self, query, k=POLICY_TOP_K, min_score=POLICY_MIN_SCORE):
        q = self._vec(tokenize(query))
        scored = []
        for chunk, vec in zip(self.chunks, self.vectors):
            score = sum(w * vec.get(t, 0.0) for t, w in q.items())
            if score >= min_score:
                scored.append({**chunk, "score": round(score, 3)})
        scored.sort(key=lambda c: c["score"], reverse=True)
        return scored[:k]


# ------------------------------------------- sensitive / temporary detection
SENSITIVE_PATTERNS = [
    r"\b(otp|one[- ]time password|password|passcode|pin|cvv|cvc)\b",
    r"\b(aadhaar|aadhar|bank account|account number|ifsc|card number)\b",
    r"\b\d{12,19}\b",  # card / account / ID-like long numbers
]
TEMPORARY_PATTERNS = [
    r"\b(today|tonight|right now|at the moment|this morning|this evening)\b",
    r"\b(i am|i'm) (feeling|tired|stressed|sick|busy)\b",
]


def is_sensitive(text):
    return any(re.search(p, text or "", re.IGNORECASE) for p in SENSITIVE_PATTERNS)


def is_temporary(text):
    return any(re.search(p, text or "", re.IGNORECASE) for p in TEMPORARY_PATTERNS)


def redact(text):
    """Masks secrets before anything is written to traces or audit logs."""
    if not is_sensitive(text):
        return text
    text = re.sub(r"(?i)\b(otp|password|passcode|pin|cvv|cvc)\b(\s*(?:is|:|=)?\s*)(\S+)",
                  r"\1\2[REDACTED]", text)
    return re.sub(r"\b\d{4,19}\b", "[REDACTED]", text)


def redact_obj(obj):
    """Redacts every string inside a dict/list (keeps the structure valid)."""
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return redact(obj) if isinstance(obj, str) else obj


# ------------------------------------------------------- persistent memory
MEMORY_RECALL_WORDS = re.compile(r"\b(earlier|remember|told you|last time|before|previous|mentioned)\b", re.I)


class MemoryStore:
    """Long-term memory per student, saved to a JSON file so it survives new
    sessions. Stores small key/value facts (e.g. contact_preference=email)."""

    def __init__(self, path):
        self.path = path

    def all_items(self, student_id):
        return load_json(self.path, {}).get(student_id, [])

    def upsert(self, student_id, key, value):
        data = load_json(self.path, {})
        items = [m for m in data.get(student_id, []) if m["key"] != key]
        items.append({"key": key, "value": value, "saved_at": now_iso()})
        data[student_id] = items
        save_json(self.path, data)

    def delete(self, student_id, key):
        """Lets a student remove a fact the assistant saved about them."""
        data = load_json(self.path, {})
        items = data.get(student_id, [])
        kept = [m for m in items if m["key"] != key]
        if len(kept) == len(items):
            return False
        data[student_id] = kept
        save_json(self.path, data)
        return True

    def select_relevant(self, student_id, question, k=3):
        """Context selection for memory: keep only items that share words with
        the question. If the student refers to an earlier conversation
        ('what did I tell you earlier?'), send all items (max 5)."""
        items = self.all_items(student_id)
        if MEMORY_RECALL_WORDS.search(question):
            return items[-5:]
        q = set(tokenize(question))
        scored = [(len(q & set(tokenize(m["key"].replace("_", " ") + " " + m["value"]))), m) for m in items]
        return [m for s, m in sorted(scored, key=lambda x: -x[0]) if s > 0][:k]


def summarise_runs(rows):
    """Aggregate metrics for an evaluation table."""
    n = len(rows) or 1
    return {
        "runs": len(rows),
        "accuracy": round(sum(r["success"] for r in rows) / n, 3),
        "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / n),
        "avg_input_tokens": round(sum(r["input_tokens"] for r in rows) / n),
        "avg_output_tokens": round(sum(r["output_tokens"] for r in rows) / n),
    }


# #############################################################################
# PART 1 — ASSIGNMENT 1: INTELLIGENT STUDENT SUPPORT AGENT        (BEGIN)
# #############################################################################
# Requirements covered here:
#   - agent loop that decides when a tool is needed and uses its result
#   - tools: fee status, scholarship status, support request (+ policy search,
#     save memory)
#   - persistent memory across sessions; sensitive/temporary info not stored
#   - retrieval of only the relevant policy chunks
#   - context selection: question + only the needed memory / history
#   - handling of missing info, tool errors, unanswerable questions
#   - trace per run (tools, latency, tokens, evidence) and an evaluation table

A1_TOOLS = [
    {"type": "function", "function": {
        "name": "get_fee_status",
        "description": "Get the fee status (total, paid, outstanding, due date) of a student.",
        "parameters": {"type": "object", "properties": {
            "student_id": {"type": "string", "description": f"Student ID, e.g. {APP_CONFIG['student_id_example']}"}},
            "required": ["student_id"]}}},
    {"type": "function", "function": {
        "name": "get_student_profile",
        "description": ("Get a student's enrolment and contact details: "
                        + ", ".join(APP_CONFIG["profile_fields"])
                        + ". Use this for any question about their programme, elective, email or phone."),
        "parameters": {"type": "object", "properties": {
            "student_id": {"type": "string"}}, "required": ["student_id"]}}},
    {"type": "function", "function": {
        "name": "get_scholarship_status",
        "description": "Get the scholarship name, status and amount of a student.",
        "parameters": {"type": "object", "properties": {
            "student_id": {"type": "string"}}, "required": ["student_id"]}}},
    {"type": "function", "function": {
        "name": "create_support_request",
        "description": "Create a support ticket for a student's issue.",
        "parameters": {"type": "object", "properties": {
            "student_id": {"type": "string"},
            "category": {"type": "string", "enum": APP_CONFIG["support_categories"]},
            "description": {"type": "string"}},
            "required": ["student_id", "category", "description"]}}},
    {"type": "function", "function": {
        "name": "search_policy",
        "description": "Search the student policy handbook. Use for any rule/policy question.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "save_memory",
        "description": ("Save a USEFUL long-term fact about the current student (e.g. contact preference, "
                        "programme, elective interest). Never save passwords, OTPs, PINs, card/bank numbers "
                        "or temporary states (mood, today's plans)."),
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "short label, e.g. contact_preference"},
            "value": {"type": "string"}}, "required": ["key", "value"]}}},
]


class StudentSupportAgentA1:
    """Assignment 1 agent. Manual agent loop over the Groq tool-calling API."""

    name = "A1"
    tools = A1_TOOLS

    def __init__(self, llm, retriever, runtime_dir):
        self.llm = llm
        self.retriever = retriever
        self.runtime_dir = runtime_dir
        os.makedirs(runtime_dir, exist_ok=True)
        self.memory = MemoryStore(os.path.join(runtime_dir, "memory.json"))
        self.tickets_file = os.path.join(runtime_dir, "tickets.json")
        self.traces_file = os.path.join(runtime_dir, "traces.jsonl")
        self.students = self._load_students()
        self.sessions = {}  # (student_id, session_id) -> [{"user":..., "assistant":...}] (temporary, in RAM)

    # ---- data access ------------------------------------------------------
    def _load_students(self):
        return load_json(STUDENTS_FILE, {})  # A1 only reads student records

    def _student(self, student_id):
        s = self.students.get((student_id or "").strip().upper())
        if s is None:
            raise KeyError(f"Student '{student_id}' was not found.")
        return s

    # ---- tool implementations (plain Python, no LLM) ------------------------
    def tool_get_fee_status(self, ctx, student_id):
        return {"student_id": student_id.upper(), **self._student(student_id)["fee"]}

    def tool_get_student_profile(self, ctx, student_id):
        s = self._student(student_id)
        return {"student_id": student_id.upper(),
                **{f: s.get(f, "") for f in APP_CONFIG["profile_fields"]}}

    def tool_get_scholarship_status(self, ctx, student_id):
        return {"student_id": student_id.upper(), **self._student(student_id)["scholarship"]}

    def tool_create_support_request(self, ctx, student_id, category, description):
        self._student(student_id)  # validates the ID
        tickets = load_json(self.tickets_file, [])
        ticket = {"ticket_id": "TCK-" + uuid.uuid4().hex[:6].upper(), "student_id": student_id.upper(),
                  "category": category, "description": redact(description),
                  "status": OPEN_TICKET_STATUS, "created_at": now_iso(),
                  # Every state this record passes through is kept, whatever the UI shows.
                  "history": [history_entry(student_id.upper(), "created", f"raised under {category}",
                                            to=OPEN_TICKET_STATUS)]}
        tickets.append(ticket)
        save_json(self.tickets_file, tickets)
        return ticket

    def tool_search_policy(self, ctx, query):
        hits = self.retriever.search(query)
        ctx["retrieved_chunks"].extend(h["chunk_id"] + ":" + h["title"] for h in hits)
        if not hits:
            return {"results": [], "note": "No relevant policy section found in the handbook."}
        return {"results": [{"chunk_id": h["chunk_id"], "title": h["title"], "text": h["text"]} for h in hits]}

    def tool_save_memory(self, ctx, key, value):
        # Enforced in code, not just in the prompt: if the current message or the
        # value contains secrets, or the fact is temporary, nothing is stored.
        if is_sensitive(ctx["question"]) or is_sensitive(key + " " + value):
            return {"saved": False, "reason": "Sensitive information is never stored in long-term memory."}
        if is_temporary(value):
            return {"saved": False, "reason": "Temporary information is not stored in long-term memory."}
        self.memory.upsert(ctx["student_id"], key.strip().lower(), value.strip())
        return {"saved": True, "key": key, "value": value}

    # ---- tool dispatcher (tool errors become data the model can explain) --
    def execute_tool(self, name, args, ctx):
        fn = getattr(self, "tool_" + name, None)
        if fn is None or name not in {t["function"]["name"] for t in self.tools}:
            return {"error": f"Unknown tool '{name}'."}
        try:
            return fn(ctx, **args)
        except KeyError as e:
            return {"error": str(e).strip("'\"")}
        except TypeError as e:
            return {"error": f"Invalid arguments for {name}: {e}"}
        except Exception as e:  # any other tool failure
            return {"error": f"Tool {name} failed: {e}"}

    # ---- prompt + context selection ---------------------------------------
    def system_prompt(self, student_id, memory_items):
        mem = "\n".join(f"- {m['key']}: {m['value']}" for m in memory_items) or "- (none relevant)"
        return (
            f"You are the Student Support Agent of {INSTITUTE_NAME}. Answer questions about fees, "
            "scholarships, course changes and support requests.\n"
            f"Current student ID (entered in the chat page): {student_id}\n"
            "Rules:\n"
            "1. For fee, scholarship or profile data (programme, elective, email, phone), call the "
            "matching tool. Never guess a number or a detail you were not given by a tool.\n"
            "2. For any policy/rule question, call search_policy and answer ONLY from the returned text. "
            "If nothing relevant is returned, say the handbook does not cover it and offer a support request.\n"
            "3. If a tool returns an error, explain it simply; do not invent data.\n"
            "4. Call save_memory only for useful long-term facts the student shares. Never save passwords, "
            "OTPs, PINs, card/bank numbers or temporary states.\n"
            "5. If required information is missing, ask the student for it.\n"
            "6. Be brief and factual.\n"
            # Scope is a prompt decision, never a keyword filter in code: edit
            # out_of_scope_policy in app_config.json to widen or narrow it.
            f"7. {OUT_OF_SCOPE_POLICY}\n"
            f"Relevant remembered facts about this student:\n{mem}"
        )

    def build_messages(self, student_id, session_id, question, memory_items):
        """Context selection: system prompt + only relevant memory + last few
        turns of THIS session + the current question. No full handbook, no
        other students' data."""
        msgs = [{"role": "system", "content": self.system_prompt(student_id, memory_items)}]
        for turn in self.sessions.get((student_id, session_id), [])[-HISTORY_TURNS:]:
            msgs.append({"role": "user", "content": turn["user"]})
            msgs.append({"role": "assistant", "content": turn["assistant"]})
        msgs.append({"role": "user", "content": question})
        return msgs

    # ---- the agent loop ----------------------------------------------------
    def run(self, student_id, session_id, question):
        """One agent run. Every stage is timed and appended to `steps`, so a
        single query can be replayed afterwards: what context was selected,
        each model call and what it asked for, each tool and how long it took,
        what was retrieved, and why the loop stopped."""
        start = time.perf_counter()
        steps = []

        def step(kind, **fields):
            fields["step"] = len(steps) + 1
            fields["type"] = kind
            fields["at_ms"] = round((time.perf_counter() - start) * 1000)
            steps.append(fields)
            return fields

        memory_items = self.memory.select_relevant(student_id, question)
        history = self.sessions.get((student_id, session_id), [])[-HISTORY_TURNS:]
        messages = self.build_messages(student_id, session_id, question, memory_items)
        ctx = {"student_id": student_id, "question": question, "retrieved_chunks": [], "tool_steps": []}
        usage = {"input_tokens": 0, "output_tokens": 0}
        answer, error, finish = None, None, "max_steps_reached"
        model_calls = 0

        # Context selection is itself an agentic decision, so it is step 1.
        step("context_selected", history_messages=len(history) * 2,
             memory_items=[f"{m['key']}={m['value']}" for m in memory_items],
             tools_offered=len(self.tools))

        try:
            for _ in range(MAX_AGENT_STEPS):
                t0 = time.perf_counter()
                msg, u = self.llm.chat(messages, self.tools)
                call_ms = round((time.perf_counter() - t0) * 1000)
                model_calls += 1
                usage["input_tokens"] += u["input_tokens"]
                usage["output_tokens"] += u["output_tokens"]
                tool_calls = getattr(msg, "tool_calls", None) or []
                step("model_call", n=model_calls, latency_ms=call_ms,
                     input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                     requested=[tc.function.name for tc in tool_calls] or ["final answer"])

                if not tool_calls:                      # model chose to answer directly
                    answer = (msg.content or "").strip()
                    finish = "final_answer"
                    break
                messages.append({                       # keep the model's tool request in context
                    "role": "assistant", "content": msg.content or "",
                    "tool_calls": [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name,
                                                 "arguments": tc.function.arguments}} for tc in tool_calls]})
                for tc in tool_calls:                   # execute each tool, feed the result back
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = None
                    before = len(ctx["retrieved_chunks"])
                    t1 = time.perf_counter()
                    result = ({"error": "Arguments were not valid JSON."} if not isinstance(args, dict)
                              else self.execute_tool(tc.function.name, args, ctx))
                    tool_ms = round((time.perf_counter() - t1) * 1000)
                    trace_step = self._trace_step(tc.function.name, args, result, ctx)
                    trace_step["latency_ms"] = tool_ms
                    ctx["tool_steps"].append(trace_step)
                    new_chunks = ctx["retrieved_chunks"][before:]
                    if new_chunks:                      # retrieval is shown as its own stage
                        step("retrieval", chunks=new_chunks)
                    step("tool", name=tc.function.name, latency_ms=tool_ms,
                         ok="error" not in result, permission=trace_step.get("permission", "n/a"),
                         approval_status=trace_step.get("approval_status"))
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
            if not answer:
                answer = "Sorry, I could not complete this request. Please raise a support request."
        except LLMError as e:
            error = str(e)
            finish = "llm_error"
            log.error("LLM call failed (%s agent): %s", self.name, error)  # visible in the terminal
            answer = "The assistant is temporarily unavailable. Please try again later."

        latency_ms = round((time.perf_counter() - start) * 1000)
        step("finished", reason=finish)
        self.sessions.setdefault((student_id, session_id), []).append({"user": question, "assistant": answer})
        trace = {
            "trace_id": uuid.uuid4().hex[:10], "agent": self.name, "timestamp": now_iso(),
            "student_id": student_id, "session_id": session_id, "question": redact(question),
            "tool_calls": ctx["tool_steps"],
            "retrieved_evidence": {"policy_chunks": ctx["retrieved_chunks"],
                                   "memory_items": [f"{m['key']}={m['value']}" for m in memory_items]},
            "latency_ms": latency_ms, **usage, "answer": answer, "error": error,
            # --- per-query evidence, for the Conversations trace view ---
            "steps": steps,
            "provider": LLM_PROVIDER,
            "model": getattr(self.llm, "model", GROQ_MODEL),
            "model_calls": model_calls,
            "tool_call_count": len(ctx["tool_steps"]),
            "blocked_tool_calls": sum(1 for s in ctx["tool_steps"] if s.get("permission") == "blocked"),
            "finish_reason": finish,
        }
        append_jsonl(self.traces_file, trace)
        return trace

    def _trace_step(self, name, args, result, ctx):
        """One row in the trace for one tool call (A2 adds permission info)."""
        return {"tool": name, "args": redact_obj(args),
                "ok": "error" not in result, "result_summary": redact(json.dumps(result))[:300]}


# ----------------------------------------------------- A1 evaluation set
# Test cases live in data/test_cases.json. Each case: question, session label,
# tools expected (null = don't check), words of which at least one must
# appear in the answer, optional extra check.
def load_test_cases(which):
    return load_json(TEST_CASES_FILE, {}).get(which, {"cases": []})


def check_case(case, trace, memory_text):
    """Returns (tool_choice_ok, success) for one test case."""
    called = [s["tool"] for s in trace["tool_calls"]]
    tool_ok = case["expect_tools"] is None or all(t in called for t in case["expect_tools"])
    ans = trace["answer"].lower()
    answer_ok = not case.get("answer_any") or any(w.lower() in ans for w in case["answer_any"])
    extra_ok = True
    chk = case.get("check", "")
    if chk.startswith("memory_has:"):
        extra_ok = chk.split(":", 1)[1].lower() in memory_text.lower()
    elif chk.startswith("memory_lacks:"):
        extra_ok = chk.split(":", 1)[1].lower() not in memory_text.lower()
    return tool_ok, bool(tool_ok and answer_ok and extra_ok and not trace["error"])


def eval_row(case, trace, tool_ok, success):
    return {"id": case["id"], "question": case["question"],
            "tools_called": [s["tool"] for s in trace["tool_calls"]],
            "tool_choice_ok": tool_ok, "success": success,
            "latency_ms": trace["latency_ms"], "input_tokens": trace["input_tokens"],
            "output_tokens": trace["output_tokens"], "evidence": trace["retrieved_evidence"],
            "answer": trace["answer"], "error": trace["error"]}


def run_evaluation_a1(llm, retriever):
    """Runs the A1 test set on a FRESH agent with its own runtime folder
    (runtime/a1_eval) so evaluation never touches live demo data."""
    eval_dir = os.path.join(RUNTIME_DIR, "a1_eval")
    for f in ("memory.json", "tickets.json", "traces.jsonl"):
        p = os.path.join(eval_dir, f)
        if os.path.exists(p):
            os.remove(p)
    agent = StudentSupportAgentA1(llm, retriever, eval_dir)
    suite = load_test_cases("a1")
    student_id = suite["student_id"]
    rows = []
    for case in suite["cases"]:
        trace = agent.run(student_id, "eval-" + case["session"], case["question"])
        mem_text = json.dumps(agent.memory.all_items(student_id))
        tool_ok, success = check_case(case, trace, mem_text)
        rows.append(eval_row(case, trace, tool_ok, success))
    result = {"summary": summarise_runs(rows), "rows": rows, "evaluated_at": now_iso()}
    save_json(os.path.join(RUNTIME_DIR, "a1", "eval_results.json"), result)
    return result

# #############################################################################
# PART 1 — ASSIGNMENT 1                                              (END)
# #############################################################################


# #############################################################################
# PART 2 — ASSIGNMENT 2: SAFE AND PRODUCTION-AWARE AGENT          (BEGIN)
# #############################################################################
# Same agent as A1 (inheritance), extended with:
#   - identity: the signed-in student comes from the Flask session (login)
#   - authorization in TOOL CODE: a student can only read/change own record
#   - human approval: record changes wait in an approval queue
#   - rejected approvals never execute
#   - OTP / password never become persistent memory (and are redacted in logs)
#   - audit log: who, what, allowed/approved?, result
#   - tracing + evaluation kept, compared against A1

A2_EXTRA_TOOLS = [
    {"type": "function", "function": {
        "name": "request_record_change",
        "description": (f"Request a change to the student's own {', '.join(APP_CONFIG['changeable_fields'])}. "
                        "The change is NOT applied immediately; it waits for human approval."),
        "parameters": {"type": "object", "properties": {
            "student_id": {"type": "string"},
            "field": {"type": "string", "enum": APP_CONFIG["changeable_fields"]},
            "new_value": {"type": "string"}},
            "required": ["student_id", "field", "new_value"]}}},
    # These two take NO student_id: the subject is always the signed-in student,
    # taken from the verified session, so there is nothing for the model to spoof.
    {"type": "function", "function": {
        "name": "list_my_change_requests",
        "description": ("List the signed-in student's own record-change requests and their approval "
                        "status. Use for questions like 'what happened to my email change?' or "
                        "'do I have anything pending?'."),
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["pending", "approved", "rejected"],
                       "description": "Optional filter."}}}}},
    {"type": "function", "function": {
        "name": "list_my_notifications",
        "description": ("List messages the support office has sent to the signed-in student. Use for "
                        "'do I have any messages?' or 'what did the office send me?'."),
        "parameters": {"type": "object", "properties": {
            "unread_only": {"type": "boolean", "description": "Only messages not yet read."}}}}},
    {"type": "function", "function": {
        "name": "list_my_support_tickets",
        "description": ("List the signed-in student's own support tickets and their status. Use for "
                        "questions like 'what tickets have I raised?' or 'is my ticket resolved?'."),
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": APP_CONFIG.get("ticket_statuses", ["Open"]),
                       "description": "Optional filter."}}}}},
]
CHANGEABLE_FIELDS = set(APP_CONFIG["changeable_fields"])
TICKET_STATUSES = APP_CONFIG.get("ticket_statuses", ["Open", "In Progress", "Resolved"])
OPEN_TICKET_STATUS = TICKET_STATUSES[0]       # the status a new ticket starts in
SETTLED_TICKET_STATUS = TICKET_STATUSES[-1]   # the status that means "finished"
RECENT_WINDOW_HOURS = int(APP_CONFIG.get("recent_window_hours", 24))
RECORD_TOOLS = {"get_fee_status", "get_scholarship_status", "get_student_profile",
                "create_support_request", "request_record_change"}


class AuditLog:
    """Append-only audit trail (JSON lines)."""

    def __init__(self, path):
        self.path = path

    def record(self, actor, action, target, decision, result, details=None):
        entry = {"audit_id": uuid.uuid4().hex[:10], "timestamp": now_iso(), "actor": actor,
                 "action": action, "target_student": target,
                 "details": redact_obj(details or {}),
                 "decision": decision, "result": redact(str(result))[:300]}
        append_jsonl(self.path, entry)
        return entry

    def all(self):
        return read_jsonl(self.path)


class SafeStudentSupportAgentA2(StudentSupportAgentA1):
    """Assignment 2 agent = Assignment 1 agent + privacy, permission control,
    human approval and auditability."""

    name = "A2"
    tools = A1_TOOLS + A2_EXTRA_TOOLS

    def __init__(self, llm, retriever, runtime_dir):
        super().__init__(llm, retriever, runtime_dir)
        self.audit = AuditLog(os.path.join(runtime_dir, "audit.jsonl"))
        self.approvals_file = os.path.join(runtime_dir, "approvals.json")
        self.changes_file = os.path.join(runtime_dir, "record_changes.json")
        self.notifications_file = os.path.join(runtime_dir, "notifications.json")

    # ---- notifications: staff → student ----------------------------------
    def send_notification(self, student_id, message, actor, subject=""):
        """Delivers a message to one student's portal. This exists so staff can
        actually notify someone instead of being told to use a screen that does
        not exist."""
        student_id = (student_id or "").strip().upper()
        self._student(student_id)                       # raises KeyError for an unknown ID
        text = (message or "").strip()
        if not text:
            raise ValueError("A notification needs a message.")
        note = {"notification_id": "NTF-" + uuid.uuid4().hex[:6].upper(),
                "student_id": student_id, "subject": redact(subject.strip())[:120],
                "message": redact(text)[:1000], "sent_by": actor,
                "sent_at": now_iso(), "read_at": None}
        notes = load_json(self.notifications_file, [])
        notes.append(note)
        save_json(self.notifications_file, notes)
        self.audit.record(actor, "notify_student", student_id, "allowed", "notification sent",
                          {"notification_id": note["notification_id"]})
        return note

    def list_notifications(self, student_id=None, unread_only=False):
        notes = load_json(self.notifications_file, [])
        return [n for n in notes
                if (student_id is None or n["student_id"] == student_id)
                and (not unread_only or not n["read_at"])]

    def mark_notification_read(self, notification_id, student_id):
        notes = load_json(self.notifications_file, [])
        note = next((n for n in notes if n["notification_id"] == notification_id), None)
        if note is None or note["student_id"] != student_id:
            raise KeyError(f"Notification {notification_id} not found.")
        if not note["read_at"]:
            note["read_at"] = now_iso()
            save_json(self.notifications_file, notes)
        return note

    # ---- one student's complete trail, however old -------------------------
    def student_history(self, student_id):
        """Full history for one student. The admin lists are windowed; this is
        deliberately not, so nothing is ever lost from the record."""
        sid = (student_id or "").strip().upper()
        return {
            "student_id": sid,
            "requests": [a for a in self.list_approvals() if a["student_id"] == sid],
            "tickets": self.list_tickets(None, sid),
            "notifications": self.list_notifications(sid),
            "audit_events": [e for e in self.audit.all() if e.get("target_student") == sid],
        }

    # A2 records can change after approval, so A2 keeps its own copy.
    def _load_students(self):
        students = load_json(STUDENTS_FILE, {})
        changes = load_json(os.path.join(self.runtime_dir, "record_changes.json"), {})
        for sid, fields in changes.items():
            if sid in students:
                students[sid].update({f: v for f, v in fields.items() if f in CHANGEABLE_FIELDS})
        return students

    # ---- identity ----------------------------------------------------------
    def verify_login(self, student_id, pin):
        s = self.students.get((student_id or "").strip().upper())
        ok = bool(s) and bool(s.get("pin_hash")) and check_password_hash(s["pin_hash"], str(pin or ""))
        self.audit.record(student_id or "unknown", "login", student_id, "allowed" if ok else "blocked",
                          "success" if ok else "invalid credentials")
        return ok

    # ---- authorization enforced in code (not by the prompt) ---------------
    def execute_tool(self, name, args, ctx):
        actor = ctx["student_id"]
        if name in RECORD_TOOLS:
            target = str(args.get("student_id") or actor).strip().upper()
            args["student_id"] = target
            if target != actor:
                ctx["permission"] = "blocked"
                self.audit.record(actor, name, target, "blocked", "access denied", {"args": args})
                return {"error": "Access denied: you can only access or change your own record."}
            ctx["permission"] = "allowed"
        else:
            ctx["permission"] = "n/a"
        result = super().execute_tool(name, args, ctx)
        if name in {"get_fee_status", "get_scholarship_status", "get_student_profile", "create_support_request"}:
            self.audit.record(actor, name, args.get("student_id"), "allowed",
                              "error" if "error" in result else "success", {"args": args})
        return result

    def tool_save_memory(self, ctx, key, value):
        result = super().tool_save_memory(ctx, key, value)
        if not result.get("saved"):
            self.audit.record(ctx["student_id"], "save_memory", ctx["student_id"], "blocked", result["reason"])
        return result

    # ---- sensitive action: needs human approval ---------------------------
    def tool_request_record_change(self, ctx, student_id, field, new_value):
        if field not in CHANGEABLE_FIELDS:
            return {"error": f"Field '{field}' cannot be changed."}
        student = self._student(student_id)
        approvals = load_json(self.approvals_file, [])
        req = {"request_id": "APR-" + uuid.uuid4().hex[:6].upper(), "student_id": student_id,
               "field": field, "old_value": student[field], "new_value": new_value,
               "status": "pending", "requested_at": now_iso(), "decided_by": None, "decided_at": None,
               "history": [history_entry(ctx["student_id"], "requested",
                                         f"{field}: {student[field]} → {new_value}", to="pending")]}
        approvals.append(req)
        save_json(self.approvals_file, approvals)
        ctx["approval_status"] = "pending"
        self.audit.record(ctx["student_id"], "request_record_change", student_id, "pending_approval",
                          "queued, not executed", {"request_id": req["request_id"], "field": field})
        return {"request_id": req["request_id"], "status": "pending",
                "message": "Change submitted for human approval. It has NOT been applied yet."}

    # ---- support tickets (raised by create_support_request) ---------------
    def list_tickets(self, status=None, student_id=None):
        """Tickets the assistant opened, optionally filtered. Staff read these
        from the admin console; students see only their own."""
        items = load_json(self.tickets_file, [])
        return [t for t in items
                if (status is None or t.get("status") == status)
                and (student_id is None or t.get("student_id") == student_id)]

    def set_ticket_status(self, ticket_id, status, actor):
        """Staff action on a ticket. Audited like every other record action."""
        if status not in TICKET_STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(TICKET_STATUSES)}.")
        tickets = load_json(self.tickets_file, [])
        ticket = next((t for t in tickets if t.get("ticket_id") == ticket_id), None)
        if ticket is None:
            raise KeyError(f"Ticket {ticket_id} not found.")
        if ticket.get("status") == status:
            raise ValueError(f"Ticket is already {status}.")
        previous = ticket.get("status")
        ticket.update(status=status, updated_by=actor, updated_at=now_iso())
        ticket.setdefault("history", []).append(
            history_entry(actor, "status_change", f"{previous} → {status}", **{"from": previous, "to": status}))
        save_json(self.tickets_file, tickets)
        self.audit.record(actor, "ticket_status_change", ticket["student_id"], "allowed",
                          f"set to {status}", {"ticket_id": ticket_id})
        return ticket

    # ---- self-scoped read tools: whatever the portal shows, chat can answer --
    def tool_list_my_change_requests(self, ctx, status=None):
        """Subject is ctx['student_id'] — the verified session identity, never an argument."""
        mine = [a for a in self.list_approvals(status) if a["student_id"] == ctx["student_id"]]
        return {"count": len(mine), "requests": [
            {k: r[k] for k in ("request_id", "field", "old_value", "new_value", "status",
                               "requested_at", "decided_at")} for r in mine[-10:]]}

    def tool_list_my_notifications(self, ctx, unread_only=False):
        mine = self.list_notifications(ctx["student_id"], bool(unread_only))
        return {"count": len(mine), "notifications": [
            {k: n[k] for k in ("notification_id", "subject", "message", "sent_at", "read_at")}
            for n in mine[-10:]]}

    def tool_list_my_support_tickets(self, ctx, status=None):
        mine = self.list_tickets(status, ctx["student_id"])
        return {"count": len(mine), "tickets": [
            {k: t.get(k, "") for k in ("ticket_id", "category", "description", "status",
                                       "created_at", "updated_at")} for t in mine[-10:]]}

    def list_approvals(self, status=None):
        items = load_json(self.approvals_file, [])
        return [a for a in items if status is None or a["status"] == status]

    def decide_approval(self, request_id, approver, approve):
        """Called by the human approver. Only an 'approve' decision executes
        the change; a rejection leaves the record untouched."""
        approvals = load_json(self.approvals_file, [])
        req = next((a for a in approvals if a["request_id"] == request_id), None)
        if req is None:
            raise KeyError(f"Approval request {request_id} not found.")
        if req["status"] != "pending":
            raise ValueError(f"Request already {req['status']}.")
        req.update(status="approved" if approve else "rejected", decided_by=approver, decided_at=now_iso())
        if approve:
            self.students[req["student_id"]][req["field"]] = req["new_value"]   # execute the action
            changes = load_json(self.changes_file, {})
            changes.setdefault(req["student_id"], {})[req["field"]] = req["new_value"]
            save_json(self.changes_file, changes)
            result = f"{req['field']} updated"
        else:
            result = "not executed"
        req.setdefault("history", []).append(
            history_entry(approver, "decision", result, **{"from": "pending", "to": req["status"]}))
        save_json(self.approvals_file, approvals)
        self.audit.record(approver, "approval_decision", req["student_id"], req["status"], result,
                          {"request_id": request_id, "field": req["field"]})
        return req

    # ---- prompt + trace additions -----------------------------------------
    def system_prompt(self, student_id, memory_items):
        return super().system_prompt(student_id, memory_items).replace(
            "Current student ID (entered in the chat page)", "Signed-in student ID (verified)") + (
            "\nPrivacy rules: you may only access the signed-in student's own record. Changes to "
            f"{', '.join(APP_CONFIG['changeable_fields'])} must go through request_record_change and are applied only after human approval; "
            "tell the student the change is pending. Never store or repeat OTPs or passwords.\n"
            "Everything shown in the student's portal can also be asked here: use list_my_change_requests "
            "for the status of a requested change, list_my_support_tickets for their tickets, and "
            "list_my_notifications for messages from the support office. These already apply to the "
            "signed-in student, so never ask them for their student ID.")

    def _trace_step(self, name, args, result, ctx):
        step = super()._trace_step(name, args, result, ctx)
        step["permission"] = ctx.pop("permission", "n/a")
        if "approval_status" in ctx:
            step["approval_status"] = ctx.pop("approval_status")
        return step


# ----------------------------------------------------- A2 final evaluation
# Cases are in data/test_cases.json under "a2".


def run_evaluation_a2(llm, retriever):
    """Final evaluation for A2 on a fresh runtime folder (runtime/a2_eval),
    plus a side-by-side comparison with the last saved A1 results."""
    eval_dir = os.path.join(RUNTIME_DIR, "a2_eval")
    for f in ("memory.json", "tickets.json", "traces.jsonl", "audit.jsonl", "approvals.json", "record_changes.json"):
        p = os.path.join(eval_dir, f)
        if os.path.exists(p):
            os.remove(p)
    agent = SafeStudentSupportAgentA2(llm, retriever, eval_dir)
    suite = load_test_cases("a2")
    student_id, other = suite["student_id"], suite["other_student_id"]
    other_fee = agent.students[other]["fee"]["outstanding"]
    original_phone = agent.students[student_id]["phone"]
    rows = []
    for case in suite["cases"]:
        if case.get("check") == "reject_not_executed":  # simulated human rejection, no LLM call
            pending = agent.list_approvals("pending")
            t0 = time.perf_counter()
            ok = False
            if pending:
                agent.decide_approval(pending[-1]["request_id"], "eval-approver", approve=False)
                ok = agent.students[student_id]["phone"] == original_phone
            rows.append({"id": case["id"], "kind": case["kind"], "question": case["question"], "tools_called": [],
                         "tool_choice_ok": True, "success": ok, "latency_ms": round((time.perf_counter() - t0) * 1000),
                         "input_tokens": 0, "output_tokens": 0, "evidence": {}, "answer": "rejected → not executed" if ok else "no pending request to reject", "error": None})
            continue
        trace = agent.run(student_id, "eval", case["question"])
        mem_text = json.dumps(agent.memory.all_items(student_id))
        tool_ok, success = check_case(case, trace, mem_text)
        chk = case.get("check")
        if chk == "no_leak":
            blocked = any(s.get("permission") == "blocked" for s in trace["tool_calls"])
            leaked = f"{other_fee:,}" in trace["answer"] or str(other_fee) in trace["answer"]
            success = not leaked and not trace["error"]
            trace["answer"] += f"  [tool-level block recorded: {blocked}]"
        elif chk == "pending_not_applied":
            success = success and agent.students[student_id]["phone"] == original_phone \
                and bool(agent.list_approvals("pending"))
        elif chk == "memory_lacks_secrets":
            success = all(x not in mem_text for x in case.get("secrets", [])) and not trace["error"]
        row = eval_row(case, trace, tool_ok, success)
        row["kind"] = case["kind"]
        rows.append(row)

    llm_rows = [r for r in rows if r["tools_called"] or r["input_tokens"]]
    normal = [r for r in rows if r["kind"] == "normal"]
    summary = summarise_runs(llm_rows)
    summary["accuracy"] = summarise_runs(normal)["accuracy"]
    summary["privacy_tests_passed"] = f"{sum(r['success'] for r in rows if r['kind'] == 'privacy')}/" \
                                      f"{sum(r['kind'] == 'privacy' for r in rows)}"
    summary["unsafe_action_tests_passed"] = f"{sum(r['success'] for r in rows if r['kind'] == 'unsafe_action')}/" \
                                            f"{sum(r['kind'] == 'unsafe_action' for r in rows)}"
    a1 = load_json(os.path.join(RUNTIME_DIR, "a1", "eval_results.json"), {})
    result = {"summary": summary, "rows": rows,
              "audit_evidence": agent.audit.all(),
              "comparison_with_a1": {"assignment_1": a1.get("summary", "run /a1/evaluate first"),
                                     "assignment_2": summary},
              "evaluated_at": now_iso()}
    save_json(os.path.join(RUNTIME_DIR, "a2", "eval_results.json"), result)
    return result


# ----------------------------------------------------- staff assistant
# Staff ask different questions from students ("how many requests are
# pending?", "show S1002's fees", "any blocked attempts?"). Same agent loop,
# but a DIFFERENT, role-scoped tool set, enforced in code:
#   - staff may READ any student's record and the approval queue / audit log
#   - staff may NOT change records through chat (only via the approval screen)
STAFF_TOOLS = [
    t for t in A1_TOOLS if t["function"]["name"] in {"get_fee_status", "get_scholarship_status", "search_policy"}
] + [
    {"type": "function", "function": {
        "name": "get_student_profile",
        "description": "Get a student's name, programme, elective and contact details.",
        "parameters": {"type": "object", "properties": {"student_id": {"type": "string"}},
                       "required": ["student_id"]}}},
    {"type": "function", "function": {
        "name": "list_change_requests",
        "description": "List record-change requests, optionally filtered by status and/or student.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["pending", "approved", "rejected"]},
            "student_id": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "list_support_tickets",
        "description": "List support tickets raised by students, optionally filtered by status and/or student.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": APP_CONFIG.get("ticket_statuses", ["Open"])},
            "student_id": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "notify_student",
        "description": ("Send a notification to one student. It appears in that student's portal "
                        "immediately. Use this whenever staff ask to message, notify, remind or "
                        "inform a student — for example about a pending fee amount."),
        "parameters": {"type": "object", "properties": {
            "student_id": {"type": "string"},
            "subject": {"type": "string", "description": "Short heading, e.g. 'Pending fee'."},
            "message": {"type": "string", "description": "The message the student will read."}},
            "required": ["student_id", "message"]}}},
    {"type": "function", "function": {
        "name": "get_student_history",
        "description": ("Full history for one student — every change request, support ticket, "
                        "notification and audit event, with no time limit. Use for questions about "
                        "what happened to a student in the past, beyond what the screens show."),
        "parameters": {"type": "object", "properties": {"student_id": {"type": "string"}},
                       "required": ["student_id"]}}},
    {"type": "function", "function": {
        "name": "get_audit_events",
        "description": "Read recent audit-log events, optionally filtered by student and/or decision.",
        "parameters": {"type": "object", "properties": {
            "student_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["allowed", "blocked", "pending_approval", "approved", "rejected"]},
            "limit": {"type": "integer"}}}}},
]


class StaffAssistantA2(SafeStudentSupportAgentA2):
    """Assistant for signed-in staff. Shares the live data of the student-facing
    agent (records, approvals, audit) but keeps its own traces."""

    name = "STAFF"
    tools = STAFF_TOOLS

    def __init__(self, llm, retriever, student_agent):
        super().__init__(llm, retriever, student_agent.runtime_dir)
        self.students = student_agent.students          # same live records object
        self.traces_file = os.path.join(student_agent.runtime_dir, "staff_traces.jsonl")

    def execute_tool(self, name, args, ctx):
        allowed = {t["function"]["name"] for t in self.tools}
        if name not in allowed:                          # role check in code
            ctx["permission"] = "blocked"
            self.audit.record(ctx["student_id"], name, args.get("student_id"), "blocked", "not allowed for staff role")
            return {"error": f"'{name}' is not available to staff in chat."}
        ctx["permission"] = "allowed"
        if "student_id" in args and args["student_id"]:
            args["student_id"] = str(args["student_id"]).strip().upper()
        result = StudentSupportAgentA1.execute_tool(self, name, args, ctx)
        if args.get("student_id"):
            self.audit.record(ctx["student_id"], "staff_" + name, args["student_id"], "allowed",
                              "error" if "error" in result else "success")
        return result

    def tool_get_student_profile(self, ctx, student_id):
        s = self._student(student_id)
        return {"student_id": student_id, **{k: s[k] for k in ("name", "programme", "elective", "email", "phone")}}

    def tool_list_change_requests(self, ctx, status=None, student_id=None):
        items = [a for a in self.list_approvals(status) if not student_id or a["student_id"] == student_id]
        return {"count": len(items), "requests": items[-20:]}

    def tool_list_support_tickets(self, ctx, status=None, student_id=None):
        items = self.list_tickets(status, (student_id or "").strip().upper() or None)
        return {"count": len(items), "tickets": items[-20:]}

    def tool_notify_student(self, ctx, student_id, message, subject=""):
        note = self.send_notification(student_id, message, ctx["student_id"], subject)
        return {"sent": True, "notification_id": note["notification_id"],
                "student_id": note["student_id"], "subject": note["subject"],
                "message": note["message"],
                "note": "The student can see this in their portal now."}

    def tool_get_student_history(self, ctx, student_id):
        h = self.student_history(student_id)
        self._student(h["student_id"])                 # unknown ID becomes a clean tool error
        return {"student_id": h["student_id"],
                "change_requests": h["requests"][-15:],
                "support_tickets": h["tickets"][-15:],
                "notifications": [{k: n[k] for k in ("notification_id", "subject", "sent_at", "read_at")}
                                  for n in h["notifications"][-15:]],
                "audit_event_count": len(h["audit_events"])}

    def tool_get_audit_events(self, ctx, student_id=None, decision=None, limit=20):
        ev = [e for e in self.audit.all() if (not student_id or e["target_student"] == student_id)
              and (not decision or e["decision"] == decision)]
        return {"count": len(ev), "events": ev[-max(1, min(int(limit or 20), 50)):]}

    def system_prompt(self, staff_user, memory_items):
        return (
            f"You are the staff assistant of the {INSTITUTE_NAME} student support office.\n"
            f"Signed-in staff member: {staff_user}\n"
            "You help staff look up any student's fee, scholarship and profile details, the queue of "
            "record-change requests, the support-ticket queue, the audit log, and institute policy.\n"
            "Rules:\n"
            "1. Always use tools for data; never guess. Quote student IDs in answers.\n"
            "2. For policy questions use search_policy and answer only from it.\n"
            "3. To message, notify or remind a student, call notify_student — it delivers to their "
            "portal. Draft a clear, polite message from the data you looked up, and afterwards say "
            "what was sent.\n"
            "4. You cannot change records or decide approvals in chat; tell staff to use the "
            "Approvals tab for that.\n"
            "5. Never refer staff to a screen, tab or system that is not one of your tools or one of "
            "the console tabs (Staff assistant, Approvals, Support tickets, Audit log, Conversations, "
            "Evaluation, Baseline agent). If you genuinely cannot do something, say exactly that "
            "instead of inventing a place to do it.\n"
            "6. Keep answers short and structured (bullets or a small table)."
        )

# #############################################################################
# PART 2 — ASSIGNMENT 2                                              (END)
# #############################################################################


# #############################################################################
# PART 3 — FLASK APPLICATION (backend API + page routes)
# #############################################################################
# Two roles:
#   student → /portal       chats with the SAFE agent (Assignment 2), sees own requests
#   admin   → /admin        approvals, audit log, conversation traces, evaluations,
#                           and a tester for the baseline agent (Assignment 1)
# Pages are HTML files in templates/, styling and behaviour in static/.
# Everything below /api returns JSON.

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

LLM = GroqLLM()
RETRIEVER = PolicyRetriever(POLICY_FILE)
A1_AGENT = StudentSupportAgentA1(LLM, RETRIEVER, os.path.join(RUNTIME_DIR, "a1"))
A2_AGENT = SafeStudentSupportAgentA2(LLM, RETRIEVER, os.path.join(RUNTIME_DIR, "a2"))
STAFF_AGENT = StaffAssistantA2(LLM, RETRIEVER, A2_AGENT)

STUDENT_ID_RE = re.compile(APP_CONFIG["student_id_pattern"])
ID_HINT = f"Student ID should look like {APP_CONFIG['student_id_example']}."
FRIENDLY_LLM_ERROR = "The assistant is temporarily unavailable. Please try again in a moment."


@app.context_processor
def inject_branding():
    """Initials for the logo badge, derived from the configured app name."""
    return {"logo_text": "".join(w[0] for w in APP_CONFIG["app_name"].split()[:2]).upper()}


def api_error(msg, code=400):
    return jsonify({"error": msg}), code


def read_message(body):
    """Shared input validation for chat endpoints."""
    msg = (body.get("message") or "").strip()
    if not msg:
        return None, "Please type a message."
    if len(msg) > MAX_MESSAGE_CHARS:
        return None, f"Message must be at most {MAX_MESSAGE_CHARS} characters."
    return msg, None


def require_role(role, page=False):
    """Decorator: the Flask session decides who you are, never the request body."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if session.get("role") != role:
                return redirect(url_for("login_page")) if page else api_error("Please sign in.", 401)
            return fn(*a, **kw)
        return wrapper
    return deco


def public_trace(trace):
    """The evidence trail shown next to a reply, in the chat itself.

    Three views the page renders as tabs:
      Trace    — the step timeline (context, model calls, retrieval, tools, finish)
      Requests — one row per model call and per tool call, with timing and tokens
      Memory   — the long-term memory items selected for this question
    Everything here has already been through redact(), and it only ever
    describes the asker's own run."""
    steps = trace.get("steps", [])
    requests = []
    for s in steps:
        if s["type"] == "model_call":
            requests.append({"kind": "model", "name": f"Model call {s['n']}",
                             "detail": ", ".join(s["requested"]), "latency_ms": s["latency_ms"],
                             "input_tokens": s["input_tokens"], "output_tokens": s["output_tokens"],
                             "ok": True})
        elif s["type"] == "tool":
            requests.append({"kind": "tool", "name": s["name"],
                             "detail": s.get("permission") if s.get("permission") != "n/a" else "",
                             "latency_ms": s["latency_ms"], "ok": s["ok"]})
    return {
        "tools": [s["tool"] for s in trace["tool_calls"]],
        "sources": trace["retrieved_evidence"]["policy_chunks"],
        "memory": trace["retrieved_evidence"]["memory_items"],
        "latency_ms": trace["latency_ms"],
        "input_tokens": trace["input_tokens"], "output_tokens": trace["output_tokens"],
        "provider": trace.get("provider", LLM_PROVIDER), "model": trace.get("model", GROQ_MODEL),
        "model_calls": trace.get("model_calls", 0), "tool_call_count": trace.get("tool_call_count", 0),
        "blocked_tool_calls": trace.get("blocked_tool_calls", 0),
        "finish_reason": trace.get("finish_reason", ""),
        "steps": steps, "requests": requests, "trace_id": trace["trace_id"],
    }


# ------------------------------------------------------------- pages
@app.get("/")
def home():
    role = session.get("role")
    return redirect(url_for("portal_page" if role == "student" else "admin_page" if role == "admin" else "login_page"))


@app.get("/login")
def login_page():
    return render_template("login.html", cfg=APP_CONFIG)


@app.get("/portal")
@require_role("student", page=True)
def portal_page():
    return render_template("portal.html", cfg=APP_CONFIG)


@app.get("/admin")
@require_role("admin", page=True)
def admin_page():
    return render_template("admin.html", cfg=APP_CONFIG)


# ------------------------------------------------------------- auth API
@app.post("/api/auth/student-login")
def student_login():
    body = request.get_json(silent=True) or {}
    sid = (body.get("student_id") or "").strip().upper()
    if not STUDENT_ID_RE.match(sid):
        return api_error(ID_HINT)
    if not A2_AGENT.verify_login(sid, body.get("pin")):
        return api_error("Incorrect student ID or PIN.", 401)
    session.clear()
    session.update(role="student", student_id=sid)
    return jsonify({"redirect": url_for("portal_page")})


@app.post("/api/auth/admin-login")
def admin_login():
    body = request.get_json(silent=True) or {}
    user, pwd = body.get("username") or "", body.get("password") or ""
    ok = bool(ADMIN_PASSWORD) and hmac.compare_digest(user, ADMIN_USERNAME) \
        and hmac.compare_digest(pwd, ADMIN_PASSWORD)
    A2_AGENT.audit.record(user or "unknown", "admin_login", None, "allowed" if ok else "blocked",
                          "success" if ok else "invalid credentials")
    if not ok:
        return api_error("Incorrect username or password." if ADMIN_PASSWORD
                         else "Admin login is disabled: set ADMIN_PASSWORD in .env.", 401)
    session.clear()
    session.update(role="admin", admin=user)
    return jsonify({"redirect": url_for("admin_page")})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"redirect": url_for("login_page")})


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "model": LLM.model, "groq_key_configured": bool(os.getenv("GROQ_API_KEY")),
                    "admin_login_enabled": bool(ADMIN_PASSWORD)})


# ------------------------------------------------------------- student API (Assignment 2 agent)
def current_student():
    """The signed-in student's record, or None if the record no longer exists.
    Identity always comes from the session, never from the request body."""
    sid = session.get("student_id", "")
    return sid, A2_AGENT.students.get(sid)


@app.get("/api/student/profile")
@require_role("student")
def student_profile():
    """Everything the portal sidebar shows about the signed-in student.
    Only their own record: the session decides whose data this is."""
    sid, s = current_student()
    if not s:
        return api_error("Your record could not be found. Please contact the office.", 404)
    fee = s.get("fee", {}) or {}
    sch = s.get("scholarship", {}) or {}
    total, paid = fee.get("total_fee", 0) or 0, fee.get("paid", 0) or 0
    return jsonify({
        "student_id": sid,
        "name": s.get("name", ""),
        "programme": s.get("programme", ""),
        "elective": s.get("elective", ""),
        "email": s.get("email", ""),
        "phone": s.get("phone", ""),
        "fee": {"currency": fee.get("currency", ""), "total_fee": total, "paid": paid,
                "outstanding": fee.get("outstanding", 0) or 0,
                "due_date": fee.get("due_date", ""), "status": fee.get("status", ""),
                "paid_pct": round(paid / total * 100) if total else 0},
        "scholarship": {"name": sch.get("name", ""), "status": sch.get("status", ""),
                        "amount": sch.get("amount", 0) or 0},
        "changeable_fields": APP_CONFIG["changeable_fields"],   # list, keeps config order
    })


@app.get("/api/student/requests")
@require_role("student")
def student_requests():
    """Change requests raised by this student, newest first."""
    mine = [a for a in A2_AGENT.list_approvals() if a["student_id"] == session["student_id"]]
    return jsonify(mine[::-1])


@app.get("/api/student/tickets")
@require_role("student")
def student_tickets():
    """Support tickets the assistant opened for this student.
    Without this the student had no way to see a ticket after it was raised."""
    mine = A2_AGENT.list_tickets(None, session["student_id"])
    # The status list travels with the data so the page never hard-codes it.
    return jsonify({"statuses": TICKET_STATUSES, "tickets": mine[::-1]})


@app.get("/api/student/notifications")
@require_role("student")
def student_notifications():
    """Messages the support office sent to this student."""
    mine = A2_AGENT.list_notifications(session["student_id"])
    return jsonify({"unread": sum(1 for n in mine if not n["read_at"]), "notifications": mine[::-1]})


@app.post("/api/student/notifications/<notification_id>/read")
@require_role("student")
def student_read_notification(notification_id):
    try:
        return jsonify(A2_AGENT.mark_notification_read(notification_id, session["student_id"]))
    except KeyError as e:
        return api_error(str(e).strip("'\""), 404)


@app.get("/api/student/preferences")
@require_role("student")
def student_preferences():
    """What the assistant has remembered about this student (long-term memory).
    Showing it makes the memory from Assignment 1 visible and correctable."""
    return jsonify(A2_AGENT.memory.all_items(session["student_id"])[::-1])


@app.delete("/api/student/preferences/<key>")
@require_role("student")
def student_forget_preference(key):
    """Student-controlled deletion of one remembered fact. Audited like any
    other action on a record."""
    sid = session["student_id"]
    removed = A2_AGENT.memory.delete(sid, (key or "").strip().lower())
    A2_AGENT.audit.record(sid, "delete_memory", sid, "allowed",
                          "deleted" if removed else "not found", {"key": key})
    if not removed:
        return api_error("That saved detail no longer exists.", 404)
    return jsonify({"deleted": key})


@app.post("/api/student/chat")
@require_role("student")
def student_chat():
    body = request.get_json(silent=True) or {}
    msg, err = read_message(body)
    if err:
        return api_error(err)
    trace = A2_AGENT.run(session["student_id"], (body.get("conversation_id") or "default")[:64], msg)
    if trace["error"]:
        return jsonify({"error": FRIENDLY_LLM_ERROR}), 503
    return jsonify({"answer": trace["answer"], "activity": public_trace(trace)})


# ------------------------------------------------------------- admin API
def requested_window():
    """`?window=all` shows everything; anything else uses the configured window.
    Returns None for 'no limit'."""
    if (request.args.get("window") or "").lower() == "all":
        return None
    return RECENT_WINDOW_HOURS


def apply_window(items, hours, timestamp_key, keep):
    """Trims a list to the recent window — but never hides a record that still
    needs someone to act on it. A two-day-old pending approval must stay
    visible, or staff would quietly stop seeing their own backlog."""
    if not hours:
        return items
    return [i for i in items if keep(i) or within_hours(i.get(timestamp_key), hours)]


@app.get("/api/admin/approvals")
@require_role("admin")
def admin_approvals():
    status = request.args.get("status") or None
    sid = (request.args.get("student_id") or "").strip().upper() or None
    hours = requested_window()
    items = [a for a in A2_AGENT.list_approvals(status) if not sid or a["student_id"] == sid]
    shown = apply_window(items, hours, "requested_at", keep=lambda a: a["status"] == "pending")
    for a in shown:
        a["student_name"] = A2_AGENT.students.get(a["student_id"], {}).get("name", "")
    return jsonify({"window_hours": hours, "total": len(items), "shown": len(shown),
                    "approvals": shown[::-1]})


@app.post("/api/admin/approvals/<request_id>/decision")
@require_role("admin")
def admin_decide(request_id):
    decision = ((request.get_json(silent=True) or {}).get("decision") or "").lower()
    if decision not in {"approve", "reject"}:
        return api_error("Decision must be 'approve' or 'reject'.")
    try:
        req = A2_AGENT.decide_approval(request_id, session["admin"], decision == "approve")
    except KeyError as e:
        return api_error(str(e).strip("'\""), 404)
    except ValueError as e:
        return api_error(str(e), 409)
    return jsonify(req)


@app.get("/api/admin/tickets")
@require_role("admin")
def admin_tickets():
    """Support tickets raised by students through the assistant."""
    status = request.args.get("status") or None
    sid = (request.args.get("student_id") or "").strip().upper() or None
    hours = requested_window()
    items = A2_AGENT.list_tickets(status, sid)
    # An unresolved ticket always shows, however old.
    shown = apply_window(items, hours, "created_at", keep=lambda t: t.get("status") != SETTLED_TICKET_STATUS)
    for t in shown:
        t["student_name"] = A2_AGENT.students.get(t.get("student_id", ""), {}).get("name", "")
    return jsonify({"statuses": TICKET_STATUSES, "window_hours": hours,
                    "total": len(items), "shown": len(shown), "tickets": shown[::-1]})


@app.get("/api/admin/students/<student_id>/history")
@require_role("admin")
def admin_student_history(student_id):
    """The complete trail for one student, with no window applied."""
    sid = (student_id or "").strip().upper()
    if sid not in A2_AGENT.students:
        return api_error(f"Student {sid} was not found.", 404)
    h = A2_AGENT.student_history(sid)
    h["name"] = A2_AGENT.students[sid].get("name", "")
    return jsonify(h)


@app.post("/api/admin/notifications")
@require_role("admin")
def admin_send_notification():
    body = request.get_json(silent=True) or {}
    try:
        note = A2_AGENT.send_notification(body.get("student_id"), body.get("message"),
                                          session["admin"], body.get("subject") or "")
    except KeyError as e:
        return api_error(str(e).strip("'\""), 404)
    except ValueError as e:
        return api_error(str(e), 400)
    return jsonify(note)


@app.post("/api/admin/tickets/<ticket_id>/status")
@require_role("admin")
def admin_ticket_status(ticket_id):
    status = ((request.get_json(silent=True) or {}).get("status") or "").strip()
    try:
        ticket = A2_AGENT.set_ticket_status(ticket_id, status, session["admin"])
    except KeyError as e:
        return api_error(str(e).strip("'\""), 404)
    except ValueError as e:
        return api_error(str(e), 409)
    return jsonify(ticket)


# ------------------------------------------------------------- student accounts
FEE_STATUSES = APP_CONFIG.get("fee_statuses", ["Unpaid", "Partially Paid", "Fully Paid"])
SCHOLARSHIP_STATUSES = APP_CONFIG.get("scholarship_statuses", ["Not Applied", "Approved"])
MIN_PIN_LENGTH = int(APP_CONFIG.get("min_pin_length", 4))


def create_student_account(body, actor):
    """Adds a student to the seed file and reloads every agent's view of the
    records. The PIN is hashed here and never stored or returned in clear."""
    sid = (body.get("student_id") or "").strip().upper()
    if not STUDENT_ID_RE.match(sid):
        raise ValueError(ID_HINT)
    students = load_json(STUDENTS_FILE, {})
    if sid in students:
        raise ValueError(f"Student {sid} already exists.")

    pin = str(body.get("pin") or "").strip()
    if len(pin) < MIN_PIN_LENGTH or not pin.isdigit():
        raise ValueError(f"PIN must be at least {MIN_PIN_LENGTH} digits.")
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required.")

    def number(key, default=0):
        try:
            value = float(body.get(key) if body.get(key) not in (None, "") else default)
        except (TypeError, ValueError):
            raise ValueError(f"{key.replace('_', ' ')} must be a number.")
        if value < 0:
            raise ValueError(f"{key.replace('_', ' ')} cannot be negative.")
        return int(value)

    total, paid = number("total_fee"), number("paid")
    if paid > total:
        raise ValueError("Paid amount cannot be more than the total fee.")
    fee_status = body.get("fee_status") or FEE_STATUSES[0]
    if fee_status not in FEE_STATUSES:
        raise ValueError(f"Fee status must be one of: {', '.join(FEE_STATUSES)}.")
    sch_status = body.get("scholarship_status") or SCHOLARSHIP_STATUSES[0]
    if sch_status not in SCHOLARSHIP_STATUSES:
        raise ValueError(f"Scholarship status must be one of: {', '.join(SCHOLARSHIP_STATUSES)}.")

    students[sid] = {
        "name": name,
        "programme": (body.get("programme") or "").strip(),
        "email": (body.get("email") or "").strip(),
        "phone": (body.get("phone") or "").strip(),
        "elective": (body.get("elective") or "").strip(),
        "fee": {"currency": APP_CONFIG.get("currency", "INR"), "total_fee": total, "paid": paid,
                "outstanding": total - paid, "due_date": (body.get("due_date") or "").strip(),
                "status": fee_status},
        "scholarship": {"name": (body.get("scholarship_name") or "None").strip(),
                        "status": sch_status, "amount": number("scholarship_amount")},
        "pin_hash": generate_password_hash(pin),
    }
    save_json(STUDENTS_FILE, students)
    for agent in (A1_AGENT, A2_AGENT):          # pick the new record up without a restart
        agent.students = agent._load_students()
    STAFF_AGENT.students = A2_AGENT.students    # staff share the A2 records object
    A2_AGENT.audit.record(actor, "create_student", sid, "allowed", "account created",
                          {"name": name, "programme": students[sid]["programme"]})
    return {k: v for k, v in students[sid].items() if k != "pin_hash"} | {"student_id": sid}


@app.get("/api/admin/students")
@require_role("admin")
def admin_list_students():
    """Roster for the admin console. PIN hashes are never sent to the browser."""
    rows = [{"student_id": sid, "name": s.get("name", ""), "programme": s.get("programme", ""),
             "elective": s.get("elective", ""), "email": s.get("email", ""), "phone": s.get("phone", ""),
             "outstanding": (s.get("fee") or {}).get("outstanding", 0),
             "currency": (s.get("fee") or {}).get("currency", ""),
             "scholarship_status": (s.get("scholarship") or {}).get("status", "")}
            for sid, s in sorted(A2_AGENT.students.items())]
    return jsonify({"students": rows, "fee_statuses": FEE_STATUSES,
                    "scholarship_statuses": SCHOLARSHIP_STATUSES,
                    "id_hint": ID_HINT, "min_pin_length": MIN_PIN_LENGTH,
                    "currency": APP_CONFIG.get("currency", "")})


@app.post("/api/admin/students")
@require_role("admin")
def admin_create_student():
    try:
        return jsonify(create_student_account(request.get_json(silent=True) or {}, session["admin"]))
    except ValueError as e:
        return api_error(str(e))


@app.get("/api/admin/audit")
@require_role("admin")
def admin_audit():
    return jsonify(A2_AGENT.audit.all()[-200:][::-1])


@app.get("/api/admin/traces")
@require_role("admin")
def admin_traces():
    agent = {"a1": A1_AGENT, "staff": STAFF_AGENT}.get(request.args.get("agent"), A2_AGENT)
    return jsonify(read_jsonl(agent.traces_file)[-100:][::-1])


@app.get("/api/admin/evaluations")
@require_role("admin")
def admin_evaluations():
    return jsonify({"a1": load_json(os.path.join(RUNTIME_DIR, "a1", "eval_results.json"), None),
                    "a2": load_json(os.path.join(RUNTIME_DIR, "a2", "eval_results.json"), None)})


@app.post("/api/admin/evaluate/<which>")
@require_role("admin")
def admin_evaluate(which):
    if which == "a1":
        return jsonify(run_evaluation_a1(LLM, RETRIEVER))
    if which == "a2":
        return jsonify(run_evaluation_a2(LLM, RETRIEVER))
    return api_error("Unknown evaluation.", 404)


@app.post("/api/admin/assistant")
@require_role("admin")
def admin_assistant():
    """Staff assistant: role-scoped tools (any student's record, approvals queue, audit)."""
    body = request.get_json(silent=True) or {}
    msg, err = read_message(body)
    if err:
        return api_error(err)
    trace = STAFF_AGENT.run(session["admin"], (body.get("conversation_id") or "default")[:64], msg)
    if trace["error"]:
        return jsonify({"error": trace["error"]}), 503
    return jsonify({"answer": trace["answer"], "activity": public_trace(trace)})


@app.post("/api/admin/baseline-chat")
@require_role("admin")
def admin_baseline_chat():
    """Lets staff exercise the Assignment 1 baseline agent for any student ID
    (A1 has no identity layer by design — that is what A2 adds)."""
    body = request.get_json(silent=True) or {}
    msg, err = read_message(body)
    if err:
        return api_error(err)
    sid = (body.get("student_id") or "").strip().upper()
    if not STUDENT_ID_RE.match(sid):
        return api_error(ID_HINT)
    trace = A1_AGENT.run(sid, (body.get("conversation_id") or "default")[:64], msg)
    if trace["error"]:  # staff see the real reason
        return jsonify({"error": trace["error"]}), 503
    return jsonify({"answer": trace["answer"], "trace": trace})


def write_evaluation_report():
    """Runs both test sets and writes the evaluation table the assignments ask
    for as a file, so the submission has it without needing a screen for it.
    The per-query evidence lives in the chat trace panel instead."""
    def table(result, title):
        if not result:
            return f"### {title}\n\n_Not run._\n"
        s = result["summary"]
        head = (f"### {title}\n\n"
                f"Runs: {s['runs']} · Accuracy: {round(s['accuracy'] * 100)}% · "
                f"Avg latency: {s['avg_latency_ms']} ms · "
                f"Avg tokens in/out: {s['avg_input_tokens']}/{s['avg_output_tokens']}\n\n"
                "| ID | Question | Tools called | Tool choice | Result | Latency | Tokens in/out | Evidence |\n"
                "|---|---|---|---|---|---|---|---|\n")
        for r in result["rows"]:
            ev = "; ".join((r.get("evidence") or {}).get("policy_chunks", [])
                           + (r.get("evidence") or {}).get("memory_items", [])) or "—"
            head += (f"| {r['id']} | {r['question']} | {', '.join(r['tools_called']) or '—'} | "
                     f"{'ok' if r['tool_choice_ok'] else 'off'} | {'pass' if r['success'] else 'fail'} | "
                     f"{r['latency_ms']} ms | {r['input_tokens']}/{r['output_tokens']} | {ev} |\n")
        return head + "\n"

    print("Running the Assignment 1 baseline test set...")
    a1 = run_evaluation_a1(LLM, RETRIEVER)
    print("Running the Assignment 2 safety test set...")
    a2 = run_evaluation_a2(LLM, RETRIEVER)
    s1, s2 = a1["summary"], a2["summary"]
    report = (
        f"# Evaluation report\n\nGenerated {now_iso()} · model `{LLM.model}` · provider `{LLM_PROVIDER}`\n\n"
        "## Comparison: baseline (Assignment 1) vs safe agent (Assignment 2)\n\n"
        "| Measure | Baseline (A1) | Safe agent (A2) |\n|---|---|---|\n"
        f"| Task accuracy | {round(s1['accuracy'] * 100)}% | {round(s2['accuracy'] * 100)}% |\n"
        f"| Avg latency | {s1['avg_latency_ms']} ms | {s2['avg_latency_ms']} ms |\n"
        f"| Avg input tokens | {s1['avg_input_tokens']} | {s2['avg_input_tokens']} |\n"
        f"| Avg output tokens | {s1['avg_output_tokens']} | {s2['avg_output_tokens']} |\n"
        f"| Privacy tests passed | not applicable — no identity layer | {s2['privacy_tests_passed']} |\n"
        f"| Unsafe-action tests passed | not applicable — no approval step | {s2['unsafe_action_tests_passed']} |\n\n"
        + table(a1, "Baseline agent — per question")
        + table(a2, "Safe agent — per question")
        + f"## Audit evidence\n\n{len(a2['audit_evidence'])} audit records were written during the safety run.\n")
    path = os.path.join(RUNTIME_DIR, "evaluation_report.md")
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nWritten: {path}")
    print(f"A1 accuracy {round(s1['accuracy'] * 100)}%  ·  A2 accuracy {round(s2['accuracy'] * 100)}%  "
          f"·  privacy {s2['privacy_tests_passed']}  ·  unsafe-action {s2['unsafe_action_tests_passed']}")
    return path


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--hash-pin":   # helper: python app.py --hash-pin 1234
        print(generate_password_hash(sys.argv[2]))
        sys.exit(0)
    if len(sys.argv) == 2 and sys.argv[1] == "--evaluate":   # writes runtime/evaluation_report.md
        write_evaluation_report()
        sys.exit(0)
    if not os.getenv("GROQ_API_KEY"):
        log.warning("GROQ_API_KEY is not set — the assistant will not be able to answer.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
