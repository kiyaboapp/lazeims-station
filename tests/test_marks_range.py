"""Over-max marks: the 422s seen at S0439 / 032 / THEORY1.

``isValidMark`` in the browser only checked "finite and >= 0", with no upper
bound, while the server enforces the paper maximum. A DE could therefore type a
mark above the maximum, see it accepted locally, and have the save refused with
``MARK_OUT_OF_RANGE`` — and the entry screen discarded the reason, so the only
visible symptom was "N failed" followed by an unexplained 409 on finalize.

These tests pin down the two things the fix depends on:

* ``/api/scopes`` publishes ``paper_max`` so the browser can check before saving;
* the server still rejects an over-max mark, and accepts one at exactly the max.
"""

from __future__ import annotations

import asyncio

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from lazeims_common.hashing import sha256_prefixed
from station.config import StationConfig
from station.db import connect
from station.main import create_app
from station.migrations import apply_migrations, import_package

_ph = PasswordHasher()

PAPER_MAX = 50  # deliberately not 100, so a "valid looking" 70 is out of range
DE_ASSIGNMENT_ID = 8801


@pytest.fixture
def app():
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "station.sqlite3"

    seed = {
        "schools": [{"centre_number": "S0439", "name": "Test School"}],
        "subjects": [{
            "subject_code": "032", "name": "Physics", "papers": ["THEORY1"],
            "total_marks": {"THEORY1": PAPER_MAX, "THEORY2": 0, "PRACTICAL": 0},
            "groups": [], "questions": [],   # no questions -> TOTAL_MARKS mode
        }],
        "students": [{
            "student_id": "S0439/0001", "centre_number": "S0439",
            "first_name": "A", "middle_name": None, "surname": "B", "sex": "M",
        }],
        "registrations": [{"student_id": "S0439/0001", "subject_code": "032"}],
        "credentials": [{
            "assignment_id": DE_ASSIGNMENT_ID, "role": "DATA_ENTERER",
            "pin_hash": _ph.hash("1234"), "initials": "DE",
            "password_hash": None, "admin_username": None,
        }],
    }
    manifest = {
        "contract_version": "station-package/v1",
        "package_id": "pkg-range", "package_version": 1,
        "rules_version": "1.0", "software_min_version": "1.0.0",
        "station_code": "ST-1", "exam_id": "EX1",
        "configuration_hash": sha256_prefixed(seed),
        "issued_at": "2026-08-23T00:00:00Z",
        "scope": {"schools": ["S0439"], "subjects": ["032"], "papers": ["THEORY1"]},
        "data_enterers": [],
    }

    conn = connect(db_path)
    try:
        apply_migrations(conn)
        import_package(conn, {"manifest": manifest, "seed": seed})
    finally:
        conn.close()

    cfg = StationConfig(data_dir=tmp, db_path=db_path, secret_key="k" * 32,
                        station_code="ST-1", exam_id="EX1")
    return create_app(cfg)


async def _login_and_attend(c) -> None:
    assert (await c.post("/api/login/de",
                         json={"initials": "DE", "pin": "1234"})).status_code == 200
    # Attendance must exist first, otherwise the rejection would be
    # ATTENDANCE_REQUIRED_FIRST instead of the range check under test.
    r = await c.put("/api/attendance", json={
        "student_id": "S0439/0001", "subject_code": "032",
        "paper_type": "THEORY1", "is_present": True,
    })
    assert r.status_code == 200, r.text


def _put_mark(c, value):
    return c.put("/api/marks/students?student_id=S0439%2F0001", json={
        "subject_code": "032", "paper_type": "THEORY1",
        "mode": "TOTAL_MARKS", "total_marks_obtained": value,
    })


def test_scopes_publishes_paper_max(app):
    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            await _login_and_attend(c)
            scopes = (await c.get("/api/scopes")).json()
            t1 = next(s for s in scopes if s["paper_type"] == "THEORY1")
            assert t1["paper_max"] == PAPER_MAX, t1

    asyncio.run(_check())


def test_mark_above_paper_max_is_rejected_with_a_reason(app):
    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            await _login_and_attend(c)

            r = await _put_mark(c, PAPER_MAX + 20)
            assert r.status_code == 422, r.text
            body = r.json()
            # The entry screen renders this message, so it must be present.
            assert body["error"]["code"] == "MARK_OUT_OF_RANGE", body
            assert body["error"]["message"], body

    asyncio.run(_check())


def test_mark_exactly_at_paper_max_is_accepted(app):
    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            await _login_and_attend(c)
            r = await _put_mark(c, PAPER_MAX)
            assert r.status_code == 200, r.text

    asyncio.run(_check())
