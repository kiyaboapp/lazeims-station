"""Regression tests:

1. GET /api/scopes/report previously crashed with
   ``sqlite3.OperationalError: no such column: school_name`` because it
   queried columns/tables that do not exist in the real schema
   (station/migrations.py). Reproduced on KIYABO-CONSORT after finalizing
   S0202/031/THEORY1 on MWANZA-2.
2. GET /api/sync/rejected surfaces Central's per-event rejection reason so
   an admin isn't stuck looking at a bare "48 rejected" count.
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from lazeims_common.hashing import sha256_prefixed

from station.config import StationConfig
from station.db import connect
from station.main import create_app

_ph = PasswordHasher()


def _bundle():
    seed = {
        "schools": [{"centre_number": "SCH-1", "name": "School One"}],
        "subjects": [{
            "subject_code": "011", "name": "History", "papers": ["THEORY1"],
            "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
            "groups": [],
            "questions": [
                {"paper_type": "THEORY1", "question_number": "1", "max_marks": "10",
                 "group_code": None, "topics": []},
            ],
        }],
        "students": [
            {"student_id": "S-1", "centre_number": "SCH-1", "first_name": "A",
             "middle_name": "B", "surname": "One", "sex": "M"},
            {"student_id": "S-2", "centre_number": "SCH-1", "first_name": "C",
             "middle_name": None, "surname": "Two", "sex": "F"},
        ],
        "registrations": [{"student_id": "S-1", "subject_code": "011"},
                          {"student_id": "S-2", "subject_code": "011"}],
        "credentials": [{"assignment_id": 42, "role": "DATA_ENTERER",
                         "pin_hash": _ph.hash("123456"), "initials": "JK", "password_hash": None},
                        {"assignment_id": 7, "role": "EXAM_ADMIN",
                         "pin_hash": None, "initials": "ADMIN",
                         "password_hash": _ph.hash("adminpw"), "admin_username": "admin-1"}],
    }
    return {"manifest": {
        "contract_version": "station-package/v1", "package_id": "pkg_a", "package_version": 1,
        "rules_version": "1.0", "software_min_version": "1.0.0", "station_code": "ST-1",
        "exam_id": "FTNA-2026", "configuration_hash": sha256_prefixed(seed),
        "issued_at": "2026-07-27T08:00:00Z",
        "scope": {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]},
    }, "seed": seed}


def _app(tmp_path):
    cfg = StationConfig(data_dir=tmp_path, db_path=tmp_path / "s.sqlite3",
                        secret_key="test-secret-0123456789")
    return create_app(cfg)


@pytest.mark.asyncio
async def test_scope_report_does_not_crash_and_uses_real_columns(tmp_path):
    """Reproduces the KIYABO-CONSORT 500: schools.name (not school_name),
    subjects.subject_code (not code), no exam_subjects table, students have
    no full_name column (must be derived from first/middle/surname).
    """
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await c.post("/api/import", json=_bundle())
        await c.post("/api/login/de", json={"pin": "123456", "initials": "JK"})

        scope = {"centre_number": "SCH-1", "subject_code": "011", "paper_type": "THEORY1"}
        await c.post("/api/locks/acquire", json=scope)
        for sid, mark in (("S-1", 7), ("S-2", 9)):
            await c.put("/api/attendance", json={
                "student_id": sid, "subject_code": "011", "paper_type": "THEORY1",
                "is_present": True, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"})
            await c.put("/api/marks/students", params={"student_id": sid}, json={
                "subject_code": "011", "paper_type": "THEORY1",
                "mode": "TOTAL_MARKS", "total_marks_obtained": mark})

        r = await c.get("/api/scopes/report", params={
            "centre_number": "SCH-1", "subject_code": "011", "paper_type": "THEORY1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["school_name"] == "School One"
        assert body["subject_name"] == "History"
        assert body["total_possible"] == 100
        names = {s["student_id"]: s["full_name"] for s in body["students"]}
        assert names["S-1"] == "A B ONE"
        assert names["S-2"] == "C TWO"
        totals = {s["student_id"]: s["total_marks"] for s in body["students"]}
        assert totals["S-1"] == 7.0
        assert totals["S-2"] == 9.0


@pytest.mark.asyncio
async def test_sync_rejected_lists_reason_codes(tmp_path):
    """The bare rejected_events count in /api/progress doesn't say WHY.
    /api/sync/rejected must expose the natural key + Central's code."""
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await c.post("/api/import", json=_bundle())
        await c.post("/api/login/admin", json={"username": "admin-1", "password": "adminpw"})

        # No rejected events yet
        r0 = await c.get("/api/sync/rejected")
        assert r0.status_code == 200 and r0.json() == []

    # Manually mark an outbox event REJECTED the way run_sync() would after
    # Central responds — avoids needing a live Central in this test.
    conn = connect(tmp_path / "s.sqlite3")
    conn.execute(
        "INSERT INTO outbox_events(event_id, entity_type, operation, natural_key_json,"
        " value_json, local_version, actor_assignment_id, occurred_at, status, attempts, last_error)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("evt_test1", "STUDENT_PAPER_MARKS_REPLACED", "UPSERT",
         '{"student_id": "S-1", "subject_code": "011", "paper_type": "THEORY1"}',
         '{"total_marks_obtained": 7}', 1, 42, "2026-08-04T00:00:00Z",
         "REJECTED", 1, "ATTENDANCE_REQUIRED_FIRST"),
    )
    conn.commit()
    conn.close()

    app2 = _app(tmp_path)  # fresh app instance, same DB file
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://sta") as c:
        await c.post("/api/login/admin", json={"username": "admin-1", "password": "adminpw"})
        r = await c.get("/api/sync/rejected")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["rejection_code"] == "ATTENDANCE_REQUIRED_FIRST"
        assert items[0]["natural_key"]["student_id"] == "S-1"


@pytest.mark.asyncio
async def test_sync_rejected_is_admin_only(tmp_path):
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await c.post("/api/import", json=_bundle())
        await c.post("/api/login/de", json={"pin": "123456", "initials": "JK"})
        r = await c.get("/api/sync/rejected")
        assert r.status_code == 403
