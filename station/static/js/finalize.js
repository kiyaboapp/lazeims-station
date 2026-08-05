// finalize.js — finalize button, completeness check, incidents
import { $, api, jpost, esc, setMsg } from './api.js';
import { CURRENT } from './state.js';
import { loadPortal } from './portal.js';

window.jumpToStudent = function (sid) {
  const inp = $('marks-tbody')?.querySelector(`input[data-sid="${sid}"]`);
  if (inp) { inp.scrollIntoView({ block: 'center', behavior: 'smooth' }); inp.focus(); inp.select(); inp.classList.add('jump-highlight'); setTimeout(() => inp.classList.remove('jump-highlight'), 1500); }
};

export function initFinalize() {
  $('finalize-btn')?.addEventListener('click', async () => {
    if (!confirm('Finalize this scope? This cannot be undone.')) return;
    if (window.flushMarks) await window.flushMarks();
    const r = await jpost('/api/scopes/finalize', CURRENT);
    if (r.ok) {
      setMsg('entry-msg', 'Scope finalized.', false);
      // Offer to print marks report
      if (confirm('Scope finalized! Print marks report?')) {
        await printMarksReport();
      }
      await loadPortal();
      setTimeout(() => $('entry-back')?.click(), 1400);
    } else {
      const d = await r.json().catch(() => ({}));
      const b = (d.detail?.result?.blockers || []).map(x => x.message).join('; ');
      setMsg('entry-msg', 'Cannot finalize: ' + (b || 'incomplete data'), true);
    }
  });

  $('check-completeness')?.addEventListener('click', async () => {
    if (!CURRENT) return;
    const btn = $('check-completeness'); btn.disabled = true; btn.textContent = 'Checking…';
    if (window.flushMarks) await window.flushMarks();
    const box = $('marks-validation');
    try {
      const q = new URLSearchParams({ centre_number: CURRENT.centre_number, subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type });
      const res = await (await api('/api/scopes/validation?' + q)).json();
      box.hidden = false;
      if (res.complete) {
        box.className = 'marks-validation ok';
        box.innerHTML = '<div class="mv-title">✓ Complete — every present student has marks.</div>You can finalize this scope now.';
      } else {
        const missing = (res.blockers || []).filter(b => b.student_id && b.code === 'BLANK_MARK_NOT_ALLOWED');
        const other = (res.blockers || []).filter(b => b.student_id && b.code !== 'BLANK_MARK_NOT_ALLOWED');
        box.className = 'marks-validation blocked';
        box.innerHTML = `<div class="mv-title">${missing.length + other.length} student(s) need attention:</div>
          <div class="mv-list">${[...missing, ...other].map(b => `<button type="button" class="missing-chip" onclick="jumpToStudent('${esc(b.student_id)}')" title="${esc(b.message)}">${esc(b.student_id)}</button>`).join('')}</div>
          ${missing.length ? `<div style="margin-top:10px"><button type="button" id="force-complete-btn" class="btn-warning btn-sm">Force complete (${missing.length})</button></div>` : ''}`;
        if (missing.length) {
          $('force-complete-btn')?.addEventListener('click', async () => {
            const reason = prompt(`Explain why these ${missing.length} present student(s) have no mark:`);
            if (!reason) return;
            const fb = $('force-complete-btn'); fb.disabled = true; fb.textContent = 'Raising incidents…';
            for (const b of missing) {
              await jpost('/api/incidents', { student_id: b.student_id, subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type, incident_type: 'OTHER', explanation: reason });
            }
            $('check-completeness')?.click();
          });
        }
      }
    } catch (e) { box.hidden = false; box.className = 'marks-validation blocked'; box.innerHTML = 'Could not check.'; }
    btn.disabled = false; btn.textContent = 'Check completeness';
  });
}

async function printMarksReport() {
  if (!CURRENT) return;
  try {
    const q = new URLSearchParams({ centre_number: CURRENT.centre_number, subject_code: CURRENT.subject_code, paper_type: CURRENT.paper_type });
    const res = await (await api('/api/scopes/report?' + q)).json();
    const students = res.students || [];
    const questions = res.questions || [];
    const hasItems = questions.length > 0;

    let html = `<!DOCTYPE html><html><head><title>Marks Report</title><style>
      * { margin:0; padding:0; box-sizing:border-box; }
      body { font-family:'Segoe UI',Arial,sans-serif; font-size:11px; padding:20px; color:#111; }
      h1 { font-size:16px; text-align:center; margin-bottom:4px; }
      .subtitle { font-size:11px; text-align:center; color:#555; margin-bottom:16px; }
      .meta { display:flex; flex-wrap:wrap; gap:8px 24px; margin-bottom:12px; font-size:10px; border:1px solid #ddd; padding:8px 12px; border-radius:4px; }
      .meta span { white-space:nowrap; }
      .meta strong { font-weight:700; }
      table { width:100%; border-collapse:collapse; margin-top:8px; }
      th, td { border:1px solid #999; padding:4px 6px; text-align:center; font-size:10px; }
      th { background:#f0f0f0; font-weight:700; text-transform:uppercase; font-size:9px; }
      td.name { text-align:left; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      td.id { font-family:monospace; font-size:10px; text-align:left; white-space:nowrap; }
      .absent td { background:#fef2f2; color:#991b1b; }
      .total-col { font-weight:700; background:#f9fafb; }
      .footer { margin-top:16px; font-size:9px; color:#666; display:flex; justify-content:space-between; }
      @media print { body { padding:10px; } }
    </style></head><body>`;

    html += `<h1>${esc(res.exam_name || 'MARKS REPORT')}</h1>`;
    html += `<div class="subtitle">ISAL TRANSCRIPTION — MARKS REPORT</div>`;
    html += `<div class="meta">`;
    html += `<span><strong>Centre:</strong> ${esc(res.centre_number)}</span>`;
    html += `<span><strong>School:</strong> ${esc(res.school_name)}</span>`;
    html += `<span><strong>Subject:</strong> ${esc(res.subject_name)} (${esc(res.subject_code)})</span>`;
    html += `<span><strong>Paper:</strong> ${esc(res.paper_type.replace('THEORY1','Theory 1').replace('THEORY2','Theory 2').replace('PRACTICAL','Practical'))}</span>`;
    html += `<span><strong>Max Marks:</strong> ${res.total_possible}</span>`;
    html += `</div>`;
    html += `<div class="meta">`;
    if (res.station_code) html += `<span><strong>Station:</strong> ${esc(res.station_code)}</span>`;
    if (res.enterer_initials) html += `<span><strong>Entered by:</strong> ${esc(res.enterer_initials)}</span>`;
    if (res.finalized_at) html += `<span><strong>Finalized:</strong> ${new Date(res.finalized_at).toLocaleString()}</span>`;
    const present = students.filter(s => s.attendance !== false).length;
    const absent = students.length - present;
    html += `<span><strong>Present:</strong> ${present}</span>`;
    html += `<span><strong>Absent:</strong> ${absent}</span>`;
    html += `<span><strong>Total Students:</strong> ${students.length}</span>`;
    html += `</div>`;

    html += `<table><thead><tr>`;
    html += `<th>#</th><th>Index</th><th>Student Name</th><th>Att</th>`;
    if (hasItems) questions.forEach(q => { html += `<th>Q${q.number}</th>`; });
    html += `<th class="total-col">Total</th></tr></thead><tbody>`;

    students.forEach((s, i) => {
      const isAbsent = s.attendance === false;
      html += `<tr class="${isAbsent ? 'absent' : ''}">`;
      html += `<td>${i + 1}</td><td class="id">${esc(s.student_id)}</td><td class="name">${esc(s.full_name)}</td>`;
      html += `<td>${isAbsent ? 'A' : 'P'}</td>`;
      if (hasItems) questions.forEach(q => { html += `<td>${isAbsent ? '—' : (s.item_marks?.[q.number] ?? '—')}</td>`; });
      html += `<td class="total-col">${isAbsent ? '—' : (s.total_marks ?? '—')}</td></tr>`;
    });

    html += `</tbody></table>`;
    html += `<div class="footer"><span>Printed: ${new Date().toLocaleString()}</span><span>Station: ${esc(res.station_code || 'N/A')} · ${esc(res.exam_name)} · ${esc(res.centre_number)} · ${esc(res.subject_name)} (${esc(res.paper_type)})</span></div>`;
    html += `</body></html>`;

    const win = window.open('', '_blank', 'width=800,height=600');
    if (win) { win.document.write(html); win.document.close(); setTimeout(() => { win.print(); }, 300); }
  } catch (e) {
    alert('Could not generate report: ' + (e.message || e));
  }
}
window.printMarksReport = printMarksReport;
