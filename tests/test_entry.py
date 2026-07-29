from __future__ import annotations

from pathlib import Path

import pytest
from argon2 import PasswordHasher

from lazeims_common.enums import FillingMode, PaperType
from lazeims_common.errors import ValidationError
from lazeims_common.hashing import sha256_prefixed

from station import entry as E
from station import finalize as F
from station import locking, outbox
from station.db import connect
from station.migrations import apply_migrations, import_package

_ph = PasswordHasher()


def _seed():
    return {
        "schools": [{"centre_number": "SCH-1", "name": "School One"}],
        "subjects": [{
            "subject_code": "011", "name": "History", "papers": ["THEORY1"],
            "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
            "groups": [], "questions": [],
        }],
        "students": [
            {"student_id": "S-1", "centre_number": "SCH-1", "first_name": "A", "middle_name": None, "surname": "One", "sex": "M"},
            {"student_id": "S-2", "centre_number": "SCH-1", "first_name": "B", "middle_name": None, "surname": "Two", "sex": "F"},
        ],
        "registrations": [
            {"student_id": "S-1", "subject_code": "011"},
            {"student_id": "S-2", "subject_code": "011"},
        ],
        "credentials": [{"assignment_id": 42, "role": "DATA_ENTERER",
                         "pin_hash": _ph.hash("123456"), "initials": "JK", "password_hash": None}],
        "processing_api_key": None,
    }


def _bundle():
    seed = _seed()
    return {"manifest": {
        "contract_version": "station-package/v1", "package_id": "pkg_e", "package_version": 1,
        "rules_version": "1.0", "software_min_version": "1.0.0",
        "station_code": "ST-1", "exam_id": "FTNA-2026",
        "configuration_hash": sha256_prefixed(seed), "issued_at": "2026-07-27T08:00:00Z",
        "scope": {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]},
        "signature": "x",
    }, "seed": seed}


def _db(tmp_path: Path):
    conn = connect(tmp_path / "s.sqlite3")
    apply_migrations(conn)
    import_package(conn, _bundle())
    return conn


KEY = locking.scope_key("SCH-1", "011", "THEORY1")
T1 = PaperType.THEORY1


# ---------- locking concurrency ----------

def test_two_browsers_one_lock(tmp_path):
    dbfile = tmp_path / "s.sqlite3"
    conn = _db(tmp_path)
    conn.close()
    c1 = connect(dbfile); c2 = connect(dbfile)
    locking.acquire(c1, KEY, owner=1)
    with pytest.raises(locking.LockError) as exc:
        locking.acquire(c2, KEY, owner=2)
    assert exc.value.code == "LOCKED"
    # owner1 releases -> owner2 can acquire
    locking.release(c1, KEY, owner=1)
    got = locking.acquire(c2, KEY, owner=2)
    assert got["acquired"] is True


def test_finalized_scope_cannot_lock(tmp_path):
    conn = _db(tmp_path)
    locking.mark_finalized(conn, KEY)
    with pytest.raises(locking.LockError) as exc:
        locking.acquire(conn, KEY, owner=1)
    assert exc.value.code == "SCOPE_ALREADY_FINALIZED"


def test_force_release_requires_reason_and_audits(tmp_path):
    conn = _db(tmp_path)
    locking.acquire(conn, KEY, owner=1)
    with pytest.raises(locking.LockError):
        locking.force_release(conn, KEY, admin_id=9, reason="")
    assert locking.force_release(conn, KEY, admin_id=9, reason="stuck") is True
    audit = conn.execute("SELECT * FROM audit_log WHERE action='LOCK_FORCE_RELEASED'").fetchone()
    assert audit is not None


# ---------- attendance gate + marks rules ----------

def test_marks_require_attendance_first(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(ValidationError) as exc:
        E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                    mode=FillingMode.TOTAL_MARKS, total_marks_obtained=50, actor_assignment_id=42)
    assert exc.value.code.value == "ATTENDANCE_REQUIRED_FIRST"


def test_absent_with_marks_rejected(tmp_path):
    conn = _db(tmp_path)
    E.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1,
                            is_present=False, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    with pytest.raises(ValidationError) as exc:
        E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                    mode=FillingMode.TOTAL_MARKS, total_marks_obtained=10, actor_assignment_id=42)
    assert exc.value.code.value == "ABSENT_STUDENT_HAS_MARKS"


def test_over_max_rejected_and_atomic(tmp_path):
    conn = _db(tmp_path)
    E.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1,
                            is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    marks_events_before = conn.execute(
        "SELECT COUNT(*) c FROM outbox_events WHERE entity_type='STUDENT_PAPER_MARKS_REPLACED'").fetchone()["c"]
    with pytest.raises(ValidationError) as exc:
        E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                    mode=FillingMode.TOTAL_MARKS, total_marks_obtained=101, actor_assignment_id=42)
    assert exc.value.code.value == "MARK_OUT_OF_RANGE"
    # nothing written: no total_marks row, no new outbox event (validation before txn)
    assert conn.execute("SELECT COUNT(*) c FROM total_marks").fetchone()["c"] == 0
    after = conn.execute(
        "SELECT COUNT(*) c FROM outbox_events WHERE entity_type='STUDENT_PAPER_MARKS_REPLACED'").fetchone()["c"]
    assert after == marks_events_before


# ---------- transactional outbox ----------

def test_outbox_written_atomically_with_domain(tmp_path):
    conn = _db(tmp_path)
    E.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1,
                            is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    res = E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                      mode=FillingMode.TOTAL_MARKS, total_marks_obtained=67, actor_assignment_id=42)
    assert res["computed_total"] == "67"
    # domain rows
    assert conn.execute("SELECT total_marks_obtained FROM total_marks WHERE student_id='S-1'").fetchone()["total_marks_obtained"] == 67
    # outbox events (attendance + marks), both PENDING
    ev = conn.execute("SELECT entity_type, status FROM outbox_events ORDER BY event_id").fetchall()
    types = {r["entity_type"] for r in ev}
    assert "ATTENDANCE_TRANSCRIBED" in types and "STUDENT_PAPER_MARKS_REPLACED" in types
    assert all(r["status"] == "PENDING" for r in ev)


# ---------- finalize block / pass ----------

def test_finalize_blocks_then_passes(tmp_path):
    conn = _db(tmp_path)
    # both present, only S-1 has marks -> blocked
    for sid in ("S-1", "S-2"):
        E.transcribe_attendance(conn, student_id=sid, subject_code="011", paper_type=T1,
                                is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                mode=FillingMode.TOTAL_MARKS, total_marks_obtained=40, actor_assignment_id=42)
    ok, result = F.finalize(conn, centre_number="SCH-1", subject_code="011", paper_type=T1, finalized_by=42)
    assert ok is False
    assert any(b["code"] == "BLANK_MARK_NOT_ALLOWED" for b in result["blockers"])

    # give S-2 marks -> passes
    E.apply_student_paper_marks(conn, student_id="S-2", subject_code="011", paper_type=T1,
                                mode=FillingMode.TOTAL_MARKS, total_marks_obtained=55, actor_assignment_id=42)
    ok2, _ = F.finalize(conn, centre_number="SCH-1", subject_code="011", paper_type=T1, finalized_by=42)
    assert ok2 is True
    # finalized_scopes row + SCOPE_FINALIZED event + lock FINALIZED
    assert conn.execute("SELECT 1 FROM finalized_scopes WHERE scope_key=?", (KEY,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM outbox_events WHERE entity_type='SCOPE_FINALIZED'").fetchone() is not None
    assert conn.execute("SELECT status FROM work_locks WHERE scope_key=?", (KEY,)).fetchone()["status"] == "FINALIZED"


def test_absent_students_allow_finalize(tmp_path):
    conn = _db(tmp_path)
    # S-1 present with marks, S-2 absent (no marks) -> finalize passes
    E.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1,
                            is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                mode=FillingMode.TOTAL_MARKS, total_marks_obtained=40, actor_assignment_id=42)
    E.transcribe_attendance(conn, student_id="S-2", subject_code="011", paper_type=T1,
                            is_present=False, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    ok, _ = F.finalize(conn, centre_number="SCH-1", subject_code="011", paper_type=T1, finalized_by=42)
    assert ok is True


# ---------- restart persistence (gate) ----------

def test_restart_persistence(tmp_path):
    dbfile = tmp_path / "s.sqlite3"
    conn = _db(tmp_path)
    E.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1,
                            is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                mode=FillingMode.TOTAL_MARKS, total_marks_obtained=70, actor_assignment_id=42)
    conn.close()  # simulate process/PC restart

    conn2 = connect(dbfile)
    assert conn2.execute("SELECT total_marks_obtained FROM total_marks WHERE student_id='S-1'").fetchone()["total_marks_obtained"] == 70
    assert conn2.execute("SELECT COUNT(*) c FROM outbox_events").fetchone()["c"] >= 2
    # attendance persisted too
    assert conn2.execute("SELECT is_present FROM attendance WHERE student_id='S-1'").fetchone()["is_present"] == 1


def test_finalized_scope_blocks_further_marks(tmp_path):
    conn = _db(tmp_path)
    E.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1,
                            is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                mode=FillingMode.TOTAL_MARKS, total_marks_obtained=40, actor_assignment_id=42)
    E.transcribe_attendance(conn, student_id="S-2", subject_code="011", paper_type=T1,
                            is_present=False, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    F.finalize(conn, centre_number="SCH-1", subject_code="011", paper_type=T1, finalized_by=42)
    with pytest.raises(ValidationError) as exc:
        E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                    mode=FillingMode.TOTAL_MARKS, total_marks_obtained=41, actor_assignment_id=42)
    assert exc.value.code.value == "SCOPE_ALREADY_FINALIZED"
