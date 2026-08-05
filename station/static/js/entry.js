// entry.js — attendance tab + marks tab (total + CAL)
import { $, api, jpost, jput, esc, fmt, isValidMark, setMsg } from './api.js';
import { SESSION, CURRENT, ROSTER, ATT, ATT_PERSISTED, ATT_SAVING, MARKS, DEBOUNCE_T, SCOPES, setState } from './state.js';
import { showView } from './router.js?v=2';
import { dbGet, dbSet, dbDel, draftKey } from './idb.js';
import { loadPortal } from './portal.js';

export async function enterScope(i) {
  const scope = SCOPES[i];
  const lr = await jpost('/api/locks/acquire', scope);
  if (!lr.ok) { const e = await lr.json().catch(() => ({})); alert('Cannot enter scope: ' + (e.detail?.message || 'locked')); return; }
  setState.current({ ...scope });
  $('entry-title').textContent = `${scope.centre_number} · ${scope.school_name || ''} · ${scope.subject_name || scope.subject_code} · ${scope.paper_type}`;
  $('entry-sub').textContent = 'Loading roster…';
  switchEntryTab('attendance');
  showView('entry');
  await loadRoster();
}
window.enterScope = enterScope;

async function loadRoster() {
  const q = new URLSearchParams({ subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, centre_number: CURRENT.centre_number });
  try { setState.roster(await (await api('/api/roster?' + q)).json()); } catch (e) { setState.roster([]); }
  $('entry-sub').textContent = `${ROSTER.length} students`;
  setState.att(Object.fromEntries(ROSTER.map(s => [s.student_id, s.attendance !== null ? s.attendance : true])));
  setState.attPersisted(Object.fromEntries(ROSTER.map(s => [s.student_id, s.attendance !== null])));
  setState.attSaving({});
  setState.marks(Object.fromEntries(ROSTER.map(s => [s.student_id, { value: '', status: 'idle' }])));
  const drafts = await dbGet('marks', draftKey()) || {};
  Object.entries(drafts).forEach(([id, v]) => { if (MARKS[id]) MARKS[id] = { value: v, status: 'dirty' }; });
  renderAttTable();
  updateEntryBar();
}

function switchEntryTab(tab) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $('tab-attendance').hidden = tab !== 'attendance';
  $('tab-marks').hidden = tab !== 'marks';
  if (tab === 'marks') renderMarksTable(); else renderAttTable();
}

// ── Attendance ──
function renderAttTable() {
  if (!ROSTER.length) { $('att-tbody').innerHTML = '<tr><td colspan="4" class="px-4 py-8 text-center text-sm text-gray-500 dark:text-gray-400">No students registered for this subject at this school.</td></tr>'; updateAttSummary(); return; }
  $('att-tbody').innerHTML = ROSTER.map((s, i) => {
    const p = ATT[s.student_id] !== false;
    return `<tr class="att-row ${!p ? 'row-absent' : ''}" data-i="${i}" tabindex="0" onkeydown="attKey(event,${i},'${esc(s.student_id)}')">
      <td class="col-n">${i + 1}</td><td class="col-id">${esc(s.student_id)}</td>
      <td>${esc(s.full_name)}</td>
      <td class="col-att"><button class="att-toggle ${p ? 'att-p' : 'att-a'}" onclick="attToggle('${esc(s.student_id)}',${i})" aria-label="${p ? 'Present' : 'Absent'}"><span class="att-lp">P</span><span class="att-knob"></span><span class="att-la">A</span></button>${ATT_SAVING[s.student_id] ? '<span class="saving-dot"></span>' : ''}</td>
    </tr>`;
  }).join('');
  updateAttSummary();
}

window.attToggle = async function (sid, idx) {
  const prev = ATT[sid];
  ATT[sid] = (ATT[sid] === false); ATT_SAVING[sid] = true;
  // A toggle always supersedes any earlier in-flight PUT for this student
  // (e.g. from "Mark all present"); mark it not-yet-persisted so a stale
  // out-of-order response from that earlier call cannot resurrect it.
  ATT_PERSISTED[sid] = false;
  renderAttTable(); focusAttRow(idx);
  const r = await jput('/api/attendance', { student_id: sid, subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, is_present: ATT[sid] !== false, source: 'INVIGILATOR_ISAL_TRANSCRIPTION' });
  if (r.ok) { ATT_PERSISTED[sid] = true; }
  else {
    // Revert on error
    ATT[sid] = prev;
    ATT_PERSISTED[sid] = false;
    const err = await r.json().catch(() => ({}));
    const msg = err.detail?.message || err.detail || 'Failed to update attendance';
    setMsg('entry-msg', msg, true);
  }
  ATT_SAVING[sid] = false; renderAttTable(); focusAttRow(idx); updateEntryBar();
};
window.attKey = function (e, idx, sid) {
  if (e.key === 'p' || e.key === 'P') { e.preventDefault(); if (ATT[sid] === false) window.attToggle(sid, idx); else focusAttRow(Math.min(idx + 1, ROSTER.length - 1)); }
  else if (e.key === 'a' || e.key === 'A') { e.preventDefault(); if (ATT[sid] !== false) window.attToggle(sid, idx); else focusAttRow(Math.min(idx + 1, ROSTER.length - 1)); }
  else if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); window.attToggle(sid, idx); }
  else if (e.key === 'ArrowDown' || e.key === 'PageDown') { e.preventDefault(); focusAttRow(Math.min(idx + 1, ROSTER.length - 1)); }
  else if (e.key === 'ArrowUp' || e.key === 'PageUp') { e.preventDefault(); focusAttRow(Math.max(idx - 1, 0)); }
};
function focusAttRow(i) { const rows = $('att-tbody')?.querySelectorAll('tr'); if (rows?.[i]) rows[i].focus(); }
function updateAttSummary() {
  const p = ROSTER.filter(s => ATT[s.student_id] !== false).length;
  const ab = ROSTER.length - p;
  const el = $('att-summary'); if (el) el.textContent = `${p} / ${ROSTER.length} present`;
  const hint = $('att-absent-hint'); if (hint) hint.innerHTML = ab > 0 ? `<span class="absent-warn">${ab} absent</span>` : '';
}

// ── Marks ──
function markSt(st, inv) {
  if (inv) return '<span class="st-err">Invalid</span>';
  if (st === 'saving') return '<span class="st-saving">Saving…</span>';
  if (st === 'saved') return '<span class="st-saved">✓</span>';
  if (st === 'dirty') return '<span class="st-dirty">Unsaved</span>';
  if (st === 'error') return '<span class="st-err">Failed</span>';
  return '';
}

function renderMarksTable() {
  if (!ROSTER.length) { $('marks-tbody').innerHTML = '<tr><td colspan="6" class="td-empty">No students.</td></tr>'; return; }
  $('marks-tbody').innerHTML = ROSTER.map((s, i) => {
    const p = ATT[s.student_id] !== false;
    const cell = MARKS[s.student_id] || { value: '', status: 'idle' };
    const inv = !isValidMark(cell.value) && cell.value !== '';
    return `<tr class="${!p ? 'row-absent' : ''}">
      <td class="col-n">${i + 1}</td><td class="col-id">${esc(s.student_id)}</td>
      <td>${esc(s.full_name)}</td>
      <td class="col-att"><span class="badge ${p ? 'badge-open' : 'badge-locked'}">${p ? 'P' : 'A'}</span></td>
      <td class="col-marks">${p ? `<div class="marks-cell"><input class="marks-inp${inv ? ' bad' : ''}" type="text" inputmode="decimal" value="${esc(cell.value)}" data-sid="${esc(s.student_id)}" placeholder="marks" oninput="marksChange('${esc(s.student_id)}',this.value)" onblur="flushMarks()" onkeydown="marksKey(event,'${esc(s.student_id)}',${i})"/></div>` : '<span class="muted">—</span>'}</td>
      <td id="mst-${esc(s.student_id)}">${markSt(cell.status, inv)}</td>
    </tr>`;
  }).join('');
  updateMarksSummary();
  updateEntryBar();
}

window.marksChange = function (sid, val) {
  MARKS[sid] = { value: val, status: 'dirty' };
  const el = $('mst-' + sid); if (el) el.innerHTML = markSt('dirty', !isValidMark(val) && val !== '');
  persistDrafts();
  if (DEBOUNCE_T) clearTimeout(DEBOUNCE_T);
  setState.debounceT(setTimeout(flushMarks, 800));
  updateMarksSummary();
};
window.marksKey = function (e, sid, idx) {
  if (e.key === 'Enter' || e.key === 'PageDown') {
    e.preventDefault(); flushMarks();
    for (let j = idx + 1; j < ROSTER.length; j++) {
      if (ATT[ROSTER[j].student_id] !== false) { const inp = $('marks-tbody')?.querySelector(`input[data-sid="${ROSTER[j].student_id}"]`); if (inp) { inp.focus(); inp.select(); break; } }
    }
  } else if (e.key === 'PageUp') {
    e.preventDefault();
    for (let j = idx - 1; j >= 0; j--) {
      if (ATT[ROSTER[j].student_id] !== false) { const inp = $('marks-tbody')?.querySelector(`input[data-sid="${ROSTER[j].student_id}"]`); if (inp) { inp.focus(); inp.select(); break; } }
    }
  }
};

function updateMarksSummary() {
  const p = ROSTER.filter(s => ATT[s.student_id] !== false).length;
  const e = Object.values(MARKS).filter(c => c.value.trim() !== '').length;
  const el = $('marks-summary'); if (el) el.textContent = `${e} / ${p} marks entered`;
}

async function persistDrafts() {
  const dirty = Object.fromEntries(Object.entries(MARKS).filter(([, c]) => c.status === 'dirty' && c.value.trim() !== '').map(([id, c]) => [id, c.value]));
  if (Object.keys(dirty).length) await dbSet('marks', draftKey(), dirty);
  else await dbDel('marks', draftKey());
}

async function flushMarks() {
  if (!CURRENT) return;
  const pending = Object.entries(MARKS).filter(([id, c]) => c.status === 'dirty' && c.value.trim() !== '' && isValidMark(c.value) && ATT[id] !== false);
  if (!pending.length) return;
  for (const [sid, cell] of pending) {
    MARKS[sid] = { ...cell, status: 'saving' };
    const el = $('mst-' + sid); if (el) el.innerHTML = markSt('saving', false);
    if (!ATT_PERSISTED[sid]) {
      const ar = await jput('/api/attendance', { student_id: sid, subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, is_present: true, source: 'INVIGILATOR_ISAL_TRANSCRIPTION' });
      if (ar.ok) ATT_PERSISTED[sid] = true;
    }
    const r = await jput(`/api/marks/students?student_id=${encodeURIComponent(sid)}`, { subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, mode: 'TOTAL_MARKS', total_marks_obtained: Number(cell.value) });
    if (r.ok) { MARKS[sid] = { value: cell.value, status: 'saved' }; }
    else { MARKS[sid] = { value: cell.value, status: 'error' }; }
    const el2 = $('mst-' + sid); if (el2) el2.innerHTML = markSt(MARKS[sid].status, false);
  }
  await persistDrafts();
  updateMarksSummary();
  updateEntryBar();
}
window.flushMarks = flushMarks;

function updateEntryBar() {
  const present = ROSTER.filter(s => ATT[s.student_id] !== false).length;
  const entered = Object.values(MARKS).filter(c => c.value.trim() !== '').length;
  const errors = Object.values(MARKS).filter(c => c.status === 'error').length;
  const dirty = Object.values(MARKS).filter(c => c.status === 'dirty').length;
  let sv = '';
  if (errors) sv = `<span class="st-err">${errors} failed</span>`;
  else if (dirty) sv = `<span class="st-dirty">${dirty} unsaved</span>`;
  else if (entered) sv = '<span class="st-saved">✓ All saved</span>';
  const bar = $('entry-statusbar');
  if (bar) bar.innerHTML = `<span>Present: <strong>${present}</strong>/${ROSTER.length}</span><span>Marks: <strong>${entered}</strong>/${present}</span>${sv ? `<span>${sv}</span>` : ''}`;
}

export function initEntry() {
  $('entry-back')?.addEventListener('click', async () => {
    if (CURRENT) await jpost('/api/locks/release', CURRENT).catch(() => { });
    setState.current(null); setState.roster([]);
    await loadPortal(); showView('entry-portal');
  });
  document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => switchEntryTab(b.dataset.tab)));
  $('mark-all-present')?.addEventListener('click', async () => {
    ROSTER.forEach(s => { ATT[s.student_id] = true; ATT_SAVING[s.student_id] = true; }); renderAttTable();
    updateEntryBar();
    // Await every PUT (allSettled) instead of firing-and-forgetting. Fire-
    // and-forget let a later individual toggle-to-absent race against a
    // still-in-flight "mark present" call for the SAME student — whichever
    // response landed last silently decided the server's truth, sometimes
    // leaving it "present" even though the UI showed "absent".
    await Promise.allSettled(
      ROSTER.map(async (s) => {
        const r = await jput('/api/attendance', { student_id: s.student_id, subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, is_present: true, source: 'INVIGILATOR_ISAL_TRANSCRIPTION' });
        ATT_SAVING[s.student_id] = false;
        if (r.ok) ATT_PERSISTED[s.student_id] = true;
      })
    );
    renderAttTable(); updateEntryBar();
  });
  $('save-attendance')?.addEventListener('click', async () => {
    if (!CURRENT) return;
    const btn = $('save-attendance'); btn.disabled = true; btn.textContent = 'Saving…';
    try {
      const todo = ROSTER.filter(s => !ATT_PERSISTED[s.student_id]);
      let failed = [];
      // Save in parallel for speed
      await Promise.allSettled(
        todo.map(async (s) => {
          ATT_SAVING[s.student_id] = true;
          const r = await jput('/api/attendance', { student_id: s.student_id, subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, is_present: ATT[s.student_id] !== false, source: 'INVIGILATOR_ISAL_TRANSCRIPTION' });
          ATT_SAVING[s.student_id] = false;
          if (r.ok) ATT_PERSISTED[s.student_id] = true; else failed.push(s.student_id);
        })
      );
      renderAttTable();
      const box = $('att-validation'); box.hidden = false;
      if (!failed.length) {
        box.className = 'marks-validation ok'; box.innerHTML = `<div class="mv-title">✓ Attendance saved for all ${ROSTER.length} student(s).</div>`;
        btn.innerHTML = '✓ All saved — opening Marks…'; btn.disabled = true;
        setTimeout(() => switchEntryTab('marks'), 800);
      } else {
        box.className = 'marks-validation blocked'; box.innerHTML = `<div class="mv-title">${failed.length} failed</div>`;
        btn.disabled = false; btn.textContent = 'Save & continue to Marks';
      }
    } catch (e) {
      // Never leave the button permanently disabled — a thrown error here
      // (network blip, unexpected response shape) must still let the
      // operator retry instead of getting stuck on a grayed-out button.
      btn.disabled = false; btn.textContent = 'Save & continue to Marks';
      setMsg('entry-msg', 'Could not save attendance — please try again.', true);
    }
  });
}
