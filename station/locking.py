"""Local work locking for a scope ``(exam, school, subject, paper)``.

One atomic lock per scope. ``ACTIVE`` locks expire only when stale; a
``FINALIZED`` lock never auto-releases. The Station Exam Admin can force-release
an active lock, which is recorded in ``audit_log`` with a reason.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .db import transaction

STALE_ACTIVE_SECONDS = 30 * 60  # 30 minutes


def scope_key(centre_number: str, subject_code: str, paper_type: str) -> str:
    return f"{centre_number}|{subject_code}|{paper_type}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_stale(acquired_at: str | None) -> bool:
    if not acquired_at:
        return True
    try:
        ts = datetime.fromisoformat(acquired_at)
    except ValueError:
        return True
    return (_now() - ts) > timedelta(seconds=STALE_ACTIVE_SECONDS)


class LockError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def acquire(conn: sqlite3.Connection, key: str, owner: int) -> dict:
    """Acquire (or refresh) an ACTIVE lock. Raises LockError if held by another
    active, non-stale owner, or if the scope is FINALIZED."""
    with transaction(conn):
        row = conn.execute("SELECT * FROM work_locks WHERE scope_key = ?", (key,)).fetchone()
        now = _now().isoformat()
        if row is None:
            conn.execute(
                "INSERT INTO work_locks(scope_key, owner, status, acquired_at) VALUES(?,?, 'ACTIVE', ?)",
                (key, owner, now),
            )
            return {"scope_key": key, "owner": owner, "status": "ACTIVE", "acquired": True}
        if row["status"] == "FINALIZED":
            raise LockError("SCOPE_ALREADY_FINALIZED", "Scope is finalized and cannot be locked.")
        # ACTIVE
        if row["owner"] == owner or _is_stale(row["acquired_at"]):
            conn.execute(
                "UPDATE work_locks SET owner = ?, acquired_at = ?, status = 'ACTIVE' WHERE scope_key = ?",
                (owner, now, key),
            )
            return {"scope_key": key, "owner": owner, "status": "ACTIVE", "acquired": True}
        raise LockError("LOCKED", f"Scope is locked by another user (owner {row['owner']}).")


def release(conn: sqlite3.Connection, key: str, owner: int) -> bool:
    with transaction(conn):
        row = conn.execute("SELECT * FROM work_locks WHERE scope_key = ?", (key,)).fetchone()
        if row is None or row["status"] == "FINALIZED":
            return False
        if row["owner"] != owner and not _is_stale(row["acquired_at"]):
            raise LockError("LOCKED", "Only the owner may release this lock.")
        conn.execute("DELETE FROM work_locks WHERE scope_key = ?", (key,))
        return True


def force_release(conn: sqlite3.Connection, key: str, admin_id: int, reason: str) -> bool:
    if not reason or not reason.strip():
        raise LockError("REASON_REQUIRED", "A reason is required to force-release a lock.")
    with transaction(conn):
        row = conn.execute("SELECT * FROM work_locks WHERE scope_key = ?", (key,)).fetchone()
        if row is None:
            return False
        if row["status"] == "FINALIZED":
            raise LockError("SCOPE_ALREADY_FINALIZED", "Cannot force-release a finalized scope.")
        conn.execute("DELETE FROM work_locks WHERE scope_key = ?", (key,))
        conn.execute(
            "INSERT INTO audit_log(actor, action, entity_type, entity_id, before_json, after_json, occurred_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (admin_id, "LOCK_FORCE_RELEASED", "work_lock", key,
             f'{{"owner": {row["owner"]}}}', f'{{"reason": {reason!r}}}', _now().isoformat()),
        )
        return True


def mark_finalized(conn: sqlite3.Connection, key: str) -> None:
    """Called by the finalize sweep: convert the lock to FINALIZED (never auto-released)."""
    row = conn.execute("SELECT scope_key FROM work_locks WHERE scope_key = ?", (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO work_locks(scope_key, owner, status, acquired_at) VALUES(?, NULL, 'FINALIZED', ?)",
            (key, _now().isoformat()),
        )
    else:
        conn.execute("UPDATE work_locks SET status = 'FINALIZED' WHERE scope_key = ?", (key,))
