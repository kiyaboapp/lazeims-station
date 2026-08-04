// users.js — users view + create DE form
import { $, api, jpost, jdel, esc, fmt, relTime, setMsg } from './api.js';
import { SCHOOLS, SCOPES, PENDING_SCOPES, setState } from './state.js';
import { loadSchools } from './schools.js';

function populateScopeDropdowns() {
  const schoolSel = $('new-scope-school'), subjSel = $('new-scope-subject');
  if (!schoolSel || !subjSel) return;
  schoolSel.innerHTML = '<option value="">All schools</option>' +
    (SCHOOLS || []).map(s => `<option value="${esc(s.centre_number)}">${esc(s.centre_number)} — ${esc(s.name || '(no name)')}</option>`).join('');
  const subjects = [...new Map((SCOPES || []).map(s => [s.subject_code, s.subject_name || s.subject_code]))];
  subjSel.innerHTML = '<option value="">All subjects</option>' +
    subjects.sort((a, b) => a[0].localeCompare(b[0])).map(([code, name]) => `<option value="${esc(code)}">${esc(code)}${name && name !== code ? ' · ' + esc(name) : ''}</option>`).join('');
}

function renderPendingScopes() {
  const el = $('assigned-scopes-list'); if (!el) return;
  if (!PENDING_SCOPES.length) { el.innerHTML = '<span class="muted small">No restrictions — access to all scopes</span>'; return; }
  el.innerHTML = PENDING_SCOPES.map((sc, i) => `<span class="assigned-scope-chip">${esc(sc.centre_number || '*')} · ${esc(sc.subject_code || '*')} · ${esc(sc.paper_type || '*')}<button type="button" onclick="removePendingScope(${i})">×</button></span>`).join('');
}
window.removePendingScope = function (i) { PENDING_SCOPES.splice(i, 1); renderPendingScopes(); };

export async function loadUsers() {
  // Ensure schools are loaded for dropdowns
  if (!SCHOOLS.length) await loadSchools();
  let users = [], detail = [];
  try {
    [users, detail] = await Promise.all([
      api('/api/admin/users').then(r => r.ok ? r.json() : []),
      api('/api/admin/progress/detail').then(r => r.ok ? r.json() : []),
    ]);
  } catch (e) { }
  const detMap = Object.fromEntries(detail.map(d => [d.assignment_id, d]));
  const enriched = users.map(u => ({ ...u, ...(detMap[u.assignment_id] || {}) }));

  if (!enriched.length) { $('users-list').innerHTML = '<p class="no-data">No users yet.</p>'; return; }
  $('users-list').innerHTML = `<div class="users-list">${enriched.map(u => {
    const isAdmin = u.role === 'EXAM_ADMIN';
    const active = u.active !== false;
    const name = u.initials || u.admin_username || `user_${u.assignment_id}`;
    const roleCls = isAdmin ? 'role-admin' : (active ? 'role-de' : 'role-de inactive');
    const roleLabel = isAdmin ? 'Admin' : active ? 'Data Enterer' : 'Inactive';
    const worked = u.scopes_worked || [];
    return `<div class="user-card">
      <div class="user-card-header"><div><div class="user-initials">${esc(name)}</div>${u.full_name ? `<div class="user-fullname">${esc(u.full_name)}</div>` : ''}</div><span class="user-role-badge ${roleCls}">${roleLabel}</span></div>
      ${u.phone ? `<div class="user-phone muted small">📞 ${esc(u.phone)}</div>` : ''}
      <div class="user-stats"><span>Marks: <strong>${fmt(u.marks_entered || 0)}</strong></span><span>Today: <strong>${fmt(u.marks_today || 0)}</strong></span><span>Att: <strong>${fmt(u.attendance_entered || 0)}</strong></span><span>Last: <strong>${relTime(u.last_active)}</strong></span></div>
      ${(u.assignments || []).length ? `<div class="user-scopes-label">Assigned</div><div class="user-scope-chips">${(u.assignments || []).filter(a => a.centre_number).map(a => `<span class="user-scope-chip">${esc(a.centre_number)}</span>`).join('') || '<span class="muted small">All</span>'}</div>` : ''}
      ${!isAdmin && active ? `<div class="user-card-footer"><button class="btn-danger" onclick="deactivateUser(${u.id},'${esc(name)}')">Remove</button></div>` : ''}
    </div>`;
  }).join('')}</div>`;
}

window.deactivateUser = async function (id, name) {
  if (!confirm(`Remove account for ${name}?`)) return;
  const r = await jdel(`/api/admin/users/${id}`);
  if (r.ok) { setMsg('create-user-msg', `Account for ${name} removed.`, false); loadUsers(); }
  else { const d = await r.json().catch(() => ({})); setMsg('create-user-msg', d.detail || 'Failed.', true); }
};

export function initUsers() {
  $('open-create-user')?.addEventListener('click', () => {
    $('create-user-panel').hidden = false; $('open-create-user').hidden = true;
    setState.pendingScopes([]); populateScopeDropdowns(); renderPendingScopes();
  });
  $('cancel-create-user')?.addEventListener('click', () => {
    $('create-user-panel').hidden = true; $('open-create-user').hidden = false;
    setState.pendingScopes([]); setMsg('create-user-msg', '', false);
  });
  $('add-scope-btn')?.addEventListener('click', () => {
    const cn = ($('new-scope-school')?.value || '').trim();
    const sc = ($('new-scope-subject')?.value || '').trim();
    const pt = ($('new-scope-paper')?.value || '').trim();
    if (!cn && !sc && !pt) { setMsg('create-user-msg', 'Pick at least a school, subject or paper.', true); return; }
    PENDING_SCOPES.push({ centre_number: cn || null, subject_code: sc || null, paper_type: pt || null });
    setMsg('create-user-msg', '', false); renderPendingScopes();
  });
  $('create-user-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const first_name = ($('new-first-name')?.value || '').trim().toUpperCase();
    const surname = ($('new-surname')?.value || '').trim().toUpperCase();
    const initials = ($('new-initials')?.value || '').trim().toUpperCase();
    const pin = ($('new-pin')?.value || '').trim();
    if (!first_name) { setMsg('create-user-msg', 'First name required.', true); return; }
    if (!surname) { setMsg('create-user-msg', 'Surname required.', true); return; }
    if (!initials) { setMsg('create-user-msg', 'Initials required.', true); return; }
    if (pin.length < 4) { setMsg('create-user-msg', 'PIN must be at least 4 characters.', true); return; }
    const centre_numbers = PENDING_SCOPES.length ? [...new Set(PENDING_SCOPES.filter(s => s.centre_number).map(s => s.centre_number))] : null;
    const subject_codes = PENDING_SCOPES.length ? [...new Set(PENDING_SCOPES.filter(s => s.subject_code).map(s => s.subject_code))] : null;
    setMsg('create-user-msg', 'Creating…', false);
    const r = await jpost('/api/admin/users', { first_name, middle_name: ($('new-middle-name')?.value || '').trim().toUpperCase() || null, surname, phone: ($('new-phone')?.value || '').trim() || null, initials, pin, centre_numbers, subject_codes });
    if (r.ok) {
      setMsg('create-user-msg', 'Created.', false);
      ['new-first-name', 'new-middle-name', 'new-surname', 'new-phone', 'new-initials', 'new-pin'].forEach(id => { const el = $(id); if (el) el.value = ''; });
      setState.pendingScopes([]); $('create-user-panel').hidden = true; $('open-create-user').hidden = false; loadUsers();
    } else { const d = await r.json().catch(() => ({})); setMsg('create-user-msg', d.detail || 'Failed.', true); }
  });
}
