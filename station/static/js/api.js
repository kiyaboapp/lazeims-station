// api.js — fetch helpers and formatting utilities
export const $ = id => document.getElementById(id);
export const api = (url, o) => fetch(url, { credentials: 'same-origin', ...o });
export const jpost = (url, b) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
export const jput = (url, b) => api(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
export const jdel = url => api(url, { method: 'DELETE' });
export const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
export const fmt = n => Number(n || 0).toLocaleString();
export const pct = (a, b) => b > 0 ? Math.round(a / b * 100) : 0;

export function isValidMark(v) { if (v.trim() === '') return true; const n = Number(v); return Number.isFinite(n) && n >= 0; }

export function setMsg(id, txt, isErr) {
  const el = $(id); if (!el) return;
  el.textContent = txt;
  el.className = 'form-msg' + (isErr ? ' err' : txt ? ' ok' : '');
}

export function relTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso), now = new Date(), diff = Math.floor((now - d) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return d.toLocaleDateString();
}
