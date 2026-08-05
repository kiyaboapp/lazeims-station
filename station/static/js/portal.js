// portal.js — entry portal (scope selection table)
import { $, api, esc } from './api.js';
import { SCOPES, SCHOOLS, PORTAL_FILTER, setState } from './state.js';

let _searchTerm = '';

export async function loadPortal() {
  try {
    const scopes = await api('/api/scopes').then(r => r.json());
    setState.scopes(scopes);
    if (!SCHOOLS.length) {
      const schools = await api('/api/schools').then(r => r.json()).catch(() => []);
      setState.schools(schools);
    }
  } catch (e) { setState.scopes([]); }
  renderPortal();
}

export function renderPortal() {
  let list = SCOPES.filter(s =>
    PORTAL_FILTER === 'all' ||
    (PORTAL_FILTER === 'open' && !s.finalized) ||
    (PORTAL_FILTER === 'finalized' && s.finalized)
  );

  if (_searchTerm) {
    const q = _searchTerm.toLowerCase();
    list = list.filter(s =>
      (s.centre_number || '').toLowerCase().includes(q) ||
      (s.school_name || '').toLowerCase().includes(q) ||
      (s.subject_name || '').toLowerCase().includes(q) ||
      (s.subject_code || '').toLowerCase().includes(q)
    );
  }

  const el = $('portal-scope-list');
  if (!list.length) {
    el.innerHTML = `<div class="p-8 text-center text-gray-500 dark:text-gray-400">
      <p class="text-sm">${_searchTerm || PORTAL_FILTER !== 'all' ? 'No subject papers match your filter.' : 'No subject papers available.'}</p>
    </div>`;
    return;
  }

  el.innerHTML = `<table class="w-full text-sm">
    <thead>
      <tr class="border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50">
        <th class="text-left px-4 py-3 font-medium text-gray-600 dark:text-gray-400">Centre #</th>
        <th class="text-left px-4 py-3 font-medium text-gray-600 dark:text-gray-400">School</th>
        <th class="text-left px-4 py-3 font-medium text-gray-600 dark:text-gray-400">Subject</th>
        <th class="text-left px-4 py-3 font-medium text-gray-600 dark:text-gray-400">Paper</th>
        <th class="text-center px-4 py-3 font-medium text-gray-600 dark:text-gray-400">Status</th>
        <th class="px-4 py-3"></th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
      ${list.map(s => {
        const idx = SCOPES.indexOf(s);
        const locked = !s.finalized && s.lock_status === 'LOCKED';
        const paper = s.paper_type.replace('THEORY1', 'Theory 1').replace('THEORY2', 'Theory 2').replace('PRACTICAL', 'Practical');
        let statusClass, statusText;
        if (s.finalized) {
          statusClass = 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400';
          statusText = 'Finalized';
        } else if (locked) {
          statusClass = 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400';
          statusText = `In Use${s.lock_owner ? ' · ' + esc(s.lock_owner) : ''}`;
        } else {
          statusClass = 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400';
          statusText = 'Open';
        }
        const btn = (!s.finalized && !locked)
          ? `<button onclick="enterScope(${idx})" class="px-3 py-1.5 text-xs font-medium bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg transition-colors">Enter →</button>`
          : '';
        return `<tr class="hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors">
          <td class="px-4 py-3 font-mono text-xs text-gray-900 dark:text-white">${esc(s.centre_number)}</td>
          <td class="px-4 py-3 text-gray-900 dark:text-white">${esc(s.school_name || '')}</td>
          <td class="px-4 py-3 text-gray-900 dark:text-white font-medium">${esc(s.subject_name || s.subject_code)}</td>
          <td class="px-4 py-3 text-gray-600 dark:text-gray-400">${esc(paper)}</td>
          <td class="px-4 py-3 text-center"><span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusClass}">${statusText}</span></td>
          <td class="px-4 py-3 text-right">${btn}</td>
        </tr>`;
      }).join('')}
    </tbody>
  </table>`;
}

export function initPortal() {
  // Filter chips
  document.querySelectorAll('#portal-chips .chip').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('#portal-chips .chip').forEach(x => {
      x.className = 'chip px-3 py-2 rounded-lg text-sm font-medium bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700';
    });
    b.className = 'chip px-3 py-2 rounded-lg text-sm font-medium bg-indigo-600 text-white';
    setState.portalFilter(b.dataset.f);
    renderPortal();
  }));

  // Search
  const searchInput = $('portal-search');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      _searchTerm = e.target.value.trim();
      renderPortal();
    });
  }
}
