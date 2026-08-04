// dashboard.js — loadDashboardDE(), loadDashboardAdmin()
import { $, api, esc, fmt, pct } from './api.js';
import { SESSION, SCOPES, SCHOOLS, setState } from './state.js';

export async function loadDashboardDE() {
  let p;
  try {
    [p] = await Promise.all([api('/api/progress').then(r => r.json())]);
    const scopes = await api('/api/scopes').then(r => r.json()).catch(() => []);
    setState.scopes(scopes);
  } catch (e) { return; }

  // My progress
  const myScopes = SCOPES.length;
  const myFin = SCOPES.filter(s => s.finalized).length;
  const myPct = pct(myFin, myScopes);
  $('de-progress').innerHTML = `
    <div class="kpi-grid kpi-grid-sm">
      <div class="kpi"><span class="kpi-val">${fmt(p.marks_today)}</span><span class="kpi-label">Marks today</span></div>
      <div class="kpi"><span class="kpi-val">${fmt(p.total_marks)}</span><span class="kpi-label">Total marks</span></div>
      <div class="kpi"><span class="kpi-val">${myFin}/${myScopes}</span><span class="kpi-label">Scopes done</span></div>
      <div class="kpi"><span class="kpi-val">${myPct}%</span><span class="kpi-label">Complete</span></div>
    </div>
    <div class="progress-bar-wrap"><div class="progress-bar" style="width:${myPct}%"></div></div>`;

  // My scopes table
  const nameMap = Object.fromEntries((SCHOOLS || []).map(s => [s.centre_number, s.name || '']));
  $('de-scopes-tbl').innerHTML = SCOPES.length ? `<table class="portal-tbl compact">
    <thead><tr><th>Centre</th><th>School</th><th>Subject</th><th>Paper</th><th></th></tr></thead>
    <tbody>${SCOPES.map((s, i) => {
    const paper = s.paper_type.replace('THEORY1', 'T1').replace('THEORY2', 'T2').replace('PRACTICAL', 'P');
    const done = s.finalized;
    return `<tr class="${done ? 'pt-row-fin' : ''}">
        <td>${esc(s.centre_number)}</td>
        <td>${esc(s.school_name || nameMap[s.centre_number] || '')}</td>
        <td>${esc(s.subject_name || s.subject_code)}</td>
        <td>${esc(paper)}</td>
        <td>${done ? '<span class="badge badge-done">✓</span>' : `<button class="btn-primary btn-sm" onclick="enterScope(${i})">Enter →</button>`}</td>
      </tr>`;
  }).join('')}</tbody></table>` : '<p class="no-data">No scopes assigned.</p>';
}

export async function loadDashboardAdmin() {
  let p, schools;
  try {
    [p, schools] = await Promise.all([
      api('/api/progress').then(r => r.json()),
      api('/api/schools').then(r => r.json()).catch(() => []),
    ]);
    setState.schools(schools);
  } catch (e) { return; }

  const total = p.total_scopes || 0, fin = p.finalized_scopes || 0;
  const pctDone = pct(fin, total);

  // KPIs
  $('admin-kpis').innerHTML = `
    <div class="kpi ${fin === total && total > 0 ? 'ok' : ''}"><span class="kpi-val">${fin}/${total}</span><span class="kpi-label">Scopes finalized</span></div>
    <div class="kpi"><span class="kpi-val">${fmt(p.total_marks)}</span><span class="kpi-label">Marks entered</span></div>
    <div class="kpi"><span class="kpi-val">${fmt(p.marks_today)}</span><span class="kpi-label">Entered today</span></div>
    <div class="kpi ${p.pending_events > 0 ? 'warn' : ''}"><span class="kpi-val">${fmt(p.pending_events)}</span><span class="kpi-label">Pending sync</span></div>
    <div class="kpi ${p.rejected_events > 0 ? 'err' : ''}"><span class="kpi-val">${fmt(p.rejected_events)}</span><span class="kpi-label">Rejected</span></div>
    <div class="kpi"><span class="kpi-val">${pctDone}%</span><span class="kpi-label">Complete</span></div>`;

  // Sync card
  let syncCfg = {};
  try { syncCfg = await (await api('/api/sync/config')).json(); } catch (e) { }
  $('admin-sync').innerHTML = syncCfg.configured
    ? `<p>&#10003; <strong>Connected</strong><br><span class="muted small">${esc(syncCfg.central_url)}</span><br>${p.pending_events > 0 ? `<span style="color:var(--warn)">${fmt(p.pending_events)} events pending</span>` : 'All events synced'}</p>`
    : `<p style="color:var(--warn)">&#9888; Sync not configured — go to Sync/Settings</p>`;

  // Today card
  $('admin-today').innerHTML = `<p>
    <strong>${fmt(p.marks_today)}</strong> marks entered today<br>
    <strong>${fmt(p.total_marks)}</strong> total marks in database<br>
    <strong>${fmt(p.students)}</strong> students on this station</p>`;

  // Schools table
  const sorted = [...schools].sort((a, b) => a.centre_number.localeCompare(b.centre_number));
  $('admin-schools-tbl').innerHTML = `<table class="dash-schools-tbl">
    <thead><tr><th>Centre</th><th>School Name</th><th>Scopes</th><th>Progress</th></tr></thead>
    <tbody>${sorted.map(s => {
    const p2 = pct(s.finalized_scopes, s.total_scopes);
    const cls = p2 === 100 ? 'badge-done' : p2 > 0 ? 'badge-locked' : 'badge-open';
    return `<tr class="dash-school-row" onclick="goSchool('${esc(s.centre_number)}')" style="cursor:pointer">
        <td class="ds-code">${esc(s.centre_number)}</td>
        <td class="ds-name">${esc(s.name || '—')}</td>
        <td class="ds-scopes">${s.finalized_scopes}/${s.total_scopes}</td>
        <td class="ds-pct"><span class="badge ${cls}">${p2}%</span></td>
      </tr>`;
  }).join('')}</tbody></table>`;
}
