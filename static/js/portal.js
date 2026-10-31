// Student portal: profile, fee, scholarship, requests, tickets, saved details
// and the chat with the support assistant.
const chat = document.getElementById("chat");
const scroller = document.getElementById("chat-scroll");
const input = document.getElementById("message");
const sendBtn = document.getElementById("send");
let conversationId = "c-" + Date.now();   // new id = new session (long-term memory still applies)

// Filled from the API on first load — never hard-coded in the page.
let profileChangeable = [];               // which fields a student may ask to change
let ticketSettledStatus = "";             // the last configured ticket status, e.g. "Resolved"

// ---------- small formatters ----------
// Currency comes from the record, so nothing is hard coded to one country.
function money(amount, currency) {
  const n = Number(amount) || 0;
  if (!currency) return n.toLocaleString();
  try {
    return n.toLocaleString(undefined, { style: "currency", currency, maximumFractionDigits: 0 });
  } catch (_) { return `${currency} ${n.toLocaleString()}`; }
}

// Whole days from today to an ISO date (negative = in the past).
function daysUntil(isoDate) {
  if (!isoDate) return null;
  const due = new Date(isoDate + "T00:00:00");
  if (isNaN(due)) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((due - today) / 86400000);
}

function dueBadge(fee) {
  if (!Number(fee.outstanding)) return `<span class="pill pill-ok">Fully paid</span>`;
  const d = daysUntil(fee.due_date);
  if (d === null) return pillFor(fee.status);
  if (d < 0) return `<span class="pill pill-bad">Overdue by ${-d} day${-d === 1 ? "" : "s"}</span>`;
  if (d === 0) return `<span class="pill pill-bad">Due today</span>`;
  return `<span class="pill ${d <= 7 ? "pill-bad" : d <= 30 ? "pill-warn" : "pill-neutral"}">Due in ${d} days</span>`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso + "T00:00:00");
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
}

// ---------- sidebar: profile, fee, scholarship ----------
async function loadProfile() {
  const p = await api("/api/student/profile");
  profileChangeable = p.changeable_fields;
  document.getElementById("who").textContent = `${p.name} · ${p.student_id}`;
  // The greeting lives in the welcome block, which is removed on the first
  // message — later refreshes must not assume it is still there.
  const greeting = document.getElementById("greeting");
  if (greeting) greeting.textContent = `Hi ${p.name.split(" ")[0]}, how can we help today?`;

  document.getElementById("profile").innerHTML = `
    <div class="kv"><span>Name</span><span>${esc(p.name)}</span></div>
    <div class="kv"><span>Student ID</span><span>${esc(p.student_id)}</span></div>
    <div class="kv"><span>Programme</span><span>${esc(p.programme)}</span></div>
    <div class="kv"><span>Elective</span><span>${esc(p.elective)}</span></div>
    <div class="kv"><span>Email</span><span class="small">${esc(p.email)}</span></div>
    <div class="kv"><span>Phone</span><span class="small">${esc(p.phone)}</span></div>
    <p class="muted small mt8">To correct any of these, just ask me — ${esc(p.changeable_fields.join(", "))}
      can be changed after staff approval.</p>`;

  const f = p.fee;
  document.getElementById("fee").innerHTML = `
    <div class="amount">${esc(money(f.outstanding, f.currency))}<span class="muted small"> outstanding</span></div>
    <div class="mt8">${dueBadge(f)}</div>
    <div class="meter mt8" title="${f.paid_pct}% paid"><div class="meter-fill" style="width:${f.paid_pct}%"></div></div>
    <div class="kv mt8"><span>Paid</span><span>${esc(money(f.paid, f.currency))} (${f.paid_pct}%)</span></div>
    <div class="kv"><span>Total fee</span><span>${esc(money(f.total_fee, f.currency))}</span></div>
    <div class="kv"><span>Due date</span><span>${esc(fmtDate(f.due_date))}</span></div>
    <div class="kv"><span>Status</span><span>${esc(f.status)}</span></div>`;

  const s = p.scholarship;
  document.getElementById("scholarship").innerHTML = `
    <div class="kv"><strong>${esc(s.name || "None")}</strong>${pillFor(s.status)}</div>
    ${s.amount ? `<div class="kv"><span>Amount</span><span>${esc(money(s.amount, f.currency))}</span></div>` : ""}`;

  renderGlance(p);
  return p;
}

// Three tiles on the empty welcome screen, so the portal says something
// useful before the student types anything.
function renderGlance(p) {
  const box = document.getElementById("glance");
  if (!box) return;                                  // welcome screen already dismissed
  const f = p.fee;
  box.innerHTML = `
    <div class="mini-stat"><div class="label">Outstanding fee</div>
      <div class="value">${esc(money(f.outstanding, f.currency))}</div>
      <div class="sub">${Number(f.outstanding) ? "Due " + esc(fmtDate(f.due_date)) : "Nothing to pay"}</div></div>
    <div class="mini-stat"><div class="label">Scholarship</div>
      <div class="value sm">${esc(p.scholarship.status || "—")}</div>
      <div class="sub">${esc(p.scholarship.name || "Not applied")}</div></div>
    <div class="mini-stat"><div class="label">Open with the office</div>
      <div class="value" id="glance-open">—</div>
      <div class="sub">requests &amp; tickets</div></div>`;
  updateOpenCount();
}

let openCounts = { requests: 0, tickets: 0 };
function updateOpenCount() {
  const el = document.getElementById("glance-open");
  if (el) el.textContent = openCounts.requests + openCounts.tickets;
}

// ---------- collapsible groups: newest item visible, rest behind "View all" ----------
// Groups are built from whatever the API returns, so new statuses need no code change.
function groupBlock(title, items, renderItem, groupKey) {
  if (!items.length) return "";
  const [newest, ...rest] = items;                  // API returns newest first
  return `
    <div class="group">
      <div class="group-head"><span class="group-title">${esc(title)}</span>
        <span class="muted small">${items.length}</span></div>
      ${renderItem(newest)}
      ${rest.length ? `
        <button class="link-btn" data-group="${esc(groupKey)}">View all ${items.length}</button>
        <div class="group-rest hidden" id="rest-${esc(groupKey)}">${rest.map(renderItem).join("")}</div>` : ""}
    </div>`;
}

// One delegated handler for every "View all" toggle in the sidebar.
document.querySelector(".sidebar").addEventListener("click", e => {
  const btn = e.target.closest("button[data-group]");
  if (!btn) return;
  const rest = document.getElementById("rest-" + btn.dataset.group);
  const open = rest.classList.toggle("hidden");
  btn.textContent = open ? `View all ${rest.children.length + 1}` : "Show less";
});

// Splits a list into "still waiting" and "already decided" without hard-coding
// status names: the open status is whatever the record says is not yet settled.
function splitByOpen(items, isOpen) {
  return [items.filter(isOpen), items.filter(i => !isOpen(i))];
}

// ---------- sidebar: change requests ----------
function requestItem(r) {
  return `
    <div class="req">
      <div class="kv"><strong>${esc(r.field)} change</strong>${pillFor(r.status)}</div>
      <div class="muted small">${esc(r.old_value)} → ${esc(r.new_value)}</div>
      <div class="muted small">${esc(r.request_id)} · ${fmtTime(r.requested_at)}</div>
      ${r.status === "rejected" ? `<div class="muted small">Not applied. Ask me to raise it again if needed.</div>` : ""}
    </div>`;
}

let lastRequestSignature = "";
async function loadRequests() {
  const items = await api("/api/student/requests");
  openCounts.requests = items.filter(r => r.status === "pending").length;
  updateOpenCount();

  // If a decision landed since the last poll, the record may have changed too.
  const signature = items.map(r => r.request_id + r.status).join(",");
  if (lastRequestSignature && signature !== lastRequestSignature) loadProfile().catch(() => {});
  lastRequestSignature = signature;

  const [pending, decided] = splitByOpen(items, r => r.status === "pending");
  document.getElementById("requests").innerHTML =
    groupBlock("Waiting for approval", pending, requestItem, "req-pending") +
    groupBlock("Decided", decided, requestItem, "req-decided") ||
    `<span class="muted small">No requests yet. Ask me to update your ${profileChangeable.join(", ")}.</span>`;
}

// ---------- sidebar: support tickets ----------
function ticketItem(t) {
  return `
    <div class="req">
      <div class="kv"><strong>${esc(t.ticket_id)}</strong>${pillFor(t.status)}</div>
      <div class="muted small">${esc(t.category)} · ${fmtTime(t.created_at)}</div>
      <div class="small">${esc(t.description)}</div>
      ${t.updated_by ? `<div class="muted small">Updated by ${esc(t.updated_by)} · ${fmtTime(t.updated_at)}</div>` : ""}
    </div>`;
}

async function loadTickets() {
  const d = await api("/api/student/tickets");
  const items = d.tickets;
  // "Settled" = the last configured status (e.g. Resolved); everything else is still live.
  ticketSettledStatus = d.statuses[d.statuses.length - 1];
  const settled = ticketSettledStatus;
  openCounts.tickets = items.filter(t => t.status !== settled).length;
  updateOpenCount();

  const [live, done] = splitByOpen(items, t => t.status !== settled);
  document.getElementById("tickets").innerHTML =
    groupBlock("Still open", live, ticketItem, "tkt-open") +
    groupBlock(settled, done, ticketItem, "tkt-done") ||
    `<span class="muted small">No tickets yet. Tell me a problem and I'll raise one.</span>`;
}

// ---------- sidebar: messages from the support office ----------
function notificationItem(n) {
  return `
    <div class="req ${n.read_at ? "" : "unread"}">
      <div class="kv"><strong>${esc(n.subject || "Message")}</strong>
        ${n.read_at ? "" : `<span class="pill pill-warn">new</span>`}</div>
      <div class="small">${esc(n.message)}</div>
      <div class="muted small">${esc(n.sent_by)} · ${fmtTime(n.sent_at)}</div>
      ${n.read_at ? "" : `<button class="link-btn" data-read="${esc(n.notification_id)}">Mark as read</button>`}
    </div>`;
}

async function loadNotifications() {
  const d = await api("/api/student/notifications");
  const card = document.getElementById("notif-card");
  card.classList.toggle("hidden", d.notifications.length === 0);
  const badge = document.getElementById("notif-unread");
  badge.textContent = d.unread;
  badge.classList.toggle("hidden", d.unread === 0);
  const [unread, read] = splitByOpen(d.notifications, n => !n.read_at);
  document.getElementById("notifications").innerHTML =
    groupBlock("Unread", unread, notificationItem, "ntf-unread") +
    groupBlock("Earlier", read, notificationItem, "ntf-read");
}

document.getElementById("notifications").addEventListener("click", async e => {
  const btn = e.target.closest("button[data-read]");
  if (!btn) return;
  try {
    await api(`/api/student/notifications/${btn.dataset.read}/read`, { method: "POST" });
    loadNotifications();
  } catch (err) { toast(err.message); }
});

// ---------- chat ----------
function addMessage(role, html) {
  document.getElementById("welcome")?.remove();
  const row = document.createElement("div");
  row.className = `msg ${role}`;
  row.innerHTML = `<div class="bubble">${html}</div>`;
  chat.appendChild(row);
  scroller.scrollTop = scroller.scrollHeight;
  return row;
}

function activityLine(a) {
  const parts = [...new Set(a.tools.map(t => TOOL_LABELS[t] || t))];
  if (a.sources.length) parts.push("Source: " + a.sources.map(s => s.split(":").slice(1).join(":")).join(", "));
  parts.push((a.latency_ms / 1000).toFixed(1) + "s");
  return `<div class="activity">${esc(parts.join(" · "))}</div>`;
}

async function send(text) {
  text = text.trim();
  if (!text) return;
  addMessage("user", esc(text));
  input.value = "";
  sendBtn.disabled = true;
  const typing = addMessage("bot", `<span class="typing"><span></span><span></span><span></span></span>`);
  try {
    const d = await api("/api/student/chat", { method: "POST", body: { message: text, conversation_id: conversationId } });
    typing.remove();
    // Every reply carries its own pipeline trace, openable from the chat.
    addMessage("bot", renderMarkdown(d.answer) + activityLine(d.activity) + traceToggle(d.activity));
    // Refresh only the panel the agent actually touched.
    const used = d.activity.tools;
    // Refresh whatever the agent just touched, so the sidebar and the chat never disagree.
    if (used.includes("request_record_change") || used.includes("list_my_change_requests")) loadRequests();
    if (used.includes("create_support_request") || used.includes("list_my_support_tickets")) loadTickets();
    if (used.includes("list_my_notifications")) loadNotifications();
    if (used.includes("get_student_profile") || used.includes("get_fee_status")
        || used.includes("get_scholarship_status")) loadProfile();
  } catch (e) {
    typing.remove();
    addMessage("error", esc(e.message));
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

document.getElementById("composer").onsubmit = e => { e.preventDefault(); send(input.value); };
input.addEventListener("keydown", e => {             // Enter sends, Shift+Enter = new line
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
});
document.querySelectorAll(".chip").forEach(c => c.onclick = () => send(c.textContent));
document.getElementById("new-chat").onclick = () => { conversationId = "c-" + Date.now(); window.location.reload(); };

// A failure in one panel must not stop the others loading.
function refreshAll() {
  loadProfile().catch(e => console.error("profile", e));
  loadRequests().catch(e => console.error("requests", e));
  loadTickets().catch(e => console.error("tickets", e));
  loadNotifications().catch(e => console.error("notifications", e));
}
refreshAll();
setInterval(() => {                                  // pick up staff decisions and new messages
  loadRequests().catch(() => {});
  loadProfile().catch(() => {});                     // an approved change updates the profile
  loadTickets().catch(() => {});
  loadNotifications().catch(() => {});
}, 20000);
