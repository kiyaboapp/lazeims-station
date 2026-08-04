// main.js — the only entry point
import { boot, initLogin } from './boot.js';
import { initTheme, initSidebar, showView } from './router.js';
import { initSchools, loadSchools } from './schools.js';
import { initScopes, loadScopesView } from './scopes.js';
import { initUsers, loadUsers } from './users.js';
import { initSettings, loadSettings } from './settings.js';
import { initAudit, loadAudit } from './audit.js';
import { initPortal, loadPortal } from './portal.js';
import { initEntry, enterScope } from './entry.js';
import { initFinalize } from './finalize.js';
import { loadDashboardDE, loadDashboardAdmin } from './dashboard.js';
import { SESSION, SCOPES } from './state.js';

// Wire sidebar nav
document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', async () => {
  const v = btn.dataset.view;
  if (v === 'dashboard') {
    if (SESSION?.role === 'EXAM_ADMIN') { loadDashboardAdmin(); showView('dashboard-admin'); }
    else { loadDashboardDE(); showView('dashboard-de'); }
  }
  else if (v === 'schools') { loadSchools(); showView('schools'); }
  else if (v === 'scopes') { loadScopesView(); showView('scopes'); }
  else if (v === 'users') { loadUsers(); showView('users'); }
  else if (v === 'settings') { loadSettings(); showView('settings'); }
  else if (v === 'audit') { loadAudit(); showView('audit'); }
  else if (v === 'entry-portal') {
    await loadPortal();
    const open = SCOPES.filter(s => !s.finalized && s.lock_status !== 'LOCKED');
    if (open.length === 1) { enterScope(SCOPES.indexOf(open[0])); }
    else { showView('entry-portal'); }
  }
}));

// Initialize all modules
initTheme();
initSidebar();
initLogin();
initSchools();
initScopes();
initUsers();
initSettings();
initAudit();
initPortal();
initEntry();
initFinalize();

// Boot
boot();
