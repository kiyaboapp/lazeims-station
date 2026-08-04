// idb.js — IndexedDB draft persistence helpers
import { CURRENT } from './state.js';

const idb = () => new Promise((res, rej) => {
  const r = indexedDB.open('lazeims_station', 3);
  r.onupgradeneeded = () => ['drafts', 'marks'].forEach(s => { if (!r.result.objectStoreNames.contains(s)) r.result.createObjectStore(s, { keyPath: 'k' }); });
  r.onsuccess = () => res(r.result);
  r.onerror = () => rej(r.error);
});

export const dbGet = (store, k) => idb().then(d => new Promise(res => { const t = d.transaction(store, 'readonly'), rq = t.objectStore(store).get(k); rq.onsuccess = () => res(rq.result ? rq.result.v : null); }));
export const dbSet = (store, k, v) => idb().then(d => new Promise(res => { const t = d.transaction(store, 'readwrite'); t.objectStore(store).put({ k, v }); t.oncomplete = res; }));
export const dbDel = (store, k) => idb().then(d => new Promise(res => { const t = d.transaction(store, 'readwrite'); t.objectStore(store).delete(k); t.oncomplete = res; }));
export const draftKey = () => CURRENT ? `${CURRENT.centre_number}|${CURRENT.subject_code}|${CURRENT.paper_type}` : '';
