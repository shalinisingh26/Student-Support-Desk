// Shared helpers for all pages.

// Calls the JSON API. Redirects to the login page if the session has expired.
async function api(url, options = {}) {
  const res = await fetch(url, {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-JSON response */ }
  if (res.status === 401 && !options.allow401) { window.location.href = "/login"; throw new Error("unauthorised"); }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Small, safe markdown renderer for agent replies: bold, code, lists, tables, paragraphs.
function renderMarkdown(text) {
  const inline = s => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const lines = esc(text).split(/\r?\n/);
  let html = "", list = false, table = [];
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter(r => !/^\s*\|?\s*:?-{2,}/.test(r)).map(r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim())));
    html += "<table>" + rows.map((r, i) => "<tr>" + r.map(c => i === 0 ? `<th>${c}</th>` : `<td>${c}</td>`).join("") + "</tr>").join("") + "</table>";
    table = [];
  };
  for (const line of lines) {
    if (/^\s*\|.*\|\s*$/.test(line)) { if (list) { html += "</ul>"; list = false; } table.push(line); continue; }
    flushTable();
    const item = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (item) { if (!list) { html += "<ul>"; list = true; } html += `<li>${inline(item[1])}</li>`; continue; }
    if (list) { html += "</ul>"; list = false; }
    if (line.trim()) html += `<p>${inline(line.replace(/^#+\s*/, ""))}</p>`;
  }
  flushTable();
  return html + (list ? "</ul>" : "");
}

function pillFor(status) {
  const s = String(status || "").toLowerCase();
  const cls = ["approved", "allowed", "success", "open"].includes(s) ? "pill-ok"
    : ["pending", "pending_approval"].includes(s) ? "pill-warn"
    : ["rejected", "blocked", "error"].includes(s) ? "pill-bad" : "pill-neutral";
  return `<span class="pill ${cls}">${esc(s.replace("_", " "))}</span>`;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast"; el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

async function logout() {
  const d = await api("/api/auth/logout", { method: "POST" });
  window.location.href = d.redirect;
}

// ===== Per-query trace panel (shown under every assistant reply) ============
// Three tabs over one run: the step timeline, the individual model/tool
// requests, and the memory items that were selected for the question.
const STEP_LABEL = {
  context_selected: "Context selected", model_call: "Model call",
  retrieval: "Retrieval", tool: "Tool", finished: "Finished",
};

function stepTitle(s) {
  if (s.type === "model_call") return `Model call ${s.n}`;
  if (s.type === "tool") return `Tool: ${esc(s.name)}`;
  if (s.type === "finished") return `Finished: ${esc(s.reason)}`;
  return STEP_LABEL[s.type] || s.type;
}

function stepDetail(s) {
  if (s.type === "context_selected")
    return `history ${s.history_messages} msg · memory ${s.memory_items.length ? esc(s.memory_items.join(", ")) : "none"}`;
  if (s.type === "model_call")
    return `${s.latency_ms} ms<br>requested: ${esc(s.requested.join(", "))}`;
  if (s.type === "retrieval") return esc(s.chunks.join(", "));
  if (s.type === "tool")
    return `${s.latency_ms} ms<br>${s.ok ? "ok" : "error"}`
      + (s.permission && s.permission !== "n/a" ? ` · ${esc(s.permission)}` : "")
      + (s.approval_status ? ` · approval ${esc(s.approval_status)}` : "");
  return "";
}

function stepClass(s) {
  if (s.type === "tool" && (!s.ok || s.permission === "blocked")) return "tl-bad";
  if (s.type === "model_call") return "tl-model";
  return "tl-ok";
}

let tracePanelSeq = 0;

// `a` is the `activity` object returned by the chat endpoints.
function renderTracePanel(a) {
  if (!a || !a.steps) return "";
  const id = "tp" + (++tracePanelSeq);
  const timeline = a.steps.length
    ? `<ol class="timeline">${a.steps.map(s => `
        <li class="${stepClass(s)}">
          <div class="tl-head">${stepTitle(s)}</div>
          <div class="tl-detail">${stepDetail(s)}</div>
        </li>`).join("")}</ol>`
    : `<p class="muted small">No steps recorded.</p>`;

  const requests = a.requests.length
    ? `<table class="req-table"><thead><tr><th>#</th><th>Step</th><th>Detail</th><th>Latency</th><th>Tokens</th></tr></thead>
       <tbody>${a.requests.map((r, i) => `<tr>
         <td>${i + 1}</td>
         <td><span class="pill ${r.kind === "model" ? "pill-neutral" : r.ok ? "pill-ok" : "pill-bad"}">${esc(r.kind)}</span> ${esc(r.name)}</td>
         <td class="small">${esc(r.detail || "—")}</td>
         <td>${r.latency_ms} ms</td>
         <td>${r.input_tokens !== undefined ? `${r.input_tokens}/${r.output_tokens}` : "—"}</td>
       </tr>`).join("")}</tbody></table>`
    : `<p class="muted small">No model or tool requests recorded.</p>`;

  const memory = a.memory && a.memory.length
    ? `<ul class="mem-list">${a.memory.map(m => `<li><code>${esc(m)}</code></li>`).join("")}</ul>`
    : `<p class="muted small">No memory items were selected for this question.</p>`;
  const sources = a.sources && a.sources.length
    ? `<div class="tp-sub"><strong>Policy retrieved</strong><ul class="mem-list">${
        a.sources.map(s => `<li><code>${esc(s)}</code></li>`).join("")}</ul></div>` : "";

  return `
  <div class="trace-panel" id="${id}">
    <div class="tp-tabs">
      <button class="tp-tab active" data-tp="trace">Trace</button>
      <button class="tp-tab" data-tp="requests">Requests</button>
      <button class="tp-tab" data-tp="memory">Memory</button>
      <span class="tp-finish muted small">${esc(a.finish_reason || "")}</span>
    </div>
    <div class="tp-stats">
      <div class="tp-stat"><div class="v">${a.latency_ms} ms</div><div class="k">latency</div></div>
      <div class="tp-stat"><div class="v">${a.input_tokens}/${a.output_tokens}</div><div class="k">tokens in/out</div></div>
      <div class="tp-stat"><div class="v">${a.model_calls}</div><div class="k">model calls</div></div>
      <div class="tp-stat"><div class="v">${a.tool_call_count}</div><div class="k">tool calls</div></div>
      ${a.blocked_tool_calls ? `<div class="tp-stat bad"><div class="v">${a.blocked_tool_calls}</div><div class="k">blocked</div></div>` : ""}
      <div class="tp-stat"><div class="v">${esc(a.provider)}</div><div class="k">provider</div></div>
    </div>
    <div class="tp-body" data-pane="trace">${timeline}</div>
    <div class="tp-body hidden" data-pane="requests">${requests}</div>
    <div class="tp-body hidden" data-pane="memory">${memory}${sources}</div>
  </div>`;
}

// One delegated handler covers every trace panel on the page.
document.addEventListener("click", e => {
  const tab = e.target.closest(".tp-tab");
  if (!tab) return;
  const panel = tab.closest(".trace-panel");
  panel.querySelectorAll(".tp-tab").forEach(t => t.classList.toggle("active", t === tab));
  panel.querySelectorAll(".tp-body").forEach(b =>
    b.classList.toggle("hidden", b.dataset.pane !== tab.dataset.tp));
});

// A collapsed "Trace" link that opens the panel for one reply.
function traceToggle(activity) {
  if (!activity || !activity.steps) return "";
  return `<details class="trace-wrap"><summary>Trace · ${activity.model_calls} model call${
    activity.model_calls === 1 ? "" : "s"} · ${activity.tool_call_count} tool call${
    activity.tool_call_count === 1 ? "" : "s"} · ${(activity.latency_ms / 1000).toFixed(1)}s</summary>
    ${renderTracePanel(activity)}</details>`;
}

// Human-readable names for tools, used in the student portal and admin console.
const TOOL_LABELS = {
  get_fee_status: "Checked fee record",
  get_scholarship_status: "Checked scholarship record",
  create_support_request: "Created support ticket",
  search_policy: "Searched policy handbook",
  save_memory: "Updated saved preferences",
  request_record_change: "Submitted change for approval",
};
