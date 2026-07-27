// LAZEIMS Station shell — vanilla JS, fully local (no CDN).
const $ = (id) => document.getElementById(id);
const api = (url, opts) => fetch(url, { credentials: "same-origin", ...opts });
const jpost = (url, body) =>
  api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const jput = (url, body) =>
  api(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

let SESSION = null;
let CURRENT = null; // {centre_number, subject_code, paper_type}

// ---- IndexedDB draft store (survives refresh / crash) ----
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open("lazeims_station", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("drafts", { keyPath: "k" });
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function draftSet(k, v) {
  const db = await idb();
  return new Promise((res) => { const t = db.transaction("drafts", "readwrite"); t.objectStore("drafts").put({ k, v }); t.oncomplete = res; });
}
async function draftGet(k) {
  const db = await idb();
  return new Promise((res) => { const t = db.transaction("drafts", "readonly"); const rq = t.objectStore("drafts").get(k); rq.onsuccess = () => res(rq.result ? rq.result.v : null); });
}
async function draftDel(k) {
  const db = await idb();
  return new Promise((res) => { const t = db.transaction("drafts", "readwrite"); t.objectStore("drafts").delete(k); t.oncomplete = res; });
}
const dkey = (sid) => `${CURRENT.centre_number}|${CURRENT.subject_code}|${CURRENT.paper_type}|${sid}`;

// ---- boot ----
async function boot() {
  const s = await (await api("/api/status")).json();
  $("status").innerHTML = s.station_code
    ? `<strong>${s.station_code}</strong> <span class="muted">· exam #${s.exam_id} · ${s.students} students · v${s.software_version}</span>`
    : `<span class="muted">No package imported yet.</span>`;
  const me = await api("/api/me");
  if (me.ok) { SESSION = await me.json(); afterLogin(); }
  else { show("login"); }
}
function show(...ids) {
  for (const el of ["login", "scopes", "entry", "progress"]) $(el).hidden = !ids.includes(el);
}
function afterLogin() {
  $("who").textContent = SESSION ? `${SESSION.role}` : "";
  show("scopes", "progress");
  loadScopes();
  loadProgress();
}

$("de-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("de-msg").textContent = "Signing in…";
  const r = await jpost("/api/login/de", { pin: f.pin.value, initials: f.initials.value });
  if (r.ok) { SESSION = await r.json(); afterLogin(); }
  else $("de-msg").textContent = "Invalid PIN or initials.";
});

async function loadScopes() {
  const scopes = await (await api("/api/scopes")).json();
  $("scope-list").innerHTML = scopes.map((s, i) =>
    `<div class="scope-row">
       <span>${s.centre_number} · ${s.subject_code} · ${s.paper_type}</span>
       <span class="muted">${s.finalized ? "FINALIZED" : (s.lock_status || "open")}</span>
       <button data-i="${i}" ${s.finalized ? "disabled" : ""}>Enter</button>
     </div>`).join("") || "<p class='muted'>No scopes.</p>";
  $("scope-list").querySelectorAll("button[data-i]").forEach((b) =>
    b.addEventListener("click", () => enterScope(scopes[+b.dataset.i])));
}

async function enterScope(scope) {
  const lock = await jpost("/api/locks/acquire", scope);
  if (!lock.ok) { const e = await lock.json(); alert("Cannot lock: " + (e.detail?.message || "")); return; }
  CURRENT = scope;
  $("entry-title").textContent = `${scope.centre_number} · ${scope.subject_code} · ${scope.paper_type}`;
  show("entry", "progress");
  await renderGrid();
}

$("back").addEventListener("click", async () => {
  if (CURRENT) await jpost("/api/locks/release", CURRENT);
  CURRENT = null; show("scopes", "progress"); loadScopes();
});

async function renderGrid() {
  const q = new URLSearchParams({ subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, centre_number: CURRENT.centre_number });
  const roster = await (await api("/api/roster?" + q)).json();
  const rows = [];
  for (const st of roster) {
    const draft = await draftGet(dkey(st.student_id));
    const present = draft ? draft.present : (st.attendance === null ? true : st.attendance);
    const total = draft ? draft.total : "";
    rows.push(`<tr data-sid="${st.student_id}">
      <td class="sid">${st.student_id}</td>
      <td>${st.first_name} ${st.surname}</td>
      <td><input type="checkbox" class="present" ${present ? "checked" : ""} aria-label="present"></td>
      <td><input class="total" inputmode="numeric" value="${total}" aria-label="total marks"></td>
      <td><button class="save">Save</button> <span class="cell-msg muted"></span></td>
    </tr>`);
  }
  $("grid-wrap").innerHTML = `<table class="grid"><thead><tr><th>ID</th><th>Name</th><th>Present</th><th>Total</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
  wireGrid();
}

function wireGrid() {
  const tbody = $("grid-wrap").querySelector("tbody");
  tbody.querySelectorAll("tr").forEach((tr) => {
    const sid = tr.dataset.sid;
    const present = tr.querySelector(".present");
    const total = tr.querySelector(".total");
    const msg = tr.querySelector(".cell-msg");
    const saveDraft = () => draftSet(dkey(sid), { present: present.checked, total: total.value });
    present.addEventListener("change", () => { total.disabled = !present.checked; saveDraft(); });
    total.disabled = !present.checked;
    total.addEventListener("input", saveDraft);
    // keyboard: Enter saves + moves to next row's total
    total.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); tr.querySelector(".save").click(); }
    });
    tr.querySelector(".save").addEventListener("click", () => saveStudent(sid, present, total, msg));
  });
}

async function saveStudent(sid, present, total, msg) {
  msg.textContent = "Saving…";
  // 1) attendance
  const att = await jput("/api/attendance", {
    student_id: sid, subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type,
    is_present: present.checked, source: "INVIGILATOR_ISAL_TRANSCRIPTION",
  });
  if (!att.ok) { msg.textContent = (await att.json()).error?.message || "attendance error"; return; }
  // 2) marks (no-blank enforced client + server)
  if (present.checked && total.value.trim() === "") { msg.textContent = "Enter a mark (0 is valid)."; return; }
  const body = {
    subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, mode: "TOTAL_MARKS",
    total_marks_obtained: present.checked ? Number(total.value) : null,
  };
  const r = await jput(`/api/marks/students/${encodeURIComponent(sid)}`, body);
  if (r.ok) {
    await draftDel(dkey(sid)); // clear draft only after confirmed commit
    msg.textContent = "Saved ✓"; loadProgress();
  } else {
    msg.textContent = (await r.json()).error?.message || "save error";
  }
}

$("finalize").addEventListener("click", async () => {
  const r = await jpost("/api/scopes/finalize", CURRENT);
  if (r.ok) { $("entry-msg").textContent = "Scope finalized ✓"; loadProgress(); }
  else {
    const d = await r.json();
    const blockers = (d.detail?.result?.blockers || []).map((b) => b.message).join("; ");
    $("entry-msg").textContent = "Cannot finalize: " + (blockers || "incomplete");
  }
});

async function loadProgress() {
  const p = await (await api("/api/progress")).json();
  $("progress-body").textContent =
    `Pending sync events: ${p.pending_events} · Rejected: ${p.rejected_events} · Finalized scopes: ${p.finalized_scopes} · Totals entered: ${p.total_marks}`;
}

boot();
