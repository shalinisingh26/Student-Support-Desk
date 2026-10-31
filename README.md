# Student Support Desk

An AI support assistant for students (fees, scholarships, course changes, support requests) with a staff console for approvals, audit and evaluation. Built for Agentic AI Engineering Assignments 1 & 2.

## Architecture
```
Browser ── templates/ + static/  (login, student portal, admin console)
   │  fetch JSON
   ▼
app.py  (Flask backend — ALL agent logic lives here)
   PART 0  shared: Groq client, policy retriever, memory, redaction
   PART 1  Assignment 1: StudentSupportAgentA1 (baseline agent) + evaluation
   PART 2  Assignment 2: SafeStudentSupportAgentA2 (inherits A1) + auth, approvals, audit, evaluation
   PART 3  Flask: page routes + /api/* endpoints
   │
   ├── Groq API (LLM with tool calling)
   └── data/ (sample records, policy handbook)   runtime/ (memory, traces, audit, approvals)
```
- **Students** sign in at `/login` → `/portal`, and chat with the **safe agent** (Assignment 2).
- **Staff** sign in at `/login` (Staff tab) → `/admin`: staff assistant, approvals, support tickets,
  **students** (roster + create account), audit log, conversation traces, evaluation (A1 vs A2).

### Per-query evidence trail — in the chat
Every assistant reply, in **both** the student portal and the staff assistant, carries a **Trace**
toggle. Opening it shows that one query's pipeline in three views:
- **Trace** — the step timeline: context selected → each model call (latency + tokens + what it
  requested) → retrieval (chunk ids) → each tool (timing, permission, approval) → finished (reason).
- **Requests** — one row per model call and per tool call, with timing and tokens.
- **Memory** — the long-term memory items selected for this question, plus the policy retrieved.

Header stats: latency, tokens in/out, model calls, tool calls, blocked calls, provider.


Every run records a `steps` timeline: context selected (history + memory items + tools offered) → each
**model call** with its own latency, tokens and the tools it requested → **retrieval** with the chunk ids
→ each **tool** with its timing, permission decision and approval status → **finished** with the reason.
The trace header carries `model_calls`, `tool_call_count`, `blocked_tool_calls`, `provider`, `model`,
total latency and total tokens. Admin → **Conversations** → click any row renders this timeline.

### Evaluation — a report file, not a screen
Both briefs require an evaluation table comparing the safe agent with the Assignment 1 baseline.
It is produced on the command line rather than in the console, because it is a submission artefact
rather than something school staff would use:
```bash
python app.py --evaluate      # → runtime/evaluation_report.md
```
The report contains the A1-vs-A2 comparison table (accuracy, latency, tokens, privacy tests,
unsafe-action tests) and a per-question table for each agent. `A1_AGENT` is kept purely as the control
for that comparison: same tools, no identity layer. Both test sets run on isolated copies of the data
(`runtime/a1_eval`, `runtime/a2_eval`) and never touch live records.

### Creating a student account
Admin → **Students** → *Add student*. The PIN is hashed with `generate_password_hash` before storage and
never returned by any endpoint; the new record is written to `data/students.json`, every agent reloads it
without a restart, and the creation is written to the audit log.

### Student portal panels (`/portal`)
Profile · Messages from the office · Fee summary (progress + due-date badge) · Scholarship ·
My change requests · My support tickets. Requests and tickets show the newest item per group with a
**View all** toggle. Every panel shows only the signed-in student's own data, and everything on the
page can also be asked in the chat (`get_student_profile`, `list_my_change_requests`,
`list_my_support_tickets`, `list_my_notifications`).

### Retention: a 24-hour working view over a full history
The admin **Approvals** and **Support tickets** tabs default to the last `recent_window_hours` (24),
with a **Full history** option. Two rules make this safe:
- **Anything still awaiting action is never hidden** — a three-day-old *pending* approval or *open*
  ticket always appears, so a window can't bury the backlog.
- **Nothing is deleted.** Every record keeps its own `history` trail (who did what, when, `from → to`),
  shown by the **History** link on each row. `GET /api/admin/students/<id>/history` and the staff
  assistant's `get_student_history` tool return one student's complete trail with no window.
The student portal is never windowed — students always see all of their own items.

Long-term memory still runs behind the scenes (the `save_memory` tool): it is not shown in the portal,
but `/api/student/preferences` exposes it and `DELETE /api/student/preferences/<key>` removes one item.

### Two assistants, two permission sets (enforced in code)
| | Student assistant (`/portal`) | Staff assistant (`/admin`) |
|---|---|---|
| Records it can read | Only the signed-in student's | Any student's |
| Can change records | Only by filing a request that waits for approval | No — staff approve/reject on the Approvals tab |
| Extra tools | save preferences, raise support ticket | approvals queue, audit log, student profile |

## Configuration — nothing institution-specific is in the code
| What | Where |
|---|---|
| Secrets, admin login, model, tuning numbers | `.env` (see `.env.example`) |
| App/institute name, tagline, login highlights, suggested questions, support categories, changeable fields, student-ID format | `data/app_config.json` |
| Student records (PINs stored as hashes) | `data/students.json` |
| Policy handbook | `data/policy_handbook.md` |
| Evaluation test cases | `data/test_cases.json` |
| What the assistant will answer outside student support | `out_of_scope_policy` in `data/app_config.json` |

**Scope is a prompt decision, not a rule in code.** Nothing inspects the topic of a message — the only
input validation is "not empty" and "within the length limit". `out_of_scope_policy` is injected into
the system prompt as rule 7, so the agent's breadth is changed by editing one config sentence:
the shipped default answers general questions briefly and then steers back to student support;
replacing it with *"If the question is not about student support, politely say it is outside what you
can help with."* produces a strict desk. No code change either way.

New PIN hash: `python app.py --hash-pin 1234` → paste the output as `pin_hash`.

## Setup (VS Code)
```bash
python -m venv venv
venv\Scripts\activate             # Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # Mac/Linux: cp .env.example .env  — then fill it in
python app.py                     # http://localhost:5000
```
Demo students: S1001 / 1111, S1002 / 2222, S1003 / 3333. Staff: `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`.
Check configuration at `http://localhost:5000/api/health`.

## API
| Method | Route | Who |
|---|---|---|
| POST | `/api/auth/student-login`, `/api/auth/admin-login`, `/api/auth/logout` | anyone |
| GET | `/api/health` | anyone |
| GET | `/api/student/profile`, `/api/student/requests`, `/api/student/tickets`, `/api/student/notifications`, `/api/student/preferences` | student |
| POST | `/api/student/notifications/<id>/read` | student |
| DELETE | `/api/student/preferences/<key>` | student |
| POST | `/api/student/chat` | student (safe agent, A2) |
| GET | `/api/admin/approvals`, `/api/admin/tickets` (both accept `?window=all&status=&student_id=`), `/api/admin/students`, `/api/admin/students/<id>/history`, `/api/admin/audit`, `/api/admin/traces?agent=a1|a2|staff`, `/api/admin/evaluations` | admin |
| POST | `/api/admin/students`, `/api/admin/approvals/<id>/decision`, `/api/admin/tickets/<id>/status`, `/api/admin/notifications`, `/api/admin/evaluate/a1|a2`, `/api/admin/assistant`, `/api/admin/baseline-chat` | admin |

Evaluations run on isolated copies (`runtime/a1_eval`, `runtime/a2_eval`) and never change live data.
