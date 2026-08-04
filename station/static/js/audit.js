// audit.js — marks audit log view
import { $, api, esc } from './api.js';

export async function loadAudit() {
  renderAuditTable([]);
  const params = new URLSearchParams();
  const sid = $('audit-student')?.value?.trim(); if (sid) params.set('student_id', sid);
  const sc = $('audit-subject')?.value?.trim(); if (sc) params.set('subject_code', sc);
  const pt = $('audit-paper')?.value; if (pt) params.set('paper_type', pt);
  const from = $('audit-from')?.value; if (from) params.set('from_date', from);
  const to = $('audit-to')?.value; if (to) params.set('to_date', to);
  try {
    const rows = await (await api('/api/audit/marks?' + params)).json();
    renderAuditTable(rows);
  } catch (e) { $('audit-tbody').innerHTML = `<tr><td colspan="8" class="td-empty">Error loading audit.</td></tr>`; }
}

export function renderAuditTable(rows) {
  if (!rows.length) {
    $('audit-tbody').innerHTML = '<tr><td colspan="8" class="td-empty">No audit records found.</td></tr>';
    return;
  }
  $('audit-tbody').innerHTML = rows.map(r => {
    const isCorrection = r.before_total != null && r.after_total != null;
    return `<tr class="${isCorrection ? 'audit-correction' : ''}">
      <td class="small">${esc(r.station_occurred_at?.replace('T', ' ')?.slice(0, 19) || '—')}</td>
      <td>${esc(r.student_id)}</td>
      <td>${esc(r.subject_code)}</td>
      <td>${esc(r.paper_type)}</td>
      <td>${r.before_total != null ? r.before_total : '—'}</td>
      <td>${r.after_total != null ? r.after_total : '—'}</td>
      <td>${esc(r.actor_initials || '—')}</td>
      <td>${esc(r.operation)}</td>
    </tr>`;
  }).join('');
}

export function initAudit() {
  $('audit-search-btn')?.addEventListener('click', loadAudit);
}
