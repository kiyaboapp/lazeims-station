// state.js — the only shared mutable state; everything imports from here
export let SESSION = null;
export let SCOPES = [];
export let SCHOOLS = [];
export let CURRENT = null;
export let ROSTER = [];
export let ATT = {};
export let ATT_PERSISTED = {};
export let ATT_SAVING = {};
export let MARKS = {};
export let DEBOUNCE_T = null;
export let SCOPE_FILTER = 'all';
export let PORTAL_FILTER = 'all';
export let POLL_T = null;
export let SIDEBAR_OPEN = true;
export let PENDING_SCOPES = [];

export const setState = {
  session: v => { SESSION = v; },
  scopes: v => { SCOPES = v; },
  schools: v => { SCHOOLS = v; },
  current: v => { CURRENT = v; },
  roster: v => { ROSTER = v; },
  att: v => { ATT = v; },
  attPersisted: v => { ATT_PERSISTED = v; },
  attSaving: v => { ATT_SAVING = v; },
  marks: v => { MARKS = v; },
  debounceT: v => { DEBOUNCE_T = v; },
  scopeFilter: v => { SCOPE_FILTER = v; },
  portalFilter: v => { PORTAL_FILTER = v; },
  pollT: v => { POLL_T = v; },
  sidebarOpen: v => { SIDEBAR_OPEN = v; },
  pendingScopes: v => { PENDING_SCOPES = v; },
};
