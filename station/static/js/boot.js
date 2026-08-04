// boot.js — status probe, station chooser, login, logout
//
// Flow:
//   1. GET /api/status — if no station_code, we are in chooser mode.
//   2. GET /api/stations/available — always call. If there are 0 stations
//      show "waiting for package" empty state. If 1, the resolver has
//      auto-selected and we can jump straight to login. If N > 1 with none
//      active, the user must pick.
//   3. After picking, POST /api/stations/switch → refresh flow.
//   4. Login form authenticates against the currently-active station.
//   5. Post-login: topbar shows {station · role · user} plus a Switch button.

import { $, api, jpost, fmt, esc, setMsg } from './api.js';
import { SESSION, POLL_T, setState } from './state.js';
import { showView } from './router.js';

let ACTIVE_STATION = null;   // { station_code, exam_id } or null
let AVAILABLE_STATIONS = []; // [{ station_code, exam_id, exam_name, students }]

// ── Public: boot + initLogin ─────────────────────────────────────────────────

export async function boot() {
  await refreshStationContext();

  // Update status pill from /api/status (works even in chooser mode).
  try {
    const s = await api('/api/status').then(r => r.json());
    const pill = $('status-pill');
    if (s.station_code) {
      pill.textContent = `${s.station_code} · ${fmt(s.students)} students · v${s.software_version}`;
      pill.className = 'pill pill-ok';
    } else if (AVAILABLE_STATIONS.length === 0) {
      pill.textContent = 'No package imported yet';
      pill.className = 'pill pill-warn';
    } else {
      pill.textContent = 'Choose a station';
      pill.className = 'pill pill-warn';
    }
  } catch (e) {
    $('status-pill').textContent = 'Offline';
    $('status-pill').className = 'pill pill-warn';
  }

  // Are we already authenticated on the active station?
  const me = await api('/api/me');
  if (me.ok) {
    setState.session(await me.json());
    afterLogin();
    return;
  }

  // Not authenticated. Show either the picker or the login form.
  renderPreAuthUI();
  showView('login');
}

async function refreshStationContext() {
  try {
    const r = await api('/api/stations/available');
    if (r.ok) {
      const data = await r.json();
      AVAILABLE_STATIONS = data.stations || [];
      ACTIVE_STATION = data.active || null;
    }
  } catch (e) { /* offline — leave state as-is */ }
}

// ── Pre-auth UI: choose station or log in ────────────────────────────────────

function renderPreAuthUI() {
  const selector = $('station-selector');
  const form = $('login-form');
  const hint = document.querySelector('.login-hint');

  if (!selector) return;

  // 0 stations: waiting for import
  if (AVAILABLE_STATIONS.length === 0) {
    selector.hidden = false;
    selector.innerHTML = `
      <div class="station-empty">
        <p><strong>No exam package on this computer yet.</strong></p>
        <p class="muted small">Drop a signed <code>.zip</code> package into
        <code>stations/&lt;code&gt;/exams/&lt;id&gt;/imports/pending/</code>
        and click Refresh, or ask your coordinator for the package.</p>
        <button type="button" id="refresh-imports-btn" class="btn-secondary btn-sm">Refresh</button>
      </div>`;
    if (form) form.hidden = true;
    if (hint) hint.hidden = true;
    $('refresh-imports-btn')?.addEventListener('click', async () => {
      await jpost('/api/stations/refresh-imports', {});
      await refreshStationContext();
      renderPreAuthUI();
    });
    return;
  }

  // Chooser: several stations, none active OR active isn't uniquely determined
  if (!ACTIVE_STATION) {
    selector.hidden = false;
    if (form) form.hidden = true;
    if (hint) hint.hidden = true;
    selector.innerHTML = renderPicker(
      '<p class="login-hint" style="margin-bottom:12px;font-weight:600">Select which station to work on:</p>',
      /* showActive= */ false
    );
    wirePickerButtons();
    return;
  }

  // Exactly one station is active — show its name and the login form.
  selector.hidden = false;
  if (form) form.hidden = false;
  if (hint) hint.hidden = false;
  const rest = AVAILABLE_STATIONS.filter(s =>
    !(s.station_code === ACTIVE_STATION.station_code && s.exam_id === ACTIVE_STATION.exam_id));
  const switchLink = rest.length > 0
    ? `<button type="button" id="show-picker-btn" class="linkish btn-ghost btn-sm">Switch station</button>`
    : '';
  selector.innerHTML = `
    <p class="login-hint" style="margin-bottom:8px">
      Station: <strong>${esc(ACTIVE_STATION.station_code)}</strong>
      ${switchLink}
    </p>`;
  $('show-picker-btn')?.addEventListener('click', () => {
    selector.innerHTML = renderPicker(
      '<p class="login-hint" style="margin-bottom:12px;font-weight:600">Switch to a different station on this computer:</p>',
      /* showActive= */ true
    );
    if (form) form.hidden = true;
    wirePickerButtons();
  });
}

function renderPicker(header, showActive) {
  const list = AVAILABLE_STATIONS.map(s => {
    const isActive = ACTIVE_STATION
      && ACTIVE_STATION.station_code === s.station_code
      && ACTIVE_STATION.exam_id === s.exam_id;
    const badge = isActive && showActive ? '<span class="badge badge-done">Current</span>' : '';
    return `
      <button type="button" class="station-option${isActive ? ' active' : ''}"
              data-code="${esc(s.station_code)}" data-exam="${esc(s.exam_id)}">
        <strong>${esc(s.station_code)}</strong>
        <span class="muted">${fmt(s.students)} students${s.exam_name ? ' · ' + esc(s.exam_name) : ''}</span>
        ${badge}
      </button>`;
  }).join('');
  return `${header}<div class="station-picker">${list}</div>`;
}

function wirePickerButtons() {
  document.querySelectorAll('.station-option').forEach(btn => {
    btn.addEventListener('click', async () => {
      const code = btn.dataset.code, exam = btn.dataset.exam;
      const sel = $('station-selector');
      sel.innerHTML = `<p class="login-hint">Switching to <strong>${esc(code)}</strong>…</p>`;
      const r = await jpost('/api/stations/switch',
                            { station_code: code, exam_id: exam });
      if (!r.ok) {
        sel.innerHTML = `<p class="form-msg err">Could not switch. Please refresh.</p>`;
        return;
      }
      // Refresh context and re-render (no reload needed).
      await refreshStationContext();
      renderPreAuthUI();
    });
  });
}

// ── Login form ───────────────────────────────────────────────────────────────

export function initLogin() {
  $('login-form').addEventListener('submit', async e => {
    e.preventDefault();
    const id = $('login-id').value.trim(), secret = $('login-secret').value;
    setMsg('login-msg', 'Signing in…', false);
    const [dr, ar] = await Promise.allSettled([
      jpost('/api/login/de', { pin: secret, initials: id }),
      jpost('/api/login/admin', { username: id, password: secret }),
    ]);
    const dok = dr.status === 'fulfilled' && dr.value.ok;
    const aok = ar.status === 'fulfilled' && ar.value.ok;
    if (dok) { setState.session(await dr.value.json()); setMsg('login-msg', '', false); afterLogin(); }
    else if (aok) { setState.session(await ar.value.json()); setMsg('login-msg', '', false); afterLogin(); }
    else setMsg('login-msg', 'Incorrect credentials.', true);
  });

  $('logout-btn').addEventListener('click', async () => {
    await jpost('/api/logout', {});
    setState.session(null); setState.current(null); setState.roster([]);
    $('logout-btn').hidden = true; $('who-label').textContent = '';
    if (POLL_T) { clearInterval(POLL_T); setState.pollT(null); }
    await refreshStationContext();
    renderPreAuthUI();
    showView('login');
  });

  // "Switch station" button in the topbar (added by after-login wiring).
  document.body.addEventListener('click', async (e) => {
    if (e.target?.id !== 'switch-station-btn') return;
    if (!confirm('Switch to a different station? You will need to log in again.')) return;
    await jpost('/api/logout', {});
    setState.session(null); setState.current(null); setState.roster([]);
    $('logout-btn').hidden = true; $('who-label').textContent = '';
    if (POLL_T) { clearInterval(POLL_T); setState.pollT(null); }
    await refreshStationContext();
    // Force chooser
    ACTIVE_STATION = null;
    renderPreAuthUI();
    showView('login');
  });
}

// ── Post-login ───────────────────────────────────────────────────────────────

export function afterLogin() {
  const role = SESSION?.role;
  const isAdmin = role === 'EXAM_ADMIN';
  const name = SESSION?.username || SESSION?.initials || '';
  const station = SESSION?.station_code || ACTIVE_STATION?.station_code || '';

  // Topbar: {station} · {Admin|DE} · {name}    [Switch station]
  const label = [
    station ? `<strong>${esc(station)}</strong>` : '',
    isAdmin ? 'Admin' : 'DE',
    name ? esc(name) : '',
  ].filter(Boolean).join(' · ');
  $('who-label').innerHTML =
    `${label} <button type="button" id="switch-station-btn" class="btn-ghost btn-sm" title="Switch station">⇄</button>`;
  $('logout-btn').hidden = false;

  // Capability-based nav gating (falls back to role if no capabilities).
  const caps = new Set(SESSION?.capabilities || []);
  const canAdmin = isAdmin || caps.has('admin.users.manage');
  const adminGroup = document.querySelector('.nav-group.admin-only');
  if (adminGroup) adminGroup.hidden = !canAdmin;

  const prof = $('sidebar-profile');
  if (prof) {
    prof.hidden = false;
    const av = $('sp-avatar'); if (av) av.textContent = (name || '?').slice(0, 2).toUpperCase();
    const sn = $('sp-name'); if (sn) sn.textContent = name || '—';
    const sr = $('sp-role'); if (sr) sr.textContent = isAdmin ? 'Admin' : 'Data Enterer';
  }

  if (isAdmin) {
    import('./dashboard.js').then(m => m.loadDashboardAdmin());
    showView('dashboard-admin');
  } else {
    import('./dashboard.js').then(m => m.loadDashboardDE());
    showView('dashboard-de');
  }

  if (POLL_T) clearInterval(POLL_T);
  setState.pollT(setInterval(() => {
    if (isAdmin && $('view-dashboard-admin') && !$('view-dashboard-admin').hidden) {
      import('./dashboard.js').then(m => m.loadDashboardAdmin());
    } else if (!isAdmin && $('view-dashboard-de') && !$('view-dashboard-de').hidden) {
      import('./dashboard.js').then(m => m.loadDashboardDE());
    }
  }, 30000));

  updateStatusPill();
}

async function updateStatusPill() {
  try {
    const [s, p] = await Promise.all([
      api('/api/status').then(r => r.json()),
      api('/api/progress').then(r => r.json()),
    ]);
    const pill = $('status-pill');
    if (!s.station_code) return;
    const base = `${s.station_code} · ${fmt(s.students)} students · v${s.software_version}`;
    if (p.rejected_events > 0) {
      pill.textContent = base + ` · ✗ ${p.rejected_events} rejected`;
      pill.className = 'pill pill-err';
    } else if (p.pending_events > 0) {
      pill.textContent = base + ` · ⏳ ${p.pending_events} pending`;
      pill.className = 'pill pill-warn';
    } else {
      pill.textContent = base + ' · ✓ synced';
      pill.className = 'pill pill-ok';
    }
  } catch (e) { }
}
