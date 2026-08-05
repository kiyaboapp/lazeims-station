// router.js — showView(), sidebar wiring, theme toggle
import { $, api } from './api.js';
import { SESSION, SIDEBAR_OPEN, setState, SCOPES } from './state.js';

const VIEWS = ['login', 'dashboard-de', 'dashboard-admin', 'schools', 'scopes', 'users', 'settings', 'audit', 'entry-portal', 'entry'];

export function showView(name) {
  // Allow per-page override (multi-page mode: entry.html sets window.showView)
  if (window.showView && window.showView !== showView) { window.showView(name); return; }
  VIEWS.forEach(v => {
    const el = $('view-' + v);
    if (el) { el.hidden = (v !== name); el.classList.toggle('active', v === name); }
  });
  // Map dashboard to correct nav highlight
  const navName = (name === 'dashboard-de' || name === 'dashboard-admin') ? 'dashboard' : name;
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === navName));
  const noSide = name === 'login';
  const sidebar = $('sidebar');
  if (sidebar) sidebar.hidden = noSide;
  const mainArea = $('main-area');
  if (mainArea) mainArea.classList.toggle('no-sidebar', noSide);
  if (window.innerWidth < 769) {
    if (typeof setState !== 'undefined' && setState.sidebarOpen) setState.sidebarOpen(false);
    if (sidebar) sidebar.classList.remove('mobile-open');
    const ov = $('sidebar-overlay'); if (ov) ov.classList.remove('active');
  }
}

export function updateSidebarState() {
  const isDesktop = window.innerWidth >= 769;
  const sidebar = $('sidebar'), main = $('main-area'), overlay = $('sidebar-overlay');
  if (isDesktop) {
    sidebar.classList.toggle('collapsed', !SIDEBAR_OPEN);
    main.classList.toggle('sidebar-collapsed', !SIDEBAR_OPEN);
    sidebar.classList.remove('mobile-open');
    if (overlay) overlay.classList.remove('active');
  } else {
    sidebar.classList.toggle('mobile-open', SIDEBAR_OPEN);
    sidebar.classList.remove('collapsed');
    main.classList.remove('sidebar-collapsed');
    if (overlay) overlay.classList.toggle('active', SIDEBAR_OPEN);
  }
}

// Theme toggle
export function initTheme() {
  $('theme-btn').addEventListener('click', () => {
    const html = document.documentElement;
    const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) { }
  });
}

// Sidebar toggle button
export function initSidebar() {
  $('sidebar-toggle').addEventListener('click', () => {
    setState.sidebarOpen(!SIDEBAR_OPEN);
    updateSidebarState();
  });
  const overlay = $('sidebar-overlay');
  if (overlay) overlay.addEventListener('click', () => { setState.sidebarOpen(false); updateSidebarState(); });
  window.addEventListener('resize', updateSidebarState);
}

// Navigate to school view and scroll to specific school
window.goSchool = function (code) {
  import('./schools.js').then(m => {
    m.loadSchools().then(() => {
      showView('schools');
      document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === 'schools'));
      setTimeout(() => {
        const el = document.querySelector(`[data-school="${code}"]`);
        if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); el.click(); }
      }, 200);
    });
  });
};
