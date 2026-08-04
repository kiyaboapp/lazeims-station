// scopes.js — scopes list view + force-release
import { $, api, jpost, esc } from './api.js';
import { SESSION, SCOPES, SCHOOLS, SCOPE_FILTER, setState } from './state.js';

export async function loadScopesView() {
  try { setState.scopes(await (await api('/api/scopes')).json()); } catch (e) { setState.scopes([]); }
  renderScopesView();
}

export function renderScopesView() {
  const fin = SCOPES.filter(s => s.finalized).length;
  const sub = $('scopes-sub'); if (sub) sub.textContent = `${fin}/${SCOPES.length} finalized`;
  const list = SCOPES.filter(s =>
    SCOPE_FILTER === 'all' ||
    (SCOPE_FILTER === 'open' && !s.finalized && s.lock_status !== 'LOCKED') ||
    (SCOPE_FILTER === 'locked' && s.lock_status === 'LOCKED' && !s.finalized) ||
    (SCOPE_FILTER === 'finalized' && s.finalized)
  );
  if (!list.length) { $('scopes-list').innerHTML = '<p class="no-data">No scopes match this filter.</p>'; return; }
  $('scopes-list').innerHTML = list.map(s => {
    const locked = !s.finalized && s.lock_status === 'LOCKED';
    const paper = s.paper_type.replace('THEORY', 'T').replace('PRACTICAL', 'P');
    const badgeCls = s.finalized ? 'badge-done' : locked ? 'badge-locked' : 'badge-open';
    const label = s.finalized ? 'Finalized' : locked ? 'In use' : 'Open';
    return `<div class="scope-row ${s.finalized ? 'finalized' : ''}">
      <div class="scope-icon ${s.finalized ? 'done' : locked ? 'locked' : 'open'}">${esc(paper)}</div>
      <div class="scope-info">
        <div class="scope-centre">${esc(s.centre_number)}${s.school_name ? ` <span class="scope-school-name">${esc(s.school_name)}</span>` : ''}</div>
        <div class="scope-subject">${esc(s.subject_code)}${s.subject_name ? ' · ' + esc(s.subject_name) : ''}</div>
      </div>
      <div class="scope-actions">
        <span class="badge ${badgeCls}">${label}</span>
        ${locked && SESSION?.role === 'EXAM_ADMIN' ? `<button class="btn-ghost btn-sm" onclick="forceRelease('${esc(s.centre_number)}','${esc(s.subject_code)}','${esc(s.paper_type)}')">Release lock</button>` : ''}
      </div>
    </div>`;
  }).join('');
}

export function initScopes() {
  document.querySelectorAll('#scopes-chips .chip').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('#scopes-chips .chip').forEach(x => x.classList.remove('active'));
    b.classList.add('active'); setState.scopeFilter(b.dataset.f); renderScopesView();
  }));
}

window.forceRelease = async function (cn, sc, pt) {
  const reason = prompt('Reason for force-releasing this lock?');
  if (!reason) return;
  const r = await jpost('/api/locks/force-release', { centre_number: cn, subject_code: sc, paper_type: pt, reason });
  if (r.ok) { alert('Lock released.'); loadScopesView(); }
  else { const d = await r.json().catch(() => ({})); alert('Failed: ' + (d.detail?.message || 'unknown error')); }
};
