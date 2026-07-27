"""Station outbox transport + reconciliation compute.

The transport is INJECTABLE (a callable ``(request_dict) -> response_dict``) so
it can be unit-tested without a live Central, and production uses an HTTPS
adapter. Direct HTTPS and portable envelopes both feed the SAME response-apply
logic — a different transport, never different outcome.

Outbox state machine: PENDING -> SENDING -> ACCEPTED | REJECTED.
A network failure reverts SENDING -> PENDING (with attempt++/last_error) so the
next run resumes exactly where it left off (interruption resumability).
Rejected events stay visible (REJECTED) and never block unrelated scopes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from lazeims_common.reconcile import (
    normalize_item_record,
    normalize_total_record,
    scope_digest,
)

from .db import transaction

Transport = Callable[[dict], dict]
BATCH_MAX = 200


def _meta(conn, key):
    r = conn.execute("SELECT value FROM station_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def _current_package(conn):
    return conn.execute(
        "SELECT package_id, package_version, rules_version FROM packages ORDER BY package_version DESC LIMIT 1"
    ).fetchone()


def select_pending(conn: sqlite3.Connection, limit: int = BATCH_MAX) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM outbox_events WHERE status='PENDING' ORDER BY rowid LIMIT ?", (limit,)
    ).fetchall()
    events = []
    for r in rows:
        events.append({
            "event_id": r["event_id"], "entity_type": r["entity_type"], "operation": r["operation"],
            "natural_key": json.loads(r["natural_key_json"]),
            "value": json.loads(r["value_json"]) if r["value_json"] else None,
            "local_version": r["local_version"], "actor_assignment_id": str(r["actor_assignment_id"] or ""),
            "occurred_at": r["occurred_at"],
        })
    return events


def _set_status(conn, event_ids, status, *, last_error=None, ack=False):
    now = datetime.now(timezone.utc).isoformat()
    for eid in event_ids:
        if ack:
            conn.execute("UPDATE outbox_events SET status=?, ack_at=?, last_error=NULL WHERE event_id=?",
                         (status, now, eid))
        elif status == "PENDING":
            conn.execute("UPDATE outbox_events SET status='PENDING', attempts=attempts+1, last_error=? WHERE event_id=?",
                         (last_error, eid))
        else:
            conn.execute("UPDATE outbox_events SET status=?, last_error=? WHERE event_id=?",
                         (status, last_error, eid))


def run_sync(conn: sqlite3.Connection, transport: Transport, *, limit: int = BATCH_MAX) -> dict:
    """One bounded sync run. Returns a summary dict."""
    events = select_pending(conn, limit)
    if not events:
        return {"sent": 0, "accepted": 0, "duplicates": 0, "rejected": 0}

    ids = [e["event_id"] for e in events]
    with transaction(conn):
        for eid in ids:
            conn.execute("UPDATE outbox_events SET status='SENDING' WHERE event_id=?", (eid,))

    pkg = _current_package(conn)
    body = {
        "contract_version": "station-sync/v1",
        "station_code": _meta(conn, "station_code"),
        "exam_id": _meta(conn, "exam_id"),
        "package_id": pkg["package_id"], "package_version": pkg["package_version"],
        "rules_version": pkg["rules_version"], "events": events,
    }

    try:
        resp = transport(body)
    except Exception as exc:  # network/server failure -> resume later
        with transaction(conn):
            _set_status(conn, ids, "PENDING", last_error=f"transport: {exc}")
        return {"sent": len(ids), "error": str(exc), "resumable": True}

    accepted = {e["event_id"] for e in resp.get("accepted", [])}
    duplicates = {e["event_id"] for e in resp.get("duplicates", [])}
    rejected = {e["event_id"]: e for e in resp.get("rejected", [])}

    with transaction(conn):
        for eid in ids:
            if eid in accepted or eid in duplicates:
                _set_status(conn, [eid], "ACCEPTED", ack=True)
            elif eid in rejected:
                _set_status(conn, [eid], "REJECTED", last_error=rejected[eid].get("code"))
            else:
                # not addressed by the server -> resume next run
                _set_status(conn, [eid], "PENDING", last_error="no result returned")
    return {"sent": len(ids), "accepted": len(accepted), "duplicates": len(duplicates),
            "rejected": len(rejected)}


# ---------------- reconciliation compute ----------------

def _scope_records(conn, centre_number, subject_code, paper_type) -> list[dict]:
    qrows = conn.execute(
        "SELECT id, question_number FROM questions WHERE subject_code=? AND paper_type=?",
        (subject_code, paper_type)).fetchall()
    is_item = len(qrows) > 0
    qnum_by_id = {r["id"]: r["question_number"] for r in qrows}
    students = conn.execute(
        "SELECT s.student_id FROM students s JOIN student_subjects ss ON ss.student_id=s.student_id"
        " WHERE s.centre_number=? AND ss.subject_code=?", (centre_number, subject_code)).fetchall()

    from .entry import resolve_effective_attendance
    from lazeims_common.enums import PaperType
    pt = PaperType(paper_type)
    records = []
    for row in students:
        sid = row["student_id"]
        present = resolve_effective_attendance(conn, sid, subject_code, pt)
        if is_item:
            marks = conn.execute(
                f"SELECT question_id, marks_obtained FROM item_marks WHERE student_id=? AND question_id IN ({','.join('?'*len(qnum_by_id))})",
                (sid, *qnum_by_id.keys())).fetchall() if qnum_by_id else []
            items = {qnum_by_id[m["question_id"]]: m["marks_obtained"] for m in marks}
            if present or items:
                records.append(normalize_item_record(sid, present, items))
        else:
            tm = conn.execute(
                "SELECT total_marks_obtained FROM total_marks WHERE student_id=? AND subject_code=? AND paper_type=?",
                (sid, subject_code, paper_type)).fetchone()
            records.append(normalize_total_record(sid, present, None if tm is None else tm["total_marks_obtained"]))
    return records


def compute_reconciliation(conn: sqlite3.Connection) -> list[dict]:
    """For each finalized scope, compute the local digest to send to Central."""
    out = []
    for row in conn.execute("SELECT scope_key FROM finalized_scopes").fetchall():
        centre, subject_code, paper_type = row["scope_key"].split("|")
        digest = scope_digest(_scope_records(conn, centre, subject_code, paper_type))
        out.append({"centre_number": centre, "subject_code": subject_code,
                    "paper_type": paper_type, "local_digest": digest})
    return out


# ---------------- portable ----------------

def export_pending_envelope(conn: sqlite3.Connection, key: str, *, sequence: int = 1, limit: int = BATCH_MAX) -> str | None:
    """Seal pending events into a portable EVENTS envelope addressed to CENTRAL."""
    from lazeims_common.portable import DIRECTION_EVENTS, seal
    events = select_pending(conn, limit)
    if not events:
        return None
    ids = [e["event_id"] for e in events]
    with transaction(conn):
        for eid in ids:
            conn.execute("UPDATE outbox_events SET status='SENDING' WHERE event_id=?", (eid,))
    pkg = _current_package(conn)
    payload = {"package_id": pkg["package_id"], "events": events}
    return seal(payload, key=key, sender=_meta(conn, "station_code"),
                recipient="CENTRAL", direction=DIRECTION_EVENTS, sequence=sequence)


def apply_ack_envelope(conn: sqlite3.Connection, key: str, token: str) -> dict:
    """Open a portable ACK envelope from Central and apply per-event results."""
    from lazeims_common.portable import open_envelope
    opened = open_envelope(token, key=key, expected_recipient=_meta(conn, "station_code"))
    resp = opened.payload
    accepted = {e["event_id"] for e in resp.get("accepted", [])}
    duplicates = {e["event_id"] for e in resp.get("duplicates", [])}
    rejected = {e["event_id"]: e for e in resp.get("rejected", [])}
    with transaction(conn):
        for eid in list(accepted | duplicates):
            _set_status(conn, [eid], "ACCEPTED", ack=True)
        for eid, r in rejected.items():
            _set_status(conn, [eid], "REJECTED", last_error=r.get("code"))
    return {"accepted": len(accepted), "duplicates": len(duplicates), "rejected": len(rejected)}
