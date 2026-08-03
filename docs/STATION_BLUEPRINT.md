# LAZEIMS Station — Comprehensive Redesign Blueprint

**Date:** 2026-08-03  
**Status:** Authoritative design document. No code change is permitted to contradict this document without updating it first.  
**Scope:** `lazeims-station`, `lazeims-central-api`, `lazeims-common` (multi-repo changes required)  
**Audience:** Developers implementing the redesign. Read every section before touching code.

---

## Table of Contents

1. [Why This Document Exists — Root Problems](#1-why-this-document-exists--root-problems)
2. [Repository Map and Change Summary](#2-repository-map-and-change-summary)
3. [File Structure — Station Application](#3-file-structure--station-application)
4. [Role Model and Dashboard Separation](#4-role-model-and-dashboard-separation)
5. [UI Redesign — index.html and Dependencies](#5-ui-redesign--indexhtml-and-dependencies)
6. [Marks Entry — Tables, Stages, and CAL](#6-marks-entry--tables-stages-and-cal)
7. [Sync Architecture — Never Lose Data](#7-sync-architecture--never-lose-data)
8. [Conflict Resolution — Station Timestamp Wins](#8-conflict-resolution--station-timestamp-wins)
9. [Audit Trail and Marks Change History](#9-audit-trail-and-marks-change-history)
10. [Remote Data Pull — Download Without Login](#10-remote-data-pull--download-without-login)
11. [School Name Resolution — Fix the Missing Name Problem](#11-school-name-resolution--fix-the-missing-name-problem)
12. [Multi-Repo Schema Changes](#12-multi-repo-schema-changes)
13. [API Contract Changes](#13-api-contract-changes)
14. [Implementation Order](#14-implementation-order)

---

## 1. Why This Document Exists — Root Problems

The current station has several concrete defects that operators have hit in production. Each one is documented here so the fix is tied to the original pain.

### 1.1 School names missing in tables

`/api/scopes` returns `centre_number` only. The school name is in `SCHOOLS` state but only populated if `/api/schools` has already been called. The scope-selection table and the entry portal table therefore often show a bare centre number with no name. The fix is: school name must travel with every scope row from the server, not be looked up client-side from a cache that may be empty.

### 1.2 Centre-number dropdown in user creation is broken

When creating a Data Enterer, the school dropdown is populated from `SCHOOLS` which may be `[]` if the admin went directly to the Users view. The dropdown shows only centre numbers with no names, and may be completely empty. Fix: the Users view must load schools before rendering the create form, and every dropdown option must show `CENTRE_NUMBER — School Name`.

### 1.3 Sync failures silently lose data

When a network error hits mid-batch, events in state `SENDING` are correctly reverted to `PENDING`. However: (a) the browser gives no durable feedback about what was not sent, and (b) there is no way for an admin to download a portable outbox export as a fallback transport. Fix: add a portable export/import path and a visible sync-status panel.

### 1.4 Conflict resolution uses Central's receive timestamp

When Central receives an event from a station, `marks_apply.py` writes `entered_at = datetime.now(timezone.utc)` — the Central server time. If two stations sync the same student scope (a misconfiguration), Central's last-write-wins is based on the order events arrive at the server, not when they were entered on the station. Fix: the station's `occurred_at` from the outbox event must become the authoritative `entered_at` on both `total_marks` and `item_marks` tables, and conflict resolution at Central must use that field.

### 1.5 No marks change history

`total_marks` and `item_marks` are plain upsert tables. There is no record of what the mark was before a change, who changed it, or when it was changed on the station. A supervisor cannot reconstruct the sequence of edits for a disputed student. Fix: add a `marks_audit` table on both station (SQLite) and central (PostgreSQL) that records every replace operation as an immutable append.

### 1.6 No way to pull data from Central without a full login session

There is no endpoint for an admin at the station to download a snapshot of what Central already holds for this station's scopes. This is needed when a station has been rebuilt or when verifying that a previous sync actually landed. Fix: add a `GET /api/v1/station/pull/snapshot` endpoint on Central (authenticated by machine credential, same as sync) that returns the current state of marks, attendance, and finalized scopes for the station's exam.

### 1.7 Data Enterers and Station Admins see identical dashboards

A Data Enterer only needs: their own scope list, their own progress, the marks entry table. Showing them sync settings, user management, and station-wide KPIs is confusing. An admin needs the full picture. Fix: two distinct dashboard layouts, not a single layout with DOM elements toggled by `hidden`.

---

## 2. Repository Map and Change Summary

```
github.com:kiyaboapp/
├── lazeims-station          ← PRIMARY: UI redesign, sync, audit, pull
├── lazeims-central-api      ← REQUIRED: schema, conflict fix, pull endpoint
├── lazeims-common           ← REQUIRED: outbox event schema, conflict field
└── lazaims (frontend)       ← NOT TOUCHED by this blueprint
```

### Change summary per repo

| Repo | Change type | Sections |
|------|-------------|----------|
| `lazeims-station` | UI full redesign, new audit table, pull endpoint client, sync improvements | 3,4,5,6,7,8,9,10,11 |
| `lazeims-central-api` | Schema migration: `marks_audit`, `occurred_at` on marks, pull endpoint | 9,10,12,13 |
| `lazeims-common` | `OutboxEvent` schema: add `occurred_at` as required field; conflict resolution helpers | 8,13 |

**Commit order:** `lazeims-common` first → `lazeims-central-api` → `lazeims-station`. Never merge station changes before the common and central changes they depend on.

---

## 3. File Structure — Station Application

### 3.1 Current layout (what exists today)

```
lazeims-station/
├── station/
│   ├── static/
│   │   ├── index.html          ← single-file SPA shell
│   │   ├── app.js              ← ~52 KB, all logic in one file
│   │   └── app.css             ← ~33 KB design system
│   ├── main.py
│   ├── entry.py
│   ├── sync.py
│   ├── sync_http.py
│   ├── outbox.py
│   ├── finalize.py
│   ├── locking.py
│   ├── migrations.py
│   ├── auth.py
│   ├── config.py
│   ├── paths.py
│   ├── db.py
│   ├── backup.py
│   ├── auto_import.py
│   ├── package_import.py
│   └── machine_credential.py
├── docs/
│   └── IMPLEMENTATION_PLAN.md
├── tests/
├── launcher/
├── station_data/
├── pyproject.toml
└── requirements.lock
```

### 3.2 Target layout (after this blueprint)

```
lazeims-station/
├── station/
│   ├── static/
│   │   ├── index.html              ← SPA shell: all view HTML inline, ~400 lines
│   │   ├── app.css                 ← one file, all design tokens and component styles
│   │   └── js/                     ← ES modules, no bundler, no npm, no build step
│   │       ├── api.js              ← fetch helpers (api, jpost, jput, jdel, esc, fmt…)
│   │       ├── state.js            ← mutable globals (SESSION, SCOPES, SCHOOLS, CURRENT…)
│   │       ├── router.js           ← showView(), sidebar wiring, theme toggle
│   │       ├── idb.js              ← IndexedDB draft persistence helpers
│   │       ├── boot.js             ← boot(), login form, afterLogin(), logout
│   │       ├── dashboard.js        ← loadDashboardDE(), loadDashboardAdmin()
│   │       ├── schools.js          ← schools accordion view
│   │       ├── scopes.js           ← scopes list view + force-release
│   │       ├── users.js            ← users view + create DE form
│   │       ├── settings.js         ← sync/settings + pull snapshot + export/import ack
│   │       ├── audit.js            ← marks audit log view
│   │       ├── portal.js           ← entry portal (scope selection table)
│   │       ├── entry.js            ← attendance tab + marks tab (total + CAL)
│   │       ├── finalize.js         ← finalize button, completeness check, incidents
│   │       └── main.js             ← imports all modules, calls boot()
│   ├── main.py                     ← add: /api/pull/snapshot, /api/audit/marks, etc.
│   ├── entry.py                    ← edit: write marks_audit row on every replace
│   ├── sync.py                     ← edit: pass occurred_at in event envelope
│   ├── sync_http.py                ← edit: pull snapshot + export/import ack client
│   ├── outbox.py                   ← edit: occurred_at always from domain write time
│   ├── finalize.py                 ← unchanged
│   ├── locking.py                  ← unchanged
│   ├── migrations.py               ← edit: marks_audit (schema v2), startup revert (v3)
│   ├── auth.py                     ← unchanged
│   ├── config.py                   ← unchanged
│   ├── paths.py                    ← unchanged
│   ├── db.py                       ← unchanged
│   ├── backup.py                   ← unchanged
│   ├── auto_import.py              ← unchanged
│   ├── package_import.py           ← unchanged
│   └── machine_credential.py       ← unchanged
├── docs/
│   ├── IMPLEMENTATION_PLAN.md
│   └── STATION_BLUEPRINT.md        ← this file
├── tests/
│   ├── test_marks_audit.py         ← new
│   ├── test_pull_snapshot.py       ← new
│   └── … existing tests
├── launcher/
├── station_data/
├── pyproject.toml
└── requirements.lock
```

### 3.3 ES Module wiring

`index.html` loads a single script tag:

```html
<script type="module" src="/static/js/main.js"></script>
```

`main.js` imports every other module. The browser resolves `/static/js/api.js` etc. from the FastAPI `StaticFiles` mount. On first load all JS files are fetched in parallel. After that they are in the browser cache — **zero bytes transferred on view switches.**

```javascript
// main.js — the only entry point
import { boot }    from './boot.js';
import './router.js';    // wires sidebar clicks on import
import './entry.js';     // wires tab bar on import
import './finalize.js';  // wires finalize/check buttons on import

boot();                  // called exactly once at page load
```

Each module exports only what other modules need. State lives in `state.js` as plain exported variables — no framework, no reactivity overhead, fully debuggable in DevTools.

```javascript
// state.js — the only shared mutable state; everything imports from here
export let SESSION = null;
export let SCOPES  = [];
export let SCHOOLS = [];
export let CURRENT = null;   // active scope during entry
export let ROSTER  = [];
export let ATT     = {};
export let MARKS   = {};

export const setState = {
  session: v  => { SESSION = v; },
  scopes:  v  => { SCOPES  = v; },
  schools: v  => { SCHOOLS = v; },
  current: v  => { CURRENT = v; },
  roster:  v  => { ROSTER  = v; },
  att:     v  => { ATT     = v; },
  marks:   v  => { MARKS   = v; },
};
```

### 3.4 Performance on offline/low-spec machines

| Concern | Design choice | Result |
|---------|--------------|--------|
| Initial cold-cache load | ~14 small JS files, 1 CSS, 1 HTML — all localhost | < 200 ms total |
| View switch | Pure DOM manipulation, zero HTTP | < 5 ms |
| Data refresh | One `/api/*` call per action or 30 s poll | Only Python is hit |
| Memory footprint | No virtual DOM, no framework runtime | ~2 MB for 500-student station |
| Python process load | Static files served by Starlette's `StaticFiles` (anyio file send, bypasses Python app code) | Python only handles `/api/*` |
| Cache between sessions | Browser caches CSS + all JS modules; only `index.html` re-validates | Near-zero load on reconnect |
| Offline resilience | All JS already in browser after first load | View switches work even if FastAPI is briefly unreachable |

### 3.5 Static serving in `main.py` — no changes needed

```python
# Already in main.py — keep exactly as-is:
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_STATIC / "index.html")

if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
```

`StaticFiles` handles the `js/` subdirectory automatically — any file under `static/` is served by path. No extra configuration needed. The ES module `import './api.js'` resolves to `GET /static/js/api.js` in the browser.

> **Architecture decision — Plain static files. No Jinja2. No build step. Ever.**
>
> **Why not Jinja2:** FastAPI/Starlette includes Jinja2, so it is technically free. But server-side rendering means every view transition re-hits the Python process. On a low-spec station computer (Windows 10, 4 GB RAM, shared CPU) the Python process is the bottleneck. A pure client-side SPA hits the server only for `/api/*` data calls. After the first page load the browser has everything in memory — view switches are instant, zero network round trips. This is measurably faster offline.
>
> **Why not fetched HTML fragments:** Fetching `views/login.html`, injecting into the DOM, then fetching `views/entry.html` on transition creates a waterfall of HTTP requests *and* makes every view unavailable when the server is slow to respond. All view HTML stays inline in `index.html`. The file is large, but it is loaded once and cached by the browser.
>
> **Why split the JS:** `app.js` is already 52 KB and growing. A 52 KB file with no module boundaries is impossible to maintain. The solution is **ES Modules** (`<script type="module">`). Native browser ES modules work in Chrome 61+, Firefox 60+, Edge 79+ — all modern browsers. No bundler, no npm, no build step. The browser resolves relative imports from `/static/js/` directly.
>
> **The split:** `index.html` keeps all view HTML inline (one file, clear comment banners). `app.css` stays as one file (CSS has no import performance penalty at this scale). JavaScript is split into focused modules under `static/js/`. Each module imports only what it needs from `api.js` and `state.js`.

---

## 4. Role Model and Dashboard Separation

### 4.1 The two roles

| Role | Login | What they can do |
|------|-------|-----------------|
| `DATA_ENTERER` | Initials + PIN | Enter attendance and marks for assigned scopes only. View own progress. |
| `EXAM_ADMIN` | Username + Password | Everything a DE can do, plus: manage users, view all scopes/schools, configure and run sync, import packages, view audit trail, pull snapshot from Central, force-release locks. |

There is no third role. There is no "read-only" role. Admins are also Data Enterers and can enter marks for any scope.

### 4.2 Why two separate dashboard designs, not DOM toggling

The current code renders one dashboard and toggles `hidden` on admin-only elements. This is fragile (easy to forget `hidden` on a new element), confusing (DEs see grey-out empty panels), and makes the HTML hard to reason about.

The redesign uses **two completely separate `<section>` elements** for the two dashboards:

```html
<section id="view-dashboard-de"   class="view" hidden>  <!-- DATA_ENTERER -->
<section id="view-dashboard-admin" class="view" hidden>  <!-- EXAM_ADMIN   -->
```

The `afterLogin()` function shows exactly one based on `SESSION.role`. No CSS class tricks, no hidden panels — the other section simply does not exist in the viewport.

### 4.3 Data Enterer dashboard layout

```
┌─────────────────────────────────────────────────────────────┐
│ TOPBAR: [☰] LAZEIMS Station · [station-pill]   [DE·AB] [↪] │
├──────────────┬──────────────────────────────────────────────┤
│              │  ┌─ My Progress ──────────────────────────┐  │
│  SIDEBAR     │  │  Marks today: 42  │  Total: 310        │  │
│              │  │  Attendance: 67   │  Scopes done: 3/8  │  │
│  ▸ My Work   │  │  ████████████░░░░  68%                 │  │
│              │  └────────────────────────────────────────┘  │
│  ▸ Enter     │                                               │
│    Marks  ◄  │  ┌─ My Scopes ─────────────────────────────┐ │
│              │  │ Centre  │ School Name  │ Subject │ Paper  │ │
│  ──────────  │  │ S0104   │ Mwanza Sec  │ MATH    │ T1  →  │ │
│  [AB]        │  │ S0104   │ Mwanza Sec  │ MATH    │ T2  →  │ │
│  Data Enterer│  │ S0107   │ Nyakato Sec │ BIO     │ T1 ✓  │ │
│              │  └────────────────────────────────────────┘  │
└──────────────┴──────────────────────────────────────────────┘
```

The DE dashboard has:
- **My Progress card**: marks today, total marks, attendance records, scopes assigned vs done.
- **My Scopes table**: only the scopes assigned to this DE, with school names, paper types, status badges, and a direct "Enter →" button. Clicking the row opens entry directly — no intermediate portal page for DEs with scopes.
- **No KPI grid** showing station-wide totals.
- **No sync panel**, no user management link, no settings.
- Sidebar has only: "My Work" (dashboard) and "Enter Marks".

### 4.4 Station Admin (EXAM_ADMIN) dashboard layout

```
┌──────────────────────────────────────────────────────────────────┐
│ TOPBAR: [☰] LAZEIMS Station · [station-pill]  [Admin·MWANZA] [↪]│
├───────────────┬──────────────────────────────────────────────────┤
│               │  ┌─ KPIs ──────────────────────────────────────┐ │
│  SIDEBAR      │  │ 12/40      │ 1,842    │ 38    │ 0  │  30%   │ │
│               │  │ Scopes fin │ Marks    │ Today │ Rej│ Done   │ │
│  ▸ Dashboard  │  └─────────────────────────────────────────────┘ │
│  ▸ Schools    │                                                   │
│  ▸ Scopes     │  ┌─ Sync Status ──────┐ ┌─ Today's Activity ──┐ │
│  ▸ Users      │  │ ✓ Connected        │ │ 38 marks entered    │ │
│  ▸ Sync/      │  │ lazeims.online     │ │ 1,842 total in DB   │ │
│    Settings   │  │ 5 events pending   │ │ 450 students        │ │
│  ──────────── │  └────────────────────┘ └─────────────────────┘ │
│  ▸ Enter      │                                                   │
│    Marks      │  ┌─ Schools at a Glance ──────────────────────┐  │
│               │  │ Centre │ School Name        │ Scopes │ Pct  │  │
│  ──────────── │  │ S0104  │ Mwanza Secondary   │ 3/8    │  37% │  │
│  [MW]         │  │ S0107  │ Nyakato Secondary  │ 8/8    │ 100% │  │
│  Admin        │  │ S0112  │ Bugarika Secondary │ 0/4    │   0% │  │
│               │  └────────────────────────────────────────────┘  │
└───────────────┴──────────────────────────────────────────────────┘
```

The admin dashboard has:
- **KPI grid**: scopes finalized, total marks, marks today, rejected events, overall completion %.
- **Sync Status card**: URL, pending count, last sync time, quick "Sync Now" button.
- **Today's Activity card**: station-wide activity numbers.
- **Schools at a Glance table**: every school, with name, scope progress, clickable rows.
- **Data Enterers Progress card**: per-DE marks/attendance/last-active, visible without leaving dashboard.
- Sidebar has all nav items including Users, Sync/Settings, Schools, Scopes.

---

## 5. UI Redesign — index.html and Dependencies

### 5.1 HTML structure

The file remains a single `index.html` (no Jinja, no build step). The structure is reorganised into clearly delimited regions with HTML comments as section markers. Every section has a unique `id`. No inline `style=""` attributes except for computed values (progress bar widths).

```html
<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
  <!-- theme flash prevention, CSS link -->
</head>
<body>

<!-- ═══ TOPBAR ════════════════════════════════════════════════════ -->
<header class="topbar"> ... </header>

<div class="app-shell">

  <!-- ═══ SIDEBAR ══════════════════════════════════════════════════ -->
  <nav class="sidebar" id="sidebar" hidden> ... </nav>
  <div class="sidebar-overlay" id="sidebar-overlay"></div>

  <!-- ═══ MAIN CONTENT ═════════════════════════════════════════════ -->
  <main class="main-area" id="main-area">

    <!-- LOGIN ──────────────────────────────────────────────────── -->
    <section id="view-login" class="view active"> ... </section>

    <!-- DASHBOARD — DATA ENTERER ───────────────────────────────── -->
    <section id="view-dashboard-de" class="view" hidden> ... </section>

    <!-- DASHBOARD — ADMIN ──────────────────────────────────────── -->
    <section id="view-dashboard-admin" class="view" hidden> ... </section>

    <!-- SCHOOLS (admin only) ───────────────────────────────────── -->
    <section id="view-schools" class="view" hidden> ... </section>

    <!-- SCOPES (admin only) ────────────────────────────────────── -->
    <section id="view-scopes" class="view" hidden> ... </section>

    <!-- USERS (admin only) ─────────────────────────────────────── -->
    <section id="view-users" class="view" hidden> ... </section>

    <!-- SYNC / SETTINGS (admin only) ───────────────────────────── -->
    <section id="view-settings" class="view" hidden> ... </section>

    <!-- MARKS AUDIT (admin only) ───────────────────────────────── -->
    <section id="view-audit" class="view" hidden> ... </section>

    <!-- ENTRY PORTAL — scope selection ─────────────────────────── -->
    <section id="view-entry-portal" class="view" hidden> ... </section>

    <!-- DATA ENTRY — attendance + marks ────────────────────────── -->
    <section id="view-entry" class="view" hidden> ... </section>

  </main>
</div>

<script src="/static/app.js"></script>
</body>
</html>
```

### 5.2 Sidebar design — role-aware

The sidebar items are rendered once and shown/hidden based on role, but the structure is split into a DE section and an admin section rather than individual `hidden` attributes on each button.

```html
<nav class="sidebar" id="sidebar" hidden>

  <!-- shared: always visible after login -->
  <button class="nav-item" data-view="dashboard">Dashboard</button>

  <!-- admin-only group -->
  <div class="nav-group admin-only" hidden>
    <button class="nav-item" data-view="schools">Schools</button>
    <button class="nav-item" data-view="scopes">All Scopes</button>
    <button class="nav-item" data-view="users">Users</button>
    <button class="nav-item" data-view="settings">Sync / Settings</button>
    <button class="nav-item" data-view="audit">Marks Audit</button>
  </div>

  <div class="nav-divider"></div>

  <!-- shared: visible to all roles -->
  <button class="nav-item nav-entry" data-view="entry-portal">Enter Marks</button>

  <!-- sidebar profile card -->
  <div class="sidebar-profile" id="sidebar-profile" hidden> ... </div>
</nav>
```

`afterLogin()` calls `document.querySelector('.admin-only').hidden = !isAdmin` — one operation, not many.

### 5.3 Login form changes

Current login attempts both `/api/login/de` and `/api/login/admin` in parallel. This is correct behaviour but the hint text is confusing. The new hint is:

```
Data Enterer: use your Initials and PIN
Station Admin: use your Username and Password
```

The login form adds a visible role indicator that updates as the user types (if the input matches known initials format vs username format). This is cosmetic only — the actual auth attempt is identical.

### 5.4 Status pill in topbar

The pill currently shows `station_code · N students · vX.Y.Z`. Add sync state:

```
S0104-MWZ · 450 students · v1.2.0 · ✓ synced        (green — all sent)
S0104-MWZ · 450 students · v1.2.0 · ⏳ 5 pending    (amber — outbox non-empty)
S0104-MWZ · 450 students · v1.2.0 · ✗ 2 rejected    (red — rejected events)
```

The pill is refreshed every 30 seconds alongside the dashboard poll.

### 5.5 Module responsibilities reference

| File | Exports | DOM it owns |
|------|---------|-------------|
| `api.js` | `api`, `jpost`, `jput`, `jdel`, `esc`, `fmt`, `pct`, `relTime` | none |
| `state.js` | `SESSION`, `SCOPES`, `SCHOOLS`, `CURRENT`, `ROSTER`, `ATT`, `MARKS`, `setState` | none |
| `idb.js` | `dbGet`, `dbSet`, `dbDel`, `draftKey` | none |
| `router.js` | `showView`, `goSchool` | `sidebar`, `main-area`, all `.nav-item` buttons |
| `boot.js` | `boot`, `afterLogin` | `login-form`, `logout-btn`, `who-label`, `status-pill`, `sidebar-profile` |
| `dashboard.js` | `loadDashboardDE`, `loadDashboardAdmin` | `view-dashboard-de`, `view-dashboard-admin` |
| `schools.js` | `loadSchools`, `renderSchools` | `view-schools`, `schools-list` |
| `scopes.js` | `loadScopesView`, `renderScopesView` | `view-scopes`, `scopes-list` |
| `users.js` | `loadUsers` | `view-users`, `users-list`, `create-user-form` |
| `settings.js` | `loadSettings`, `loadSyncConfig`, `loadPullSnapshot` | `view-settings` |
| `audit.js` | `loadAudit`, `renderAuditTable` | `view-audit` |
| `portal.js` | `loadPortal`, `renderPortal` | `view-entry-portal`, `portal-scope-list` |
| `entry.js` | `enterScope`, `loadRoster`, `renderAttTable`, `renderMarksTable` | `view-entry`, `att-tbody`, `marks-tbody` |
| `finalize.js` | `checkCompleteness`, `finalizeScope` | `finalize-btn`, `check-completeness`, `marks-validation` |
| `main.js` | — | imports all, calls `boot()` |

No module may `import` from a module that is further down this table except `api.js` and `state.js` (which any module may import). This prevents circular dependencies.

---

## 6. Marks Entry — Tables, Stages, and CAL

### 6.1 The three stages of entry for a scope

Every scope (school + subject + paper) goes through exactly three stages. The UI must make the current stage unmistakably clear and prevent skipping.

```
┌─────────────────────────────────────────────────────────────────┐
│  SCOPE:  S0104 · Mwanza Secondary · MATHEMATICS · THEORY 1      │
│  Stage:  [① Attendance] ──▶ [② Marks] ──▶ [③ Finalize]         │
│           CURRENT                                                │
└─────────────────────────────────────────────────────────────────┘
```

**Stage 1 — Attendance (ISAL transcription)**  
Mark every student present (P) or absent (A) from the ISAL register. Every toggle is saved immediately to the server. A "Save & continue to Marks →" button confirms all attendance before enabling stage 2. The button is disabled if any student has no attendance record at all (i.e., neither P nor A has been saved for them).

**Stage 2 — Marks entry**  
Enter the total mark (or per-question marks for CAL subjects) for every **present** student. Absent students show a greyed `—` row, not an input. The marks input auto-saves on debounce (800 ms after last keystroke) and on blur. A "Check completeness" button runs the server validation and highlights any present student with no mark.

**Stage 3 — Finalize**  
Available only when the server confirms all present students have marks and no open incidents. Clicking "Finalize scope" commits the scope. This cannot be undone from the station. The finalized scope event is pushed to the outbox immediately.

### 6.2 Attendance table

```
┌───┬────────────┬────────────────────────────┬───────────┐
│ # │ Student ID │ Full Name                  │ Att       │
├───┼────────────┼────────────────────────────┼───────────┤
│ 1 │ S0104/0001 │ JOHN PAUL MWANZA           │ [P ●   A] │
│ 2 │ S0104/0002 │ GRACE NYAKATO              │ [P   ● A] │ ← absent, row tinted red
│ 3 │ S0104/0003 │ IBRAHIM HASSAN SALUM       │ [P ●   A] │
└───┴────────────┴────────────────────────────┴───────────┘
  Present: 24 / 25    1 absent    [Mark all present]
```

Rules:
- Full name is always shown as the computed `full_name` from the students table (already uppercased by DB). Never truncate it.
- Student ID is monospaced.
- The P/A toggle is a custom switch: left side = P (green), right side = A (red), knob slides. Keyboard: `P` key = present, `A` = absent, `Space`/`Enter` = toggle, arrows = navigate rows.
- Absent rows have a faint red background.
- A spinning dot appears next to the toggle while the save HTTP request is in flight.
- The "Mark all present" button sets all students to present and fires all saves in the background.

### 6.3 Marks table — TOTAL_MARKS mode

```
┌───┬────────────┬────────────────────────┬──────┬──────────────┬────────┐
│ # │ Student ID │ Full Name              │ Att  │ Total Marks  │ Status │
├───┼────────────┼────────────────────────┼──────┼──────────────┼────────┤
│ 1 │ S0104/0001 │ JOHN PAUL MWANZA       │  P   │  [  87.5   ] │   ✓    │
│ 2 │ S0104/0002 │ GRACE NYAKATO          │  A   │      —       │        │
│ 3 │ S0104/0003 │ IBRAHIM HASSAN SALUM   │  P   │  [        ] │        │
│ 4 │ S0104/0004 │ AMINA SAID JUMBE       │  P   │  [  94     ] │ Unsaved│
└───┴────────────┴────────────────────────┴──────┴──────────────┴────────┘
  38 / 42 marks entered    4 absent (skip)    [Unsaved: 1]   [Check completeness]
```

Rules:
- Absent students: marks cell shows `—`, no input, no status. Their row is visually subdued.
- Status column values: empty (no mark yet), `Saving…` (request in flight), `✓` (saved), `Unsaved` (dirty/debounced), `Failed` (server error).
- Max-marks hint shown in the column header: `Total Marks (max 100)`.
- Input accepts decimals. Invalid input (letters, negative) turns input border red immediately without waiting for save.
- `Enter` key moves to the next present student's input.
- `PageDown` / `PageUp` jump between present students.
- The "Check completeness" button flushes all pending saves, then calls `/api/scopes/validation` and highlights any present student without a mark as a clickable chip.

### 6.4 Marks table — ITEM_LEVEL mode (CAL / per-question)

When a subject has questions configured (the `questions` table has rows for this subject+paper), the marks table shows one column per question. This is the CAL (Continuous Assessment Log) mode.

```
┌───┬────────────┬──────────────────────┬──────┬──────┬──────┬──────┬──────┬────────┬────────┐
│ # │ Student ID │ Full Name            │ Att  │ Q1   │ Q2   │ Q3   │ Q4   │ Total  │ Status │
│   │            │                      │      │ /20  │ /20  │ /30  │ /30  │ /100   │        │
├───┼────────────┼──────────────────────┼──────┼──────┼──────┼──────┼──────┼────────┼────────┤
│ 1 │ S0104/0001 │ JOHN PAUL MWANZA     │  P   │ [18] │ [16] │ [28] │ [25] │  87.0  │   ✓    │
│ 2 │ S0104/0002 │ GRACE NYAKATO        │  A   │  —   │  —   │  —   │  —   │   —    │        │
│ 3 │ S0104/0003 │ IBRAHIM HASSAN SALUM │  P   │ [  ] │ [  ] │ [  ] │ [  ] │   —    │        │
└───┴────────────┴──────────────────────┴──────┴──────┴──────┴──────┴──────┴────────┴────────┘
```

Rules:
- The `Total` column is **computed client-side** as the sum of entered question marks. It is read-only. It is never sent to the server — only the individual question marks are sent.
- When all questions for a student are filled, the row automatically saves (same 800 ms debounce per-student, not per-question).
- The save payload is `mode: ITEM_LEVEL`, `items: { "Q1": 18, "Q2": 16, ... }`.
- Questions that belong to a group (`group_code` set) show the group header row spanning those columns.
- `Tab` navigates left-to-right across questions, then down to the next student.
- If a question has `max_marks = 0`, it is hidden (it's a structural placeholder).
- The column header shows `Q{n} /{max}` where max comes from `questions.max_marks`.

### 6.5 CAL group headers

For subjects with question groups (e.g. "Section A", "Section B"), a header row spans the group's columns:

```
┌──────────────── Section A (answer any 3) ────────────────┬─── Section B ───┐
│  Q1/20  │  Q2/20  │  Q3/20  │  Q4/20  │  Q5/20           │  Q6/30  │ Q7/30 │
```

The `pick_count` field from `question_groups` drives the "(answer any N)" label.

### 6.6 Entry bar (top of the entry view)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ← Back  │  S0104 · Mwanza Secondary · MATHEMATICS · T1  │  P:24/25 M:38/42  │
│          │  Loading…                                      │  ✓ All saved      │
│          │                                                │  [Finalize scope] │
└──────────────────────────────────────────────────────────────────────────────┘
```

The entry bar shows:
- Back button (releases the lock, returns to portal).
- Title: `CENTRE_NUMBER · School Name · Subject Name · Paper`. School name and subject name are loaded with the scope data. **Never show just the centre number.**
- Status bar: present count / total, marks entered / present, save state.
- Finalize button: enabled only when all present students have marks (checked client-side; server is final arbiter).

### 6.7 Stage progress indicator

Between the entry bar and the tab bar, a three-stage progress strip:

```
① Attendance  ──────  ② Marks  ──────  ③ Finalize
   DONE                 CURRENT          LOCKED
```

Stages are coloured: DONE=green, CURRENT=blue, LOCKED=grey. Clicking a done stage switches the tab.

---

## 7. Sync Architecture — Never Lose Data

### 7.1 The core guarantee

> **Every committed domain write has a corresponding outbox event in the same SQLite transaction. A crash, power cut, or network failure can never leave marks saved without a sync event, or a sync event without its domain row.**

This is already implemented in `entry.py` via `with transaction(conn):` blocks that write both the domain row and the `outbox_events` row atomically. This guarantee must never be broken. Any future domain write MUST follow this pattern.

### 7.2 Outbox state machine

```
                    station writes
                         │
                         ▼
                    ┌─────────┐
                    │ PENDING │  ◄─────────────────────────────┐
                    └────┬────┘                                │
                         │  sync run starts                    │ network error
                         ▼                                     │ or server 5xx
                    ┌─────────┐                                │
                    │ SENDING │ ─────────────────────────────► │
                    └────┬────┘
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
        ┌──────────┐          ┌──────────┐
        │ ACCEPTED │          │ REJECTED │  ← never blocks other events
        └──────────┘          └──────────┘
```

**Transition rules:**
- `PENDING → SENDING`: batch start. Written inside a transaction so if the app crashes before the HTTP call, events remain `SENDING` on restart. The startup sequence must revert orphan `SENDING` events back to `PENDING` (see §7.3).
- `SENDING → ACCEPTED`: Central returned the event in `accepted` or `duplicates` list.
- `SENDING → REJECTED`: Central returned the event in `rejected` list.
- `SENDING → PENDING` (revert): network/server error; `attempts++`, `last_error` set.
- `REJECTED → PENDING`: admin presses "Retry rejected". Happens via `outbox.retry_rejected()`.

**REJECTED events never block other events.** The sync loop processes all `PENDING` events regardless of whether some previous events for the same scope are `REJECTED`. Rejected events stay visible and auditable.

### 7.3 Startup SENDING cleanup

Add to `apply_migrations()` (runs at every startup):

```python
# Revert any SENDING events left by a previous crash
conn.execute(
    "UPDATE outbox_events SET status='PENDING', last_error='reverted_from_sending_at_startup'"
    " WHERE status='SENDING'"
)
conn.commit()
```

This is already safe because: if the HTTP call actually succeeded before the crash, Central will return those events as `duplicates` on the next sync, which counts as `ACCEPTED`. No data is double-written.

### 7.4 Autosync

The `STATION_AUTOSYNC_SECONDS` environment variable (already implemented) drives a background asyncio loop. In the default package, this should be set to `120` (2 minutes). The launcher `start.sh` / `start.ps1` must export this variable.

### 7.5 Sync now — UI feedback requirements

The current "Sync now" button shows a result string but does not tell the user what happened visually in a durable way. The new design:

```
┌─ Sync to Central ──────────────────────────────────────────────┐
│  ✓ Ready  ·  lazeims.online:10048                              │
│                                                                │
│  Last sync:  2026-08-03 10:45:32 UTC  (8 minutes ago)         │
│  Accepted:   342 events                                        │
│  Rejected:    0 events                                         │
│  Pending:     0 events                                         │
│                                                                │
│  [Sync Now]   [Export outbox as .zip]   [Import ack .zip]      │
└────────────────────────────────────────────────────────────────┘
```

- "Last sync" is stored in `station_meta` with key `last_sync_at` on every successful `run_http_sync` call.
- The accepted/rejected/pending counts come from `/api/progress` (already available).
- "Export outbox as .zip" and "Import ack .zip" are the portable path (§7.6).

### 7.6 Portable outbox transport (removable-media fallback)

When the station has no internet, the admin can:

1. Click **"Export outbox as .zip"** → downloads a signed ZIP containing all `PENDING` events sealed as a portable envelope (the `export_pending_envelope()` function in `sync.py` already exists; this adds a download endpoint).
2. Carry the USB drive to a computer with internet access.
3. Upload the ZIP to Central via a web form (or a new Central endpoint).
4. Central processes the events and returns an ACK ZIP.
5. Bring the ACK ZIP back to the station and click **"Import ack .zip"** → the station applies the ACK and marks events as `ACCEPTED`/`REJECTED`.

**New station endpoints:**

```
GET  /api/sync/export-outbox
     → 200 application/zip  (sealed portable envelope)
     → 204 No Content       (nothing pending)

POST /api/sync/import-ack
     body: multipart/form-data  file=<ack.zip>
     → 200 { accepted: N, rejected: M, duplicates: K }
```

**New central endpoint (§13):**

```
POST /api/v1/station/sync/portable-events
     body: application/zip  (the exported envelope)
     auth: X-Package-Credential-Id + X-Package-Secret
     → 200 application/zip  (ack envelope)
```

### 7.7 Sync config display — no raw secrets in UI

The sync settings panel must never display the machine credential secret. It shows only:
- Central URL (editable by admin).
- Credential ID (read-only, shows first 8 chars + `…`).
- Credential status: `✓ Present` or `✗ Missing (re-import package)`.

---

## 8. Conflict Resolution — Station Timestamp Wins

### 8.1 The problem

In `marks_apply.py` (central):
```python
entered_at = datetime.now(timezone.utc)   # ← WRONG: Central's receive time
```

In `station_sync.py` (central), `_apply_event` calls `apply_student_paper_marks` with `actor_id=None`. The station's `occurred_at` timestamp from the outbox event is available in the event dict but is never passed through to the mark row.

**Consequence:** If station A syncs on Monday and station B syncs on Tuesday (both wrote the same student's marks — a misconfiguration), Tuesday wins even if station A entered the correct mark on Sunday and station B entered a wrong mark on Monday morning.

### 8.2 The fix

**In `lazeims-common`:** Add `occurred_at: str` (ISO 8601 UTC) as a required field in `OutboxEvent`. The station already writes `occurred_at` in `outbox.add_event()` — expose it in the event envelope.

**In `lazeims-station` `outbox.py`:** Verify `occurred_at` is always set to the domain write timestamp (the moment the user clicked Save), not the current time at sync. The `occurred_at` in `add_event()` should be the `_now()` of the transaction, which is correct today — just make it explicit that it must never be replaced by the transport layer.

**In `lazeims-central-api` `station_sync.py`:** Pass `occurred_at` from the event dict into `apply_student_paper_marks`:

```python
# In _apply_event, for STUDENT_PAPER_MARKS_REPLACED:
station_occurred_at = event.get("occurred_at")
await apply_student_paper_marks(
    db, scope=scope, paper_type=paper, mode=mode,
    total_marks_obtained=..., items=...,
    actor_id=None,
    station_occurred_at=station_occurred_at,   # ← new param
)
```

**In `marks_apply.py`:** Accept and use it:

```python
async def apply_student_paper_marks(
    db, *, scope, paper_type, mode,
    total_marks_obtained=None, items=None,
    actor_id=None,
    station_occurred_at: str | None = None,   # ← new
) -> dict:
    ...
    # Determine the authoritative entered_at
    if station_occurred_at:
        try:
            entered_at = datetime.fromisoformat(station_occurred_at)
        except ValueError:
            entered_at = datetime.now(timezone.utc)
    else:
        entered_at = datetime.now(timezone.utc)
```

### 8.3 Conflict resolution policy

When two events for the same natural key (same student + subject + paper) arrive, the later-**station**-timestamp wins. This is enforced by Central's existing `SyncEventReceipt` deduplication — the second event for the same `event_id` is a duplicate. The conflict case is different: two *different* events from two different stations for the same scope.

Policy:

> If a scope is simultaneously assigned to two stations (misconfiguration), the station whose `occurred_at` is **later** wins the final mark. Central writes a `CONFLICT_DETECTED` audit record identifying both event IDs, both station codes, and both timestamps. The exam admin is notified via the existing notification system.

The conflict detection logic lives in `marks_apply.py`:

```python
# Before writing, check if there is an existing mark with a LATER station timestamp
existing = await db.execute(
    select(TotalMark).where(
        TotalMark.exam_student_subject_id == ess_id,
        TotalMark.paper_type == paper_type,
    )
).scalar_one_or_none()

if existing and existing.entered_at and entered_at < existing.entered_at:
    # Incoming event is OLDER than what Central already has — skip the write
    # but still ACCEPT the event (it arrived late, not wrong)
    await _record_conflict_audit(db, ...)
    return existing_result_snapshot
```

This means: late-arriving events are accepted (not rejected) but do not overwrite newer data.

### 8.4 Attendance conflict — same policy

`upsert_attendance` in `attendance.py` must also accept and use `station_occurred_at` for `transcribed_at`.

---

## 9. Audit Trail and Marks Change History

### 9.1 Why the current design is insufficient

`total_marks` has a `UNIQUE(student_id, subject_code, paper_type)` constraint — one row per student-paper. When a mark is corrected, the row is updated. The previous value is gone. There is no way to answer: "what was this student's mark before we changed it at 14:30?"

### 9.2 New table: `marks_audit` (station SQLite)

Added in schema version 3 (migration in `migrations.py`):

```sql
CREATE TABLE IF NOT EXISTS marks_audit (
    id          INTEGER PRIMARY KEY,
    -- Scope coordinates
    student_id  TEXT    NOT NULL,
    subject_code TEXT   NOT NULL,
    paper_type  TEXT    NOT NULL,
    -- What changed
    operation   TEXT    NOT NULL,  -- 'SET' | 'CLEAR' | 'ITEM_SET' | 'ITEM_CLEAR'
    mode        TEXT    NOT NULL,  -- 'TOTAL_MARKS' | 'ITEM_LEVEL'
    -- Previous value (NULL if this is the first write)
    before_total REAL,
    before_items TEXT,   -- JSON {"Q1": 18, "Q2": 16, ...} or NULL
    -- New value
    after_total  REAL,
    after_items  TEXT,   -- JSON or NULL
    -- Who and when
    actor_assignment_id INTEGER,
    station_occurred_at TEXT NOT NULL,  -- the authoritative station timestamp
    -- Outbox linkage
    event_id    TEXT,    -- the outbox event_id that carried this change to Central
    -- Finalization context
    scope_was_finalized INTEGER NOT NULL DEFAULT 0  -- 1 if scope already finalized (should not happen)
);

CREATE INDEX IF NOT EXISTS idx_marks_audit_student
    ON marks_audit (student_id, subject_code, paper_type);
CREATE INDEX IF NOT EXISTS idx_marks_audit_actor
    ON marks_audit (actor_assignment_id, station_occurred_at);
```

### 9.3 Writing to `marks_audit`

In `entry.py`, `apply_student_paper_marks()`, before the domain write, read the existing mark (before value), then after the domain write, insert a `marks_audit` row. All in the same transaction:

```python
with transaction(conn):
    # 1. Read before-value
    before_total = conn.execute(
        "SELECT total_marks_obtained FROM total_marks WHERE student_id=? AND subject_code=? AND paper_type=?",
        (student_id, subject_code, paper_type.value)
    ).fetchone()
    before_val = before_total["total_marks_obtained"] if before_total else None

    # 2. Apply domain write (existing logic)
    ...

    # 3. Write audit row
    conn.execute(
        "INSERT INTO marks_audit(student_id, subject_code, paper_type, operation, mode,"
        " before_total, after_total, actor_assignment_id, station_occurred_at, event_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (student_id, subject_code, paper_type.value,
         'CLEAR' if not is_present else 'SET',
         mode.value, before_val,
         float(total_marks_obtained) if is_present and total_marks_obtained is not None else None,
         actor_assignment_id, _now(), event_id)
    )
```

For `ITEM_LEVEL` mode, `before_items` and `after_items` are JSON-serialised dicts of question marks.

### 9.4 New API endpoint: marks audit log

```
GET /api/audit/marks
    Query params:
      student_id      (optional)
      subject_code    (optional)
      paper_type      (optional)
      from_date       (optional, ISO date)
      to_date         (optional, ISO date)
      limit           (default 100, max 500)
    Auth: EXAM_ADMIN only
    Response: list of audit rows, newest first
```

### 9.5 Marks Audit view in the UI (admin only)

A new view `view-audit` accessible from the admin sidebar:

```
┌─ Marks Audit Log ──────────────────────────────────────────────────────┐
│  Filter: [Student ID ___] [Subject ___] [Paper ▾] [From ___] [To ___]  │
│                                                          [Search]       │
├──────────────────────────────────────────────────────────────────────── │
│ Time (station)       │ Student          │ Subject │ Paper │ Before │ After │ By  │
├──────────────────────┼──────────────────┼─────────┼───────┼────────┼───────┼─────┤
│ 2026-08-03 09:14:22  │ S0104/0003       │ MATH    │ T1    │  —     │  87.5 │ AB  │
│ 2026-08-03 09:16:55  │ S0104/0003       │ MATH    │ T1    │  87.5  │  79.0 │ AB  │ ← correction
│ 2026-08-03 10:01:33  │ S0107/0012       │ MATH    │ T1    │  —     │  64.0 │ CD  │
└──────────────────────┴──────────────────┴─────────┴───────┴────────┴───────┴─────┘
```

Rows where `before_total IS NOT NULL AND after_total IS NOT NULL` are corrections — they should be highlighted in amber to indicate a value was changed, not just entered for the first time.

### 9.6 Central marks audit (`marks_audit` table in PostgreSQL)

Added via Alembic migration in `lazeims-central-api`:

```sql
-- alembic migration: add_marks_audit_table
CREATE TABLE marks_audit (
    id              BIGSERIAL PRIMARY KEY,
    exam_id         UUID NOT NULL REFERENCES exams(id),
    exam_student_subject_id INTEGER NOT NULL REFERENCES exam_student_subjects(id),
    paper_type      VARCHAR(20) NOT NULL,
    operation       VARCHAR(20) NOT NULL,   -- SET | CLEAR | ITEM_SET | ITEM_CLEAR
    mode            VARCHAR(20) NOT NULL,
    before_total    NUMERIC(7,2),
    before_items    JSONB,
    after_total     NUMERIC(7,2),
    after_items     JSONB,
    station_id      INTEGER REFERENCES stations(id),
    station_occurred_at  TIMESTAMPTZ NOT NULL,  -- from event.occurred_at
    central_received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sync_event_id   TEXT,   -- outbox event_id
    actor_assignment_id INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_marks_audit_ess ON marks_audit (exam_student_subject_id, paper_type);
CREATE INDEX idx_marks_audit_exam ON marks_audit (exam_id, station_occurred_at);
CREATE INDEX idx_marks_audit_event ON marks_audit (sync_event_id);
```

`marks_apply.py` writes to this table on every `STUDENT_PAPER_MARKS_REPLACED` event, capturing the before/after values in the same transaction as the domain write.

### 9.7 Reversibility

The audit trail enables reversal, but reversal is an **admin action via the Central UI**, not an automated process. The data needed for reversal is:
- The `before_total` (or `before_items`) from the `marks_audit` row.
- The `station_occurred_at` so the reversal can be timestamped correctly.

The blueprint does not implement a "revert" button — that is a Central UI feature. This blueprint ensures the data is there to support it.

---

## 10. Remote Data Pull — Download Without Login

### 10.1 Use case

A station admin needs to verify that marks entered last week actually landed on Central, without having a full Central login. The machine credential is enough — it is already on the station.

### 10.2 New central endpoint

```
GET /api/v1/station/pull/snapshot
    Auth: X-Package-Credential-Id + X-Package-Secret  (same as sync)
    Response: {
        "station_code": "S0104-MWZ",
        "exam_id": "...",
        "generated_at": "2026-08-03T10:00:00Z",
        "scopes": [
            {
                "centre_number": "S0104",
                "subject_code": "MATH",
                "paper_type": "THEORY1",
                "finalized": true,
                "student_count": 25,
                "marks_count": 24,       -- present students with marks
                "absent_count": 1,
                "scope_digest": "sha256:...",
                "last_updated_at": "2026-08-03T09:16:55Z"  -- from max(station_occurred_at)
            },
            ...
        ]
    }
```

The `scope_digest` is the same digest that `compute_central_scope_digest()` already computes — it can be compared against the station's local `compute_reconciliation()` result to detect discrepancies without transferring all marks.

### 10.3 New station endpoint

```
POST /api/sync/pull-snapshot
     Auth: station session (EXAM_ADMIN only)
     Response: same shape as Central's GET above, or:
     {
         "configured": false,
         "reason": "..."
     }
```

This endpoint calls Central's `GET /api/v1/station/pull/snapshot` using the machine credential and proxies the result. The station admin never needs to configure anything extra — it reuses the same credential already used for sync.

### 10.4 Pull snapshot view in the UI

In the Sync/Settings panel, a new collapsible section "Central Snapshot":

```
┌─ Central Snapshot ─────────────────────────────────────────────┐
│  Fetch what Central currently holds for this station.          │
│  Uses your machine credential — no Central login needed.       │
│                                                                │
│  [Pull latest snapshot]                                        │
│                                                                │
│  Last fetched: 2026-08-03 10:00:00 UTC                         │
│                                                                │
│  Scope              │ Central   │ Local  │ Match?              │
│  S0104·MATH·T1      │ 24/25     │ 24/25  │ ✓ Digest match      │
│  S0104·MATH·T2      │ 0/25      │ 8/25   │ ⚠ Not yet received  │
│  S0104·BIO·T1       │ 25/25     │ 25/25  │ ✓ Finalized         │
└────────────────────────────────────────────────────────────────┘
```

The "Match?" column compares the `scope_digest` from Central against the locally computed digest from `compute_reconciliation()`.

### 10.5 New station endpoint: local digest

```
GET /api/sync/local-digests
    Auth: EXAM_ADMIN session
    Response: [
        {
            "centre_number": "S0104",
            "subject_code": "MATH",
            "paper_type": "THEORY1",
            "local_digest": "sha256:..."
        },
        ...
    ]
```

This calls `compute_reconciliation(conn)` from `sync.py` which already exists.

---

## 11. School Name Resolution — Fix the Missing Name Problem

### 11.1 Root cause

`/api/scopes` queries `students` + `student_subjects` and returns `centre_number` + `subject_code` + `paper_type` + lock/finalize status. It does NOT join `schools`. School name is a second lookup.

`/api/roster` returns student rows with `first_name` + `surname` separately (violating the AGENTS.md rule: always expose `full_name`).

### 11.2 Fix to `/api/scopes`

The scope query must join `schools` and include `school_name` in every row. Also include `subject_name` from `subjects`:

```python
# In main.py, /api/scopes handler
# Add to the result dict for each scope:
school = conn.execute(
    "SELECT name FROM schools WHERE centre_number = ?", (r["centre_number"],)
).fetchone()
subj_name = conn.execute(
    "SELECT name FROM subjects WHERE subject_code = ?", (r["subject_code"],)
).fetchone()

out.append({
    "centre_number": r["centre_number"],
    "school_name": school["name"] if school else r["centre_number"],   # ← ADDED
    "subject_code": r["subject_code"],
    "subject_name": subj_name["name"] if subj_name else r["subject_code"],  # ← ADDED
    "paper_type": p,
    "lock_status": ...,
    "lock_owner": ...,
    "finalized": ...,
})
```

### 11.3 Fix to `/api/roster`

Return `full_name` instead of `first_name` / `surname`:

```python
# In main.py, /api/roster handler — change:
result.append({
    "student_id": st["student_id"],
    "full_name": (                        # ← CHANGED from first_name/surname
        (st["first_name"] or "").strip().upper()
        + (" " + (st["middle_name"] or "").strip().upper() if st["middle_name"] else "")
        + " " + (st["surname"] or "").strip().upper()
    ).strip(),
    "attendance": ...,
    "has_marks": ...,
})
```

Update `fullName()` in `app.js` — it is no longer needed. Replace all `fullName(s)` calls with `s.full_name`.

### 11.4 Fix to scope selection dropdowns (user creation)

In `loadUsers()` (before rendering the create user form), always call `loadSchools()` first. In `populateScopeDropdowns()`, use school names:

```javascript
// Current (broken):
schoolSel.innerHTML = '...' + schools.map(c => `<option value="${esc(c)}">${esc(c)}</option>`)

// Fixed:
schoolSel.innerHTML = '<option value="">All schools</option>'
  + SCHOOLS.map(s =>
      `<option value="${esc(s.centre_number)}">`
      + `${esc(s.centre_number)} — ${esc(s.name || '(no name)')}`
      + `</option>`
    ).join('');
```

### 11.5 Fix to scope rows in all views

Every place a scope is rendered — scopes list, entry portal table, school accordion, entry bar title — must show the school name. Since `/api/scopes` now includes `school_name` in each row, there is no longer any client-side lookup needed. Remove the `SCHOOLS.find(...)` inline lookup from `renderScopesView()` and `renderPortal()`.

---

## 12. Multi-Repo Schema Changes

### 12.1 `lazeims-station` — SQLite schema (migrations.py)

Three schema migrations are needed. Current `SCHEMA_VERSION` is 1.

**Migration to version 2 — `marks_audit` table:**

```python
if version < 2:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS marks_audit (
            id          INTEGER PRIMARY KEY,
            student_id  TEXT    NOT NULL,
            subject_code TEXT   NOT NULL,
            paper_type  TEXT    NOT NULL,
            operation   TEXT    NOT NULL,
            mode        TEXT    NOT NULL,
            before_total REAL,
            before_items TEXT,
            after_total  REAL,
            after_items  TEXT,
            actor_assignment_id INTEGER,
            station_occurred_at TEXT NOT NULL,
            event_id    TEXT,
            scope_was_finalized INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_marks_audit_student
            ON marks_audit (student_id, subject_code, paper_type);
        CREATE INDEX IF NOT EXISTS idx_marks_audit_actor
            ON marks_audit (actor_assignment_id, station_occurred_at);
        CREATE INDEX IF NOT EXISTS idx_marks_audit_event
            ON marks_audit (event_id);
    """)
    set_user_version(conn, 2)
    version = 2
```

**Migration to version 3 — startup SENDING revert (idempotent):**

```python
if version < 3:
    # Revert orphan SENDING events that survived a crash
    conn.execute(
        "UPDATE outbox_events SET status='PENDING', "
        "last_error='reverted_from_sending_at_startup_v3' "
        "WHERE status='SENDING'"
    )
    conn.commit()
    set_user_version(conn, 3)
    version = 3
```

Update `SCHEMA_VERSION = 3` in `station/__init__.py`.

Also add the startup revert to `apply_migrations()` so it runs on every boot regardless of version:

```python
# Always run at startup, regardless of version:
conn.execute(
    "UPDATE outbox_events SET status='PENDING', last_error='reverted_at_startup'"
    " WHERE status='SENDING'"
)
conn.commit()
```

### 12.2 `lazeims-central-api` — PostgreSQL migrations (Alembic)

**New migration: `add_marks_audit_and_station_occurred_at`**

```python
# alembic/versions/XXXXXXXX_marks_audit_and_occurred_at.py

def upgrade():
    # 1. Add station_occurred_at to total_marks
    op.add_column('total_marks',
        sa.Column('station_occurred_at', sa.DateTime(timezone=True), nullable=True))
    
    # 2. Add station_occurred_at to item_marks
    op.add_column('item_marks',
        sa.Column('station_occurred_at', sa.DateTime(timezone=True), nullable=True))
    
    # 3. Add station_occurred_at to attendance
    op.add_column('attendance',
        sa.Column('station_occurred_at', sa.DateTime(timezone=True), nullable=True))

    # 4. Create marks_audit table
    op.create_table('marks_audit',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('exam_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('exams.id'), nullable=False),
        sa.Column('exam_student_subject_id', sa.Integer(), sa.ForeignKey('exam_student_subjects.id'), nullable=False),
        sa.Column('paper_type', sa.String(20), nullable=False),
        sa.Column('operation', sa.String(20), nullable=False),
        sa.Column('mode', sa.String(20), nullable=False),
        sa.Column('before_total', sa.Numeric(7, 2), nullable=True),
        sa.Column('before_items', postgresql.JSONB(), nullable=True),
        sa.Column('after_total', sa.Numeric(7, 2), nullable=True),
        sa.Column('after_items', postgresql.JSONB(), nullable=True),
        sa.Column('station_id', sa.Integer(), sa.ForeignKey('stations.id'), nullable=True),
        sa.Column('station_occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('central_received_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('sync_event_id', sa.String(80), nullable=True),
        sa.Column('actor_assignment_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    op.create_index('idx_marks_audit_ess', 'marks_audit',
                    ['exam_student_subject_id', 'paper_type'])
    op.create_index('idx_marks_audit_exam', 'marks_audit',
                    ['exam_id', 'station_occurred_at'])
    op.create_index('idx_marks_audit_event', 'marks_audit', ['sync_event_id'])
    op.create_index('idx_marks_audit_station', 'marks_audit',
                    ['station_id', 'station_occurred_at'])


def downgrade():
    op.drop_table('marks_audit')
    op.drop_column('total_marks', 'station_occurred_at')
    op.drop_column('item_marks', 'station_occurred_at')
    op.drop_column('attendance', 'station_occurred_at')
```

**Generate this migration with Alembic (never write it by hand in production):**

```bash
cd /home/administrator/apis/lazeims-central-api
alembic revision --autogenerate -m "marks_audit_and_station_occurred_at"
# Review the generated file, then:
alembic upgrade head
pm2 restart lazeims-api
```

### 12.3 `lazeims-common` — OutboxEvent schema

In `lazeims_common/schemas/station_sync.py`, add `occurred_at` to the event schema:

```python
class StationEvent(BaseModel):
    event_id: str
    entity_type: str
    operation: str
    natural_key: dict
    value: dict | None = None
    local_version: int
    actor_assignment_id: str
    occurred_at: str          # ← ADD: ISO 8601 UTC, station's authoritative timestamp
```

This is a **non-breaking addition** — existing code that builds events without `occurred_at` will default to `None` on the central side (treated as `datetime.now()`). The station must always supply it.

### 12.4 SQLite model additions for `TotalMark` and `ItemMark` (station)

No column addition needed on the station side. The station's `total_marks` table does not need `station_occurred_at` as a column because the `marks_audit` table captures it. The `occurred_at` lives in `outbox_events.occurred_at` and in `marks_audit.station_occurred_at`.

---

## 13. API Contract Changes

### 13.1 Summary of all new/changed endpoints

#### `lazeims-station` (local FastAPI)

| Method | Path | Change |
|--------|------|--------|
| `GET` | `/api/scopes` | Add `school_name`, `subject_name` to each scope row |
| `GET` | `/api/roster` | Return `full_name` instead of `first_name`/`surname` |
| `GET` | `/api/audit/marks` | **NEW** — marks audit log, admin only |
| `GET` | `/api/sync/local-digests` | **NEW** — local scope digests |
| `POST` | `/api/sync/pull-snapshot` | **NEW** — proxy pull from Central |
| `GET` | `/api/sync/export-outbox` | **NEW** — portable outbox ZIP download |
| `POST` | `/api/sync/import-ack` | **NEW** — import ACK ZIP from Central |

#### `lazeims-central-api`

| Method | Path | Change |
|--------|------|--------|
| `GET` | `/api/v1/station/pull/snapshot` | **NEW** — authenticated by machine credential |
| `POST` | `/api/v1/station/sync/portable-events` | **NEW** — portable events upload, returns ACK ZIP |
| `POST` | `/api/v1/station/sync/events` | **CHANGED** — now reads `occurred_at` from event |

### 13.2 `/api/v1/station/pull/snapshot` (Central)

Authentication: same `X-Package-Credential-Id` + `X-Package-Secret` headers as the existing sync endpoint. The router reuses `get_station_from_credential()` from `station_sync.py`.

```python
@router.get("/api/v1/station/pull/snapshot", tags=["station-sync"])
async def pull_snapshot(
    db: AsyncSession = Depends(get_db),
    station = Depends(get_station_from_credential),
):
    exam_id = station.exam_id
    # For each scope assigned to this station, compute digest and counts
    scopes = await _build_snapshot_scopes(db, exam_id=exam_id, station=station)
    return {
        "station_code": station.code,
        "exam_id": str(exam_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scopes": scopes,
    }
```

### 13.3 Sync event envelope `occurred_at` threading

The full path of `occurred_at` from keypress to Central:

```
User saves mark
    │
    ▼
entry.py: apply_student_paper_marks()
    │  _now() captured as station_occurred_at
    │
    ├─► total_marks row: entered_at = station_occurred_at
    └─► outbox_events row: occurred_at = station_occurred_at
            │
            ▼
        sync.py: select_pending()
            │  occurred_at included in event dict
            │
            ▼
        sync_http.py: http_transport()
            │  event sent with occurred_at in payload
            │
            ▼
        Central: station_sync.py _apply_event()
            │  station_occurred_at = event["occurred_at"]
            │
            ├─► total_marks: station_occurred_at = <station time>
            │                entered_at = <station time>   (not server time)
            └─► marks_audit: station_occurred_at = <station time>
                             central_received_at = now()   (server time, for latency tracking)
```

---

## 14. Implementation Order

The changes span three repos. Work must be done in dependency order to avoid breaking the running system.

### Phase 1 — `lazeims-common` (no service restart needed)

1. Add `occurred_at: str | None` to `StationEvent` schema in `station_sync.py`.
2. Run `lazeims-common` tests: `cd /home/administrator/apis/lazeims-common && python -m pytest`.
3. Commit and push to `main`.

### Phase 2 — `lazeims-central-api`

1. Generate and review the Alembic migration for `marks_audit`, `station_occurred_at` columns.
2. Run `alembic upgrade head`.
3. Update `marks_apply.py`: accept `station_occurred_at`, use it as `entered_at`, write `marks_audit`.
4. Update `station_sync.py`: thread `occurred_at` from event into `apply_student_paper_marks` and `upsert_attendance`.
5. Add `GET /api/v1/station/pull/snapshot` endpoint in `station_sync.py` router.
6. Add `POST /api/v1/station/sync/portable-events` endpoint.
7. Run tests: `python -m pytest`.
8. `pm2 restart lazeims-api`.
9. Commit and push.

### Phase 3 — `lazeims-station` backend

1. Update `migrations.py`: add schema v2 (`marks_audit`), v3 (startup SENDING revert). Update `SCHEMA_VERSION`.
2. Update `entry.py`: write `marks_audit` row on every `apply_student_paper_marks` call.
3. Update `outbox.py`: confirm `occurred_at` is always the domain write timestamp.
4. Update `sync.py`: include `occurred_at` in the event envelope sent to Central.
5. Update `/api/scopes` in `main.py`: add `school_name`, `subject_name`.
6. Update `/api/roster` in `main.py`: return `full_name`.
7. Add `/api/audit/marks` endpoint.
8. Add `/api/sync/local-digests`, `/api/sync/pull-snapshot`, `/api/sync/export-outbox`, `/api/sync/import-ack`.
9. Run tests: `python -m pytest tests/`.
10. Add new tests: `test_marks_audit.py`, `test_pull_snapshot.py`.
11. Commit and push.

### Phase 4 — `lazeims-station` UI

1. Restructure `index.html`: two dashboard sections, new views, sidebar nav-group.
2. Restructure `app.js` into § 1–17 regions.
3. Update `app.js`:
   - `afterLogin()`: show the correct dashboard based on role.
   - `loadDashboardDE()`: my progress, my scopes table.
   - `loadDashboardAdmin()`: KPIs, sync card, schools table, DE progress.
   - `renderPortal()` + `renderScopesView()`: use `school_name` and `subject_name` from scope row.
   - `renderMarksTable()` / CAL mode: detect `ITEM_LEVEL` from scope config.
   - `loadUsers()`: call `loadSchools()` first, fix dropdown.
   - Remove `fullName()` helper, use `s.full_name` everywhere.
   - Add `loadAudit()` + `renderAuditTable()`.
   - Add `loadPullSnapshot()` + `renderSnapshotTable()`.
   - Add export/import ack handlers.
4. Update `app.css`: CAL table styles, stage indicator strip, snapshot comparison table.
5. Smoke-test all views manually.
6. Commit and push.

### Phase 5 — Launcher update

1. Update `launcher/start.sh` and `launcher/start.ps1`: export `STATION_AUTOSYNC_SECONDS=120`.
2. Commit and push.

### Rollback plan

- Station schema changes are additive (new table, new index). Rolling back the station code to an earlier version leaves the `marks_audit` table in place but harmless — the old code ignores it.
- Central migration adds columns (`NULLABLE`) and a new table. Rolling back the central code leaves those columns unused but harmless.
- The `occurred_at` field in the sync event envelope is a new key in the JSON. Old Central code (`v1` without the fix) ignores unknown keys — no breakage.

---

## Appendix A — Current Bugs That This Blueprint Fixes

| Bug | Root Cause | Fix Section |
|-----|-----------|-------------|
| School names missing in scope table | `/api/scopes` never joins `schools` | §11 |
| School dropdown empty in user create | `SCHOOLS` not loaded before Users view | §11.4 |
| `fullName()` builds name from parts (violates AGENTS.md) | `roster` returns `first_name`/`surname` | §11.3 |
| Marks change overwrites previous value with no history | No audit table | §9 |
| `entered_at` on marks is Central server time | `marks_apply.py` uses `datetime.now()` | §8 |
| Crashed SENDING events not recovered on restart | No startup revert | §7.3, §12.1 |
| No way to verify Central received the marks | No pull endpoint | §10 |
| Portable transport exists in code but has no UI | `export_pending_envelope()` unreachable | §7.6 |
| CAL (item-level) marks table not designed | `app.js` only handles `TOTAL_MARKS` | §6.4 |
| DE and admin see same dashboard (confusing) | Single `view-dashboard` with hidden panels | §4 |
| Sync status pill shows no outbox state | Pill only shows station_code | §5.4 |

---

## Appendix B — Design Decisions That Are Final

These decisions were made for concrete reasons. They are not open for debate without changing this document first.

1. **No Jinja2 / no server-side rendering.** The station must work fully offline. Static HTML + JSON API is the only architecture that satisfies this.

2. **No per-question mark column visibility toggle.** The CAL table shows all questions for the configured subject. Hiding questions adds complexity with no benefit.

3. **`full_name` only, never individual name parts in the UI.** Per AGENTS.md: the DB computes it, the API returns it, the UI renders it. No exceptions.

4. **`occurred_at` is the station write time, never the HTTP send time.** The moment the user's keystroke was persisted to SQLite is the authoritative timestamp. Network latency and sync timing are irrelevant.

5. **REJECTED events never block other events.** A rejected event for student A in scope X must not prevent student B's event from syncing. The per-savepoint pattern in `process_events()` already implements this.

6. **Marks audit rows are immutable.** Never UPDATE or DELETE a `marks_audit` row. If a supervisor needs to understand the history, they read the append-only log. Reversal is a new write, not a deletion of old writes.

7. **Machine credential is never shown to the user.** Not in the UI, not in logs, not in error messages. The credential ID (not the secret) may be shown truncated for debugging purposes.

8. **Plain static files + ES modules. No Jinja2, no bundler, no npm, no build step.** The station runs on low-spec machines offline. Every added build-time or runtime dependency is a liability. ES modules are natively supported by every browser that can run this application. `StaticFiles` in FastAPI serves the `js/` directory with no extra configuration. The HTML is large but loaded once and cached — view switches are pure DOM, never a server round-trip.

9. **View HTML stays inline in `index.html`.** Fetched HTML fragments (`fetch('/views/login.html')`) create a dependency on server availability at the moment of navigation — unacceptable for offline use. All view `<section>` elements are in the initial HTML. The browser parses them once at load time.

10. **`app.js` is dead. `static/js/*.js` ES modules replace it.** The monolithic 52 KB `app.js` is deleted and replaced with focused modules. This is the only acceptable path — a single growing JS file with no module boundaries cannot be maintained or tested.
