// shared/nav.js -- injects sidebar + topbar into every page
import { api, jpost, esc } from '../js/api.js';

const NAV_ITEMS = [
  { href: '/', icon: 'dashboard', label: 'Dashboard', roles: ['EXAM_ADMIN', 'DATA_ENTERER'] },
  { href: '/entry', icon: 'edit', label: 'Marks Entry', roles: ['EXAM_ADMIN', 'DATA_ENTERER'] },
  { href: '/schools', icon: 'school', label: 'Schools', roles: ['EXAM_ADMIN'] },
  { href: '/scopes', icon: 'list', label: 'Scopes', roles: ['EXAM_ADMIN', 'DATA_ENTERER'] },
  { href: '/users', icon: 'users', label: 'Users', roles: ['EXAM_ADMIN'] },
  { href: '/reports', icon: 'chart', label: 'Reports', roles: ['EXAM_ADMIN'] },
  { href: '/settings', icon: 'settings', label: 'Settings', roles: ['EXAM_ADMIN'] },
];

const ICONS = {
  dashboard: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  edit: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>',
  school: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"/></svg>',
  list: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>',
  users: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"/></svg>',
  chart: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>',
  settings: '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><circle cx="12" cy="12" r="3"/></svg>',
};

let SESSION = null;

export async function initNav() {
  // Check auth
  try {
    const r = await api('/api/me');
    if (!r.ok) { window.location.href = '/'; return null; }
    SESSION = await r.json();
  } catch (e) {
    window.location.href = '/';
    return null;
  }

  const role = SESSION.role || '';
  const isAdmin = role === 'EXAM_ADMIN';
  const name = SESSION.username || SESSION.initials || '';
  const station = SESSION.station_code || '';

  // Filter nav items by role
  const visibleItems = NAV_ITEMS.filter(item => item.roles.includes(role));
  const currentPath = window.location.pathname;

  // Build sidebar HTML
  const sidebarHTML = `
    <aside id="sidebar" class="fixed inset-y-0 left-0 z-40 w-64 bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-700 transform transition-transform duration-200 lg:translate-x-0 -translate-x-full">
      <div class="flex flex-col h-full">
        <!-- Logo -->
        <div class="flex items-center gap-3 px-5 py-4 border-b border-gray-200 dark:border-gray-700">
          <div class="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-bold text-sm">L</div>
          <div>
            <h1 class="text-sm font-bold text-gray-900 dark:text-white">LAZEIMS Station</h1>
            <p class="text-xs text-gray-500 dark:text-gray-400">${esc(station)}</p>
          </div>
        </div>

        <!-- Nav items -->
        <nav class="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          ${visibleItems.map(item => {
            const active = currentPath === item.href || (item.href !== '/' && currentPath.startsWith(item.href));
            return `<a href="${item.href}" class="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${active ? 'bg-indigo-50 dark:bg-indigo-900/30 text-indigo-700 dark:text-indigo-300' : 'text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'}">
              ${ICONS[item.icon] || ''}
              <span>${item.label}</span>
            </a>`;
          }).join('')}
        </nav>

        <!-- Profile -->
        <div class="px-4 py-3 border-t border-gray-200 dark:border-gray-700">
          <div class="flex items-center gap-3">
            <div class="w-8 h-8 rounded-full bg-indigo-100 dark:bg-indigo-900 flex items-center justify-center text-indigo-700 dark:text-indigo-300 text-xs font-bold">${esc((name || '?').slice(0, 2).toUpperCase())}</div>
            <div class="flex-1 min-w-0">
              <p class="text-sm font-medium text-gray-900 dark:text-white truncate">${esc(name)}</p>
              <p class="text-xs text-gray-500 dark:text-gray-400">${isAdmin ? 'Admin' : 'Data Enterer'}</p>
            </div>
            <button id="logout-btn" title="Logout" class="p-1.5 rounded-lg text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/></svg>
            </button>
          </div>
        </div>
      </div>
    </aside>`;

  // Build topbar HTML
  const topbarHTML = `
    <header class="sticky top-0 z-30 bg-white/80 dark:bg-gray-900/80 backdrop-blur border-b border-gray-200 dark:border-gray-700 lg:ml-64">
      <div class="flex items-center justify-between px-4 py-3">
        <div class="flex items-center gap-3">
          <button id="sidebar-toggle" class="lg:hidden p-2 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/></svg>
          </button>
          <div id="page-title" class="text-lg font-semibold text-gray-900 dark:text-white"></div>
        </div>
        <div class="flex items-center gap-3">
          <span id="status-pill" class="hidden sm:inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"></span>
          <button id="theme-btn" class="p-2 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors" title="Toggle dark mode">
            <svg class="w-5 h-5 dark:hidden" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"/></svg>
            <svg class="w-5 h-5 hidden dark:block" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"/></svg>
          </button>
        </div>
      </div>
    </header>`;

  // Overlay for mobile
  const overlayHTML = '<div id="sidebar-overlay" class="fixed inset-0 z-30 bg-black/50 hidden lg:hidden"></div>';

  // Inject into page
  const app = document.getElementById('app');
  if (app) {
    app.insertAdjacentHTML('afterbegin', sidebarHTML + overlayHTML + topbarHTML);
  }

  // Wire events
  wireNavEvents();

  // Load status
  loadStatusPill();

  return SESSION;
}

function wireNavEvents() {
  // Sidebar toggle (mobile)
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');

  if (toggle && sidebar) {
    toggle.addEventListener('click', () => {
      sidebar.classList.toggle('-translate-x-full');
      overlay?.classList.toggle('hidden');
    });
  }
  if (overlay && sidebar) {
    overlay.addEventListener('click', () => {
      sidebar.classList.add('-translate-x-full');
      overlay.classList.add('hidden');
    });
  }

  // Theme toggle
  const themeBtn = document.getElementById('theme-btn');
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      document.documentElement.classList.toggle('dark');
      const isDark = document.documentElement.classList.contains('dark');
      try { localStorage.setItem('theme', isDark ? 'dark' : 'light'); } catch (e) {}
    });
  }

  // Logout
  const logoutBtn = document.getElementById('logout-btn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      await jpost('/api/logout', {});
      window.location.href = '/';
    });
  }
}

async function loadStatusPill() {
  try {
    const s = await api('/api/status').then(r => r.json());
    const pill = document.getElementById('status-pill');
    if (!pill) return;
    if (s.station_code) {
      pill.textContent = `${s.station_code} - v${s.software_version}`;
      pill.className = 'hidden sm:inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400';
    }
  } catch (e) {}
}

export function setPageTitle(title) {
  const el = document.getElementById('page-title');
  if (el) el.textContent = title;
  document.title = title + ' - LAZEIMS Station';
}

export function getSession() { return SESSION; }
