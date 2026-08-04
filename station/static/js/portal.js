// portal.js — entry portal (scope selection table)
import { $, api, esc } from './api.js';
import { SCOPES, SCHOOLS, PORTAL_FILTER, setState } from './state.js';

export async function loadPortal() {
  try {
    const scopes = await api('/api/scopes').then(r => r.json());
    setState.scopes(scopes);
    if (!SCHOOLS.length) {
      const schools = await api('/api/schools').then(r => r.json()).catch(() => []);
      setState.schools(schools);
    }
  } catch (e) { setState.scopes([]); }
  renderPortal();
}

export function renderPortal() {
  const list = SCOPES.filter(s =>
    PORTAL_FILTER === 'all' ||
    (PORTAL_FILTER === 'open' && !s.finalized) ||
    (PORTAL_FILTER === 'finalized' && s.finalized)
  );
  if (!list.length) { $('portal-scope-list').innerHTML = '<p class="no-data">No scopes available.</p>'; return; }
  $('portal-scope-list').innerHTML = `<table class="portal-tbl">
    <thead><tr><th>Centre</th><th>School</th><th>Subject</th><th>Paper</th><th>Status</th><th class="pt-action"></th></tr></thead>
    <tbody>${list.map(s => {
    const idx = SCOPES.indexOf(s);
    const locked = !s.finalized && s.lock_status === 'LOCKED';
    const paper = s.paper_type.replace('THEORY1', 'T1').replace('THEORY2', 'T2').replace('PRACTICAL', 'P');
    const status = s.finalized ? '<span class="badge badge-done">Finalized</span>' : locked ? '<span class="badge badge-locked">In use</span>' : '<span class="badge badge-open">Open</span>';
    const btn = (!s.finalized && !locked) ? `<button class="btn-primary btn-sm" onclick="enterScope(${idx})">Enter →</button>` : '';
    return `<tr class="${s.finalized ? 'pt-row-fin' : ''}${locked ? ' pt-row-locked' : ''}">
        <td class="pt-centre">${esc(s.centre_number)}</td>
        <td class="pt-school">${esc(s.school_name || '')}</td>
        <td class="pt-subject">${esc(s.subject_name || s.subject_code)}</td>
        <td class="pt-paper">${esc(paper)}</td>
        <td class="pt-status">${status}</td>
        <td class="pt-action">${btn}</td>
      </tr>`;
  }).join('')}</tbody></table>`;
}

export function initPortal() {
  document.querySelectorAll('#portal-chips .chip').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('#portal-chips .chip').forEach(x => x.classList.remove('active'));
    b.classList.add('active'); setState.portalFilter(b.dataset.f); renderPortal();
  }));
}
