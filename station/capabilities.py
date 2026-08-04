"""Per-station capability derivation.

Capabilities are keyed strings used by both the API (for authorization) and
the UI (to hide/show controls). Rather than store an explicit capability
column on ``station_users``, we derive the list from ``role`` (already stored)
and augment it with a DE's assigned scopes.

If a future package format ships an explicit capability list, we can extend
:func:`capabilities_for` to prefer it over the derived defaults without
touching every call site.
"""

from __future__ import annotations

import sqlite3

# Stable capability keys. Add here; never rename.
CAP_ENTER_MARKS   = "entry.marks.enter"
CAP_ENTER_ATT     = "entry.attendance.enter"
CAP_FINALIZE      = "entry.scope.finalize"
CAP_VIEW_SCOPES   = "entry.scopes.view"
CAP_ADMIN_USERS   = "admin.users.manage"
CAP_ADMIN_IMPORT  = "admin.package.import"
CAP_ADMIN_SYNC    = "admin.sync.run"
CAP_ADMIN_AUDIT   = "admin.audit.view"
CAP_ADMIN_SETTINGS = "admin.settings.manage"
CAP_ADMIN_FORCE_UNLOCK = "admin.scope.force_unlock"


_DE_CAPS = frozenset({
    CAP_ENTER_MARKS,
    CAP_ENTER_ATT,
    CAP_FINALIZE,
    CAP_VIEW_SCOPES,
})

_ADMIN_CAPS = frozenset({
    CAP_ENTER_MARKS,
    CAP_ENTER_ATT,
    CAP_FINALIZE,
    CAP_VIEW_SCOPES,
    CAP_ADMIN_USERS,
    CAP_ADMIN_IMPORT,
    CAP_ADMIN_SYNC,
    CAP_ADMIN_AUDIT,
    CAP_ADMIN_SETTINGS,
    CAP_ADMIN_FORCE_UNLOCK,
})


def capabilities_for(role: str) -> list[str]:
    """Return the ordered list of capabilities granted to a role."""
    if role == "EXAM_ADMIN":
        return sorted(_ADMIN_CAPS)
    if role == "DATA_ENTERER":
        return sorted(_DE_CAPS)
    return []


def has_capability(role: str, capability: str) -> bool:
    return capability in set(capabilities_for(role))


def scopes_for_de(conn: sqlite3.Connection, assignment_id: int) -> dict:
    """Return the DE's allowed centres and subjects (empty set = no restriction).

    Kept here (rather than only in :mod:`station.auth`) so capability-aware
    layers can consult it without importing auth machinery.
    """
    rows = conn.execute(
        "SELECT centre_number, subject_code FROM user_scopes WHERE assignment_id = ?",
        (assignment_id,),
    ).fetchall()
    centres: set[str] = set()
    subjects: set[str] = set()
    for r in rows:
        if r["centre_number"]:
            centres.add(r["centre_number"])
        if r["subject_code"]:
            subjects.add(r["subject_code"])
    return {"centres": sorted(centres), "subjects": sorted(subjects)}
