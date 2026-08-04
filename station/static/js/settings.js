// settings.js — sync/settings + pull snapshot + export/import ack
import { $, api, jpost, esc, fmt, setMsg } from './api.js';

export async function loadSettings() {
  loadSyncConfig();
  // Station info
  try {
    const s = await (await api('/api/status')).json();
    $('station-info-list').innerHTML = [
      ['Station code', s.station_code || '—'],
      ['Exam ID', (s.exam_id || '—').slice(0, 16) + '…'],
      ['Students', fmt(s.students)],
      ['Software version', s.software_version || '—'],
      ['Packages imported', s.packages || 0],
    ].map(([l, v]) => `<div class="info-row"><span class="info-row-label">${l}</span><span class="info-row-val">${esc(String(v))}</span></div>`).join('');
  } catch (e) { }
}

async function loadSyncConfig() {
  try {
    const c = await (await api('/api/sync/config')).json();
    const inp = $('central-url-input'); if (inp) inp.value = c.central_url || '';
    const banner = $('sync-banner');
    if (banner) {
      if (c.configured) { banner.className = 'sync-banner ready'; banner.innerHTML = `&#10003; Ready · <strong>${esc(c.central_url)}</strong>`; }
      else if (!c.central_url) { banner.className = 'sync-banner no-url'; banner.textContent = 'No Central URL — import a package to configure.'; }
      else { banner.className = 'sync-banner no-cred'; banner.innerHTML = 'URL set but no credential — re-import.'; }
    }
  } catch (e) { }
}

export function initSettings() {
  $('save-url-btn')?.addEventListener('click', async () => {
    const url = ($('central-url-input')?.value || '').trim();
    const r = await jpost('/api/sync/config', { central_url: url });
    setMsg('sync-msg', r.ok ? 'URL saved.' : 'Admin only.', !r.ok);
    if (r.ok) loadSyncConfig();
  });

  $('sync-now-btn')?.addEventListener('click', async () => {
    setMsg('sync-msg', 'Syncing…', false); $('sync-result').textContent = '';
    const r = await jpost('/api/sync/run', {});
    const d = await r.json().catch(() => ({}));
    if (d.configured === false) { setMsg('sync-msg', 'Not configured: ' + (d.reason || ''), true); return; }
    if (d.error) { setMsg('sync-msg', 'Network error: ' + d.error, true); return; }
    if ((d.sent ?? 0) === 0) { setMsg('sync-msg', '', false); $('sync-result').textContent = 'Nothing to sync.'; return; }
    setMsg('sync-msg', '', false);
    $('sync-result').textContent = `Sent ${d.sent}: accepted ${d.accepted ?? 0}, rejected ${d.rejected ?? 0}, duplicates ${d.duplicates ?? 0}`;
  });

  $('retry-rejected-btn')?.addEventListener('click', async () => {
    setMsg('sync-msg', 'Resetting…', false);
    const r = await jpost('/api/sync/retry-rejected', {});
    const d = await r.json().catch(() => ({}));
    if (r.ok) { $('sync-result').textContent = `${d.queued} event(s) queued.`; loadSyncConfig(); }
    else setMsg('sync-msg', d.detail || 'Failed.', true);
  });

  $('import-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const f = $('import-file'); if (!f?.files?.length) { setMsg('import-msg', 'Choose a .zip', true); return; }
    const fd = new FormData(); fd.append('file', f.files[0]);
    setMsg('import-msg', 'Importing…', false);
    const r = await fetch('/api/import', { method: 'POST', body: fd, credentials: 'same-origin' });
    if (r.ok) { setMsg('import-msg', 'Imported. Reloading…', false); setTimeout(() => location.reload(), 1400); }
    else { const d = await r.json().catch(() => ({})); setMsg('import-msg', d.error?.message || 'Failed.', true); }
  });

  // Pull snapshot
  $('pull-snapshot-btn')?.addEventListener('click', async () => {
    const el = $('snapshot-result'); el.innerHTML = 'Fetching…';
    try {
      const r = await jpost('/api/sync/pull-snapshot', {});
      const d = await r.json();
      if (d.configured === false) { el.innerHTML = `<p class="muted">${esc(d.reason)}</p>`; return; }
      if (!r.ok) { el.innerHTML = `<p class="st-err">${esc(d.detail || 'Error')}</p>`; return; }
      // Compare with local digests
      let local = [];
      try { local = await (await api('/api/sync/local-digests')).json(); } catch (e) { }
      const localMap = Object.fromEntries(local.map(l => [`${l.centre_number}|${l.subject_code}|${l.paper_type}`, l.local_digest]));
      el.innerHTML = `<p class="muted small">Generated: ${esc(d.generated_at)}</p>
        <table class="portal-tbl compact"><thead><tr><th>Scope</th><th>Central</th><th>Local</th><th>Match?</th></tr></thead>
        <tbody>${(d.scopes || []).map(s => {
        const key = `${s.centre_number}|${s.subject_code}|${s.paper_type}`;
        const ld = localMap[key];
        const match = ld && ld === s.scope_digest;
        return `<tr><td>${esc(s.centre_number)}·${esc(s.subject_code)}·${esc(s.paper_type)}</td>
          <td>${s.marks_count}/${s.student_count}</td><td>${ld ? '✓' : '—'}</td>
          <td>${s.finalized ? '<span class="badge badge-done">Finalized</span>' : match ? '<span class="st-saved">✓ Match</span>' : ld ? '<span class="st-err">⚠ Mismatch</span>' : '<span class="muted">Not synced yet</span>'}</td></tr>`;
      }).join('')}</tbody></table>`;
    } catch (e) { el.innerHTML = `<p class="st-err">Error: ${esc(e.message)}</p>`; }
  });

  // Export outbox
  $('export-outbox-btn')?.addEventListener('click', async () => {
    const r = await api('/api/sync/export-outbox');
    if (r.status === 204) { alert('Nothing to export — outbox is empty.'); return; }
    if (!r.ok) { alert('Export failed.'); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'outbox.zip'; a.click();
    URL.revokeObjectURL(url);
  });

  // Import ack
  $('import-ack-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const f = $('import-ack-file'); if (!f?.files?.length) { setMsg('ack-msg', 'Choose a .zip', true); return; }
    const fd = new FormData(); fd.append('file', f.files[0]);
    setMsg('ack-msg', 'Importing…', false);
    const r = await fetch('/api/sync/import-ack', { method: 'POST', body: fd, credentials: 'same-origin' });
    if (r.ok) { const d = await r.json(); setMsg('ack-msg', `Done: ${d.accepted} accepted, ${d.rejected} rejected, ${d.duplicates} dups.`, false); }
    else { const d = await r.json().catch(() => ({})); setMsg('ack-msg', d.detail || 'Failed.', true); }
  });
}
