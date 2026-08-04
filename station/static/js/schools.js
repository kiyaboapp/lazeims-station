// schools.js — schools accordion view
import { $, api, esc, fmt, pct } from './api.js';
import { SCHOOLS, setState } from './state.js';

export async function loadSchools() {
  try { setState.schools(await (await api('/api/schools')).json()); } catch (e) { setState.schools([]); }
  renderSchools();
}

export function renderSchools() {
  const q = ($('school-search')?.value || '').toLowerCase();
  const list = q ? SCHOOLS.filter(s => s.centre_number.toLowerCase().includes(q) || s.name.toLowerCase().includes(q)) : SCHOOLS;
  const fin = SCHOOLS.reduce((a, s) => a + s.finalized_scopes, 0);
  const tot = SCHOOLS.reduce((a, s) => a + s.total_scopes, 0);
  const sub = $('schools-sub'); if (sub) sub.textContent = `${SCHOOLS.length} schools · ${fin}/${tot} scopes finalized`;

  if (!list.length) { $('schools-list').innerHTML = '<p class="no-data">No schools match.</p>'; return; }

  $('schools-list').innerHTML = list.map(school => {
    const p2 = pct(school.finalized_scopes, school.total_scopes);
    const full = p2 === 100 && school.total_scopes > 0;
    return `<div class="school-card" data-school="${esc(school.centre_number)}">
      <div class="school-card-header" onclick="toggleSchool(this)">
        <span class="school-h-code">${esc(school.centre_number)}</span>
        <span class="school-h-name">${esc(school.name || '(no name)')}</span>
        <div class="school-h-stats">
          <span class="badge ${full ? 'badge-done' : 'badge-open'}">${school.finalized_scopes}/${school.total_scopes}</span>
          <span class="muted small">${fmt(school.students)} students</span>
          <div class="school-h-pbar-wrap"><div class="school-h-pbar ${full ? 'complete' : ''}" style="width:${p2}%"></div></div>
          <svg class="school-h-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
        </div>
      </div>
      <div class="school-body">
        ${(school.scopes || []).map(sc => {
      const mp = pct(sc.marks_entered, sc.students);
      const paper = sc.paper_type.replace('THEORY', 'T').replace('PRACTICAL', 'P');
      const isFin = sc.finalized;
      return `<div class="school-scope-row">
            <div class="scope-tag">
              <span class="tag-paper">${esc(paper)}</span>
              <span class="muted small">${esc(sc.subject_name || sc.subject_code)}</span>
              ${isFin ? '<span class="badge badge-done" style="font-size:10px">Finalized</span>' : sc.lock_status === 'LOCKED' ? '<span class="badge badge-locked" style="font-size:10px">In use</span>' : ''}
            </div>
            <div class="scope-nums">
              <span>Marks: <strong>${fmt(sc.marks_entered)}/${fmt(sc.students)}</strong></span>
              <span>Present: <strong>${fmt(sc.att_present)}</strong> Absent: <strong>${fmt(sc.att_absent)}</strong></span>
            </div>
            <div class="scope-bar-wrap"><div class="scope-bar ${isFin ? 'done' : ''}" style="width:${mp}%"></div></div>
          </div>`;
    }).join('')}
      </div>
    </div>`;
  }).join('');
}

export function initSchools() {
  const search = $('school-search');
  if (search) search.addEventListener('input', renderSchools);
}

window.toggleSchool = function (hdr) {
  hdr.classList.toggle('open');
  const body = hdr.nextElementSibling;
  body.classList.toggle('open');
  hdr.querySelector('.school-h-chevron').classList.toggle('open');
};
