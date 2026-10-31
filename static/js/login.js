// Login page: switches between Student and Staff forms and calls the auth API.
const studentForm = document.getElementById("student-form");
const staffForm = document.getElementById("staff-form");
const errorBox = document.getElementById("login-error");

function showTab(isStudent) {
  document.getElementById("tab-student").classList.toggle("active", isStudent);
  document.getElementById("tab-staff").classList.toggle("active", !isStudent);
  studentForm.classList.toggle("hidden", !isStudent);
  staffForm.classList.toggle("hidden", isStudent);
  errorBox.classList.add("hidden");
}
document.getElementById("tab-student").onclick = () => showTab(true);
document.getElementById("tab-staff").onclick = () => showTab(false);

async function submit(form, url) {
  const btn = form.querySelector("button");
  btn.disabled = true;
  errorBox.classList.add("hidden");
  try {
    const body = Object.fromEntries(new FormData(form));
    const d = await api(url, { method: "POST", body, allow401: true });
    window.location.href = d.redirect;
  } catch (e) {
    errorBox.textContent = e.message;
    errorBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
  }
}
studentForm.onsubmit = e => { e.preventDefault(); submit(studentForm, "/api/auth/student-login"); };
staffForm.onsubmit = e => { e.preventDefault(); submit(staffForm, "/api/auth/admin-login"); };
