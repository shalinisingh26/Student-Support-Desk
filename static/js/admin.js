// Admin console: staff assistant, approvals, tickets, students, audit and traces.
// The per-query trace panel itself is rendered by common.js, so the student
// portal and the staff chat show exactly the same thing.

// ---------- tabs ----------
const loaders = {};
document.querySelectorAll(".tab").forEach(tab => tab.onclick = () => {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t === tab));
  document.querySelectorAll("main > section").forEach(s => s.classList.toggle("hidden", s.id !== tab.dataset.tab));
  loaders[tab.dataset.tab]?.();
});
const emptyRow = (cols, text) => `<tr><td colspan="${cols}" class="empty">${text}</td></tr>`;

// ---------- shared: a record's own history trail ----------
// Lists are windowed to the recent period, but each record keeps every state
// it passed through — this renders that trail inline.
function historyRow(id, history, cols) {
  const rows = (history || []).map(h => `
    <div class="hist"><span class="hist-when">${fmtTime(h.at)}</span>
      <span><strong>${esc(h.action.replace(/_/g, " "))}</strong>${h.note ? " — " + esc(h.note) : ""}
      <span class="muted small">by ${esc(h.by)}</span></span></div>`).join("");
  return `<tr class="hidden hist-row" id="hist-${esc(id)}"><td colspan="${cols}">
    ${rows || '<span class="muted small">No history recorded.</span>'}</td></tr>`;
}
const historyBtn = id => `<button class="link-btn" data-hist="${esc(id)}">History</button>`;

function wireHistory(bodyId) {
  document.getElementById(bodyId).addEventListener("click", e => {
    const btn = e.target.closest("button[data-hist]");
    if (!btn) return;
    const row = document.getElementById("hist-" + btn.dataset.hist);
    btn.textContent = row.classList.toggle("hidden") ? "History" : "Hide";
  });
}

function rangeNote(el, d) {
  document.getElementById(el).textContent = d.window_hours
    ? `Showing ${d.shown} of ${d.total} — last ${d.window_hours} hours, plus anything still awaiting action.`
    : `Showing all ${d.total} records.`;
}

// ---------- approvals ----------
async function loadApprovals() {
  const status = document.getElementById("approval-filter").value;
  const win = document.getElementById("approval-window").value;
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (win) q.set("window", win);
  const d = await api("/api/admin/approvals?" + q);
  rangeNote("approvals-range", d);
  document.getElementById("approvals-body").innerHTML = d.approvals.length ? d.approvals.map(a => `
    <tr>
      <td><code>${esc(a.request_id)}</code></td>
      <td>${esc(a.student_name)}<div class="muted small">${esc(a.student_id)}</div></td>
      <td>${esc(a.field)}</td>
      <td>${esc(a.old_value)} → <strong>${esc(a.new_value)}</strong></td>
      <td class="small">${fmtTime(a.requested_at)}</td>
      <td>${pillFor(a.status)}${a.decided_by ? `<div class="muted small">by ${esc(a.decided_by)}</div>` : ""}</td>
      <td style="white-space:nowrap">${a.status === "pending" ? `
        <button class="btn btn-sm btn-ok" data-id="${esc(a.request_id)}" data-d="approve">Approve</button>
        <button class="btn btn-sm btn-bad" data-id="${esc(a.request_id)}" data-d="reject">Reject</button>` : ""}
        ${historyBtn(a.request_id)}</td>
    </tr>${historyRow(a.request_id, a.history, 7)}`).join("") : emptyRow(7, "No requests in this view.");
  refreshPendingCount();
}
document.getElementById("approval-window").onchange = loadApprovals;
wireHistory("approvals-body");
document.getElementById("approvals-body").onclick = async e => {
  const b = e.target.closest("button[data-id]");
  if (!b) return;
  if (!confirm(`${b.dataset.d === "approve" ? "Approve" : "Reject"} request ${b.dataset.id}?`)) return;
  try {
    await api(`/api/admin/approvals/${b.dataset.id}/decision`, { method: "POST", body: { decision: b.dataset.d } });
    toast(b.dataset.d === "approve" ? "Approved and applied" : "Rejected — no change made");
  } catch (err) { toast(err.message); }
  };
document.getElementById("approval-filter").onchange = loadApprovals;
async function refreshPendingCount() {
  const n = (await api("/api/admin/approvals?status=pending&window=all")).approvals.length;
  const badge = document.getElementById("pending-count");
  badge.textContent = n; badge.classList.toggle("hidden", n === 0);
}
loaders.approvals = loadApprovals;

// ---------- support tickets ----------
let ticketStatuses = [];

async function loadTickets() {
  const status = document.getElementById("ticket-filter").value;
  const win = document.getElementById("ticket-window").value;
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (win) q.set("window", win);
  const d = await api("/api/admin/tickets?" + q);
  rangeNote("tickets-range", d);

  if (!ticketStatuses.length) {                 // statuses come from app_config.json
    ticketStatuses = d.statuses;
    document.getElementById("ticket-filter").insertAdjacentHTML("beforeend",
      ticketStatuses.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join(""));
  }

  document.getElementById("tickets-body").innerHTML = d.tickets.length ? d.tickets.map(t => `
    <tr>
      <td><code>${esc(t.ticket_id)}</code></td>
      <td>${esc(t.student_name)}<div class="muted small">${esc(t.student_id)}</div></td>
      <td>${esc(t.category)}</td>
      <td>${esc(t.description)}</td>
      <td class="small">${fmtTime(t.created_at)}</td>
      <td>${pillFor(t.status)}${t.updated_by ? `<div class="muted small">by ${esc(t.updated_by)}</div>` : ""}</td>
      <td><select class="input input-sm" data-ticket="${esc(t.ticket_id)}">
        ${ticketStatuses.map(s => `<option${s === t.status ? " selected" : ""}>${esc(s)}</option>`).join("")}
      </select>${historyBtn(t.ticket_id)}</td>
    </tr>${historyRow(t.ticket_id, t.history, 7)}`).join("") : emptyRow(7, "No tickets in this view.");
  refreshOpenCount();
}
document.getElementById("ticket-window").onchange = loadTickets;
wireHistory("tickets-body");

// Staff change a ticket's status straight from the table.
document.getElementById("tickets-body").onchange = async e => {
  const sel = e.target.closest("select[data-ticket]");
  if (!sel) return;
  try {
    await api(`/api/admin/tickets/${sel.dataset.ticket}/status`, { method: "POST", body: { status: sel.value } });
    toast(`${sel.dataset.ticket} set to ${sel.value}`);
  } catch (err) { toast(err.message); }
  loadTickets();
};
document.getElementById("ticket-filter").onchange = loadTickets;
document.getElementById("tickets-refresh").onclick = loadTickets;

async function refreshOpenCount() {
  const first = ticketStatuses[0];
  if (!first) return;
  const n = (await api(`/api/admin/tickets?window=all&status=${encodeURIComponent(first)}`)).tickets.length;
  const badge = document.getElementById("open-count");
  badge.textContent = n; badge.classList.toggle("hidden", n === 0);
}
loaders.tickets = loadTickets;

// ---------- audit ----------
async function loadAudit() {
  const items = await api("/api/admin/audit");
  document.getElementById("audit-body").innerHTML = items.length ? items.map(a => `
    <tr><td class="small">${fmtTime(a.timestamp)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td>
    <td>${esc(a.target_student || "—")}</td><td>${pillFor(a.decision)}</td><td class="small">${esc(a.result)}</td></tr>`).join("")
    : emptyRow(6, "No audit records yet.");
}
document.getElementById("audit-refresh").onclick = loadAudit;
loaders.audit = loadAudit;

// ---------- traces: list of past runs (panel renderer is in common.js) ----------
// A stored trace has the same fields the chat endpoints return, just nested
// differently — reshape it so one renderer serves both.
function publicShape(t) {
  const steps = t.steps || [];
  const requests = steps.flatMap(s =>
    s.type === "model_call"
      ? [{ kind: "model", name: `Model call ${s.n}`, detail: s.requested.join(", "),
           latency_ms: s.latency_ms, input_tokens: s.input_tokens, output_tokens: s.output_tokens, ok: true }]
      : s.type === "tool"
        ? [{ kind: "tool", name: s.name, detail: s.permission === "n/a" ? "" : s.permission,
             latency_ms: s.latency_ms, ok: s.ok }]
        : []);
  return {
    steps, requests,
    sources: t.retrieved_evidence?.policy_chunks || [],
    memory: t.retrieved_evidence?.memory_items || [],
    latency_ms: t.latency_ms, input_tokens: t.input_tokens, output_tokens: t.output_tokens,
    provider: t.provider || "—", model: t.model || "—",
    model_calls: t.model_calls ?? 0, tool_call_count: t.tool_call_count ?? t.tool_calls.length,
    blocked_tool_calls: t.blocked_tool_calls ?? 0, finish_reason: t.finish_reason || "",
  };
}

async function loadTraces() {
  const agent = document.getElementById("trace-agent").value;
  const items = await api(`/api/admin/traces?agent=${agent}`);
  const body = document.getElementById("traces-body");
  body.innerHTML = items.length ? items.map((t, i) => `
    <tr class="clickable" data-i="${i}"><td class="small">${fmtTime(t.timestamp)}</td><td>${esc(t.student_id)}</td>
    <td>${esc(t.question)}${t.error ? ` ${pillFor("error")}` : ""}</td>
    <td>${t.model_calls ?? "—"}</td>
    <td class="small">${esc(t.tool_calls.map(s => s.tool + (s.permission === "blocked" ? " (blocked)" : "")).join(", ") || "—")}</td>
    <td>${(t.latency_ms / 1000).toFixed(1)}s</td><td>${t.input_tokens}/${t.output_tokens}</td></tr>
    <tr class="hidden" id="trace-${i}"><td colspan="7">${renderTracePanel(publicShape(t))}</td></tr>`).join("")
    : emptyRow(7, "No conversations yet.");
  body.onclick = e => {
    const row = e.target.closest("tr.clickable");
    if (row) document.getElementById("trace-" + row.dataset.i).classList.toggle("hidden");
  };
}
document.getElementById("trace-agent").onchange = loadTraces;
loaders.traces = loadTraces;

// ---------- students: roster and account creation ----------
let rosterMeta = null;
const sForm = document.getElementById("student-form");
const sError = document.getElementById("student-error");

async function loadStudents() {
  const d = await api("/api/admin/students");
  if (!rosterMeta) {                            // option lists come from app_config.json
    rosterMeta = d;
    document.getElementById("f-fee-status").innerHTML =
      d.fee_statuses.map(s => `<option>${esc(s)}</option>`).join("");
    document.getElementById("f-sch-status").innerHTML =
      d.scholarship_statuses.map(s => `<option>${esc(s)}</option>`).join("");
    document.getElementById("f-sid").placeholder = d.id_hint.replace(/^.*like /, "e.g. ").replace(".", "");
    document.getElementById("f-pin").placeholder = `${d.min_pin_length}+ digits`;
    document.getElementById("pin-note").textContent =
      "The PIN is hashed before it is stored — note it down, it cannot be read back later.";
  }
  document.getElementById("students-body").innerHTML = d.students.length ? d.students.map(s => `
    <tr>
      <td><code>${esc(s.student_id)}</code></td>
      <td>${esc(s.name)}</td>
      <td>${esc(s.programme)}</td>
      <td>${esc(s.elective)}</td>
      <td class="small">${esc(s.email)}<div class="muted">${esc(s.phone)}</div></td>
      <td>${esc(s.currency)} ${Number(s.outstanding).toLocaleString()}</td>
      <td>${pillFor(s.scholarship_status)}</td>
      <td><button class="link-btn" data-history="${esc(s.student_id)}">History</button></td>
    </tr>`).join("") : emptyRow(8, "No students yet.");
}
loaders.students = loadStudents;

document.getElementById("new-student").onclick = () => {
  sForm.classList.toggle("hidden");
  sError.classList.add("hidden");
  if (!sForm.classList.contains("hidden")) document.getElementById("f-sid").focus();
};
document.getElementById("cancel-student").onclick = () => { sForm.classList.add("hidden"); sForm.reset(); };

sForm.onsubmit = async e => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(sForm).entries());
  const btn = sForm.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const created = await api("/api/admin/students", { method: "POST", body });
    toast(`${created.student_id} created — they can sign in now`);
    sForm.reset(); sForm.classList.add("hidden"); sError.classList.add("hidden");
    loadStudents();
  } catch (err) {
    sError.textContent = err.message;
    sError.classList.remove("hidden");
  }
  btn.disabled = false;
};

// One student's complete trail, with no time window applied.
document.getElementById("students-body").addEventListener("click", async e => {
  const btn = e.target.closest("button[data-history]");
  if (!btn) return;
  const h = await api(`/api/admin/students/${btn.dataset.history}/history`);
  toast(`${h.name}: ${h.requests.length} requests · ${h.tickets.length} tickets · ${h.audit_events.length} audit events`);
});

// ---------- staff assistant ----------
let sConv = "s-" + Date.now();
const sLog = document.getElementById("s-log");
function sAdd(cls, html) {
  document.getElementById("s-welcome")?.remove();
  sLog.insertAdjacentHTML("beforeend", `<div class="msg ${cls}"><div class="bubble">${html}</div></div>`);
  sLog.scrollTop = sLog.scrollHeight;
  return sLog.lastElementChild;
}
async function staffAsk(text) {
  text = text.trim();
  if (!text) return;
  sAdd("user", esc(text));
  document.getElementById("s-msg").value = "";
  const typing = sAdd("bot", `<span class="typing"><span></span><span></span><span></span></span>`);
  try {
    const d = await api("/api/admin/assistant", { method: "POST", body: { message: text, conversation_id: sConv } });
    typing.remove();
    const used = [...new Set(d.activity.tools)].join(", ") || "no tools";
    sAdd("bot", renderMarkdown(d.answer)
      + `<div class="activity">${esc(used)} · ${(d.activity.latency_ms / 1000).toFixed(1)}s</div>`
      + traceToggle(d.activity));
  } catch (e) { typing.remove(); sAdd("error", esc(e.message)); }
}
document.getElementById("s-form").onsubmit = e => { e.preventDefault(); staffAsk(document.getElementById("s-msg").value); };
document.querySelectorAll("#s-log .chip").forEach(c => c.onclick = () => staffAsk(c.textContent));
document.getElementById("s-new").onclick = () => { sConv = "s-" + Date.now(); sLog.innerHTML = '<div class="muted small">New conversation started.</div>'; };

// Badges on page load, so staff see waiting work without opening each tab.
refreshPendingCount().catch(e => console.error("pending count", e));
api("/api/admin/tickets?window=all").then(d => {
  ticketStatuses = d.statuses;
  document.getElementById("ticket-filter").insertAdjacentHTML("beforeend",
    ticketStatuses.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join(""));
  const n = d.tickets.filter(t => t.status === ticketStatuses[0]).length;
  const badge = document.getElementById("open-count");
  badge.textContent = n; badge.classList.toggle("hidden", n === 0);
}).catch(e => console.error("ticket count", e));
