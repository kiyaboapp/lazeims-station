from __future__ import annotations

from pathlib import Path

import pytest
from argon2 import PasswordHasher

from lazeims_common.enums import FillingMode, PaperType
from lazeims_common.hashing import sha256_prefixed
from lazeims_common.portable import DIRECTION_ACKS, open_envelope, seal
from lazeims_common.reconcile import normalize_total_record, scope_digest

from station import entry as E
from station import sync as S
from station.db import connect
from station.migrations import apply_migrations, import_package

_ph = PasswordHasher()
T1 = PaperType.THEORY1


def _seed():
    return {
        "schools": [{"centre_number": "SCH-1", "name": "S1"}],
        "subjects": [{"subject_code": "011", "name": "H", "papers": ["THEORY1"],
                      "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0}, "groups": [], "questions": []}],
        "students": [{"student_id": "S-1", "centre_number": "SCH-1", "first_name": "A", "middle_name": None, "surname": "B", "sex": "M"}],
        "registrations": [{"student_id": "S-1", "subject_code": "011"}],
        "credentials": [], "processing_api_key": None,
    }


def _bundle():
    seed = _seed()
    return {"manifest": {"contract_version": "station-package/v1", "package_id": "pkg_s", "package_version": 1,
                         "rules_version": "1.0", "software_min_version": "1.0.0", "station_code": "ST-1",
                         "exam_id": "FTNA-2026", "configuration_hash": sha256_prefixed(seed),
                         "issued_at": "2026-07-27T08:00:00Z",
                         "scope": {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, "signature": "x"},
            "seed": seed}


def _db(tmp_path):
    conn = connect(tmp_path / "s.sqlite3")
    apply_migrations(conn)
    import_package(conn, _bundle())
    # produce two pending events: attendance + marks
    E.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1,
                            is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                mode=FillingMode.TOTAL_MARKS, total_marks_obtained=67, actor_assignment_id=42)
    return conn


def _accept_all(body):
    return {"accepted": [{"event_id": e["event_id"], "central_version": 1} for e in body["events"]],
            "duplicates": [], "rejected": [], "server_time": "t"}


# ---------- happy path ----------

def test_run_sync_accepts_and_acks(tmp_path):
    conn = _db(tmp_path)
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='PENDING'").fetchone()["c"] == 2
    res = S.run_sync(conn, _accept_all)
    assert res["accepted"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='ACCEPTED'").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='PENDING'").fetchone()["c"] == 0


# ---------- interruption resumability ----------

def test_network_failure_reverts_to_pending(tmp_path):
    conn = _db(tmp_path)
    def boom(body):
        raise ConnectionError("network down")
    res = S.run_sync(conn, boom)
    assert res.get("resumable") is True
    # events back to PENDING with attempts incremented, nothing lost
    rows = conn.execute("SELECT status, attempts FROM outbox_events").fetchall()
    assert all(r["status"] == "PENDING" for r in rows)
    assert all(r["attempts"] == 1 for r in rows)
    # a subsequent successful run drains them
    res2 = S.run_sync(conn, _accept_all)
    assert res2["accepted"] == 2


# ---------- duplicate ack ----------

def test_duplicate_ack_marks_accepted(tmp_path):
    conn = _db(tmp_path)
    def dup(body):
        return {"accepted": [], "duplicates": [{"event_id": e["event_id"]} for e in body["events"]],
                "rejected": [], "server_time": "t"}
    S.run_sync(conn, dup)
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='ACCEPTED'").fetchone()["c"] == 2


# ---------- rejection correction ----------

def test_rejected_event_stays_visible_then_corrected(tmp_path):
    conn = _db(tmp_path)
    # reject the marks event, accept attendance
    def mixed(body):
        acc, rej = [], []
        for e in body["events"]:
            if e["entity_type"] == "STUDENT_PAPER_MARKS_REPLACED":
                rej.append({"event_id": e["event_id"], "code": "MARK_OUT_OF_RANGE", "message": "x"})
            else:
                acc.append({"event_id": e["event_id"], "central_version": 1})
        return {"accepted": acc, "duplicates": [], "rejected": rej, "server_time": "t"}
    S.run_sync(conn, mixed)
    rej = conn.execute("SELECT event_id, last_error FROM outbox_events WHERE status='REJECTED'").fetchall()
    assert len(rej) == 1 and rej[0]["last_error"] == "MARK_OUT_OF_RANGE"
    # correction: re-enter marks -> new pending event; re-sync accepts it
    E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                mode=FillingMode.TOTAL_MARKS, total_marks_obtained=70, actor_assignment_id=42)
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='PENDING'").fetchone()["c"] == 1
    S.run_sync(conn, _accept_all)
    # the rejected one is still visible (not silently dropped)
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='REJECTED'").fetchone()["c"] == 1


# ---------- reconciliation digest equality ----------

def test_reconciliation_digest_matches_expected(tmp_path):
    conn = _db(tmp_path)
    # finalize the scope so it appears in reconciliation
    from station import finalize as F
    ok, _ = F.finalize(conn, centre_number="SCH-1", subject_code="011", paper_type=T1, finalized_by=42)
    assert ok
    recs = S.compute_reconciliation(conn)
    assert len(recs) == 1
    expected = scope_digest([normalize_total_record("S-1", True, 67)])
    assert recs[0]["local_digest"] == expected
    assert recs[0]["centre_number"] == "SCH-1"


# ---------- portable round trip ----------

def test_portable_export_and_ack(tmp_path):
    from lazeims_common.portable import generate_key, open_envelope as _open
    conn = _db(tmp_path)
    key = generate_key()
    token = S.export_pending_envelope(conn, key, sequence=1)
    assert token is not None
    # events now SENDING
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='SENDING'").fetchone()["c"] == 2
    # simulate Central: open EVENTS, produce ACK accepting all
    opened = _open(token, key=key, expected_recipient="CENTRAL")
    evids = [e["event_id"] for e in opened.payload["events"]]
    ack = seal({"accepted": [{"event_id": i, "central_version": 1} for i in evids], "duplicates": [], "rejected": [], "server_time": "t"},
               key=key, sender="CENTRAL", recipient="ST-1", direction=DIRECTION_ACKS, sequence=1)
    res = S.apply_ack_envelope(conn, key, ack)
    assert res["accepted"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='ACCEPTED'").fetchone()["c"] == 2
