"""Local transactional outbox helpers.

Every committed domain change writes a coarse-grained ``outbox_events`` row in
the SAME SQLite transaction (see ``entry.py``). Event state machine:
``PENDING -> SENDING -> ACCEPTED | REJECTED`` (sync phase drives transitions).

Coarse event types (delivery plan §11.1):
    ATTENDANCE_TRANSCRIBED, INCIDENT_RAISED,
    STUDENT_PAPER_MARKS_REPLACED, SCOPE_FINALIZED
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def _next_local_version(conn: sqlite3.Connection, natural_key: dict) -> int:
    """Monotonic per-natural-key version for last-writer bookkeeping."""
    nk = json.dumps(natural_key, sort_keys=True)
    row = conn.execute(
        "SELECT MAX(local_version) v FROM outbox_events WHERE natural_key_json = ?", (nk,)
    ).fetchone()
    return (row["v"] or 0) + 1


def add_event(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    natural_key: dict,
    value: object,
    actor_assignment_id: int | None,
    operation: str = "UPSERT",
) -> str:
    """Insert one outbox event. MUST be called inside an open transaction that
    also writes the corresponding domain row(s)."""
    event_id = "evt_" + uuid.uuid4().hex
    nk = json.dumps(natural_key, sort_keys=True)
    conn.execute(
        "INSERT INTO outbox_events(event_id, entity_type, operation, natural_key_json,"
        " value_json, local_version, actor_assignment_id, occurred_at, status, attempts)"
        " VALUES(?,?,?,?,?,?,?,?, 'PENDING', 0)",
        (
            event_id, entity_type, operation, nk,
            json.dumps(value) if value is not None else None,
            _next_local_version(conn, natural_key),
            actor_assignment_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return event_id


def pending_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) c FROM outbox_events WHERE status IN ('PENDING','SENDING')"
    ).fetchone()["c"])


def rejected_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) c FROM outbox_events WHERE status = 'REJECTED'"
    ).fetchone()["c"])


def list_rejected(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict]:
    """Return rejected outbox events with their reason so an admin can see
    WHY Central refused them (e.g. ATTENDANCE_REQUIRED_FIRST, PHASE_NOT_OPEN)
    instead of just a bare count.
    """
    rows = conn.execute(
        "SELECT event_id, entity_type, operation, natural_key_json, value_json,"
        " local_version, actor_assignment_id, occurred_at, attempts, last_error"
        " FROM outbox_events WHERE status = 'REJECTED'"
        " ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            natural_key = json.loads(r["natural_key_json"]) if r["natural_key_json"] else {}
        except (TypeError, ValueError):
            natural_key = {}
        out.append({
            "event_id": r["event_id"],
            "entity_type": r["entity_type"],
            "operation": r["operation"],
            "natural_key": natural_key,
            "local_version": r["local_version"],
            "actor_assignment_id": r["actor_assignment_id"],
            "occurred_at": r["occurred_at"],
            "attempts": r["attempts"],
            # last_error holds the rejection CODE from Central (see
            # station/sync.py::run_sync), e.g. "ATTENDANCE_REQUIRED_FIRST".
            "rejection_code": r["last_error"],
        })
    return out


def retry_rejected(conn: sqlite3.Connection) -> int:
    """Reset all REJECTED events back to PENDING so the next sync re-sends them.

    Returns the number of events queued for retry.
    """
    cur = conn.execute(
        "UPDATE outbox_events SET status='PENDING', last_error=NULL"
        " WHERE status='REJECTED'"
    )
    return cur.rowcount
