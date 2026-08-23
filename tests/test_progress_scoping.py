"""GET /api/progress scoping.

A Data Enterer's dashboard must show that DE's own progress. It used to pass
``?user_id=<assignment_id>`` from the browser, which broke two ways:

* ``/api/me`` returns no ``assignment_id`` (the session holds only uid/role/
  station/exam), so after a page reload the query string was literally
  ``user_id=undefined`` and FastAPI answered 422.
* the endpoint reads ``user_id`` as a ``station_users.id``, not an
  ``assignment_id``. When the two differed no row matched, the scope filter was
  dropped, and the DE silently saw station-wide totals.

Progress for a DE is now derived from the session.
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

# Deliberately different values so an id/assignment_id mix-up cannot pass by
# coincidence.
DE_ASSIGNMENT_ID = 9101
ADMIN_ASSIGNMENT_ID = 9102


@pytest.fixture
def app_and_db(tmp_path):
    data_dir = tmp_path / "station"
    data_dir.mkdir()
    db_path = data_dir / "station.sqlite3"

    seed = {
        "schools": [
            {"centre_number": "C-MINE", "name": "In scope"},
            {"centre_number": "C-OTHER", "name": "Out of scope"},
        ],
        "subjects": [{
            "subject_code": "011", "name": "History", "papers": ["THEORY1"],
            "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
            "groups": [], "questions": [],
        }],
        "students": [
            {"student_id": "S-MINE", "centre_number": "C-MINE",
             "first_name": "A", "middle_name": None, "surname": "B", "sex": "M"},
            {"student_id": "S-OTHER", "centre_number": "C-OTHER",
             "first_name": "C", "middle_name": None, "surname": "D", "sex": "F"},
        ],
        "registrations": [
            {"student_id": "S-MINE", "subject_code": "011"},
            {"student_id": "S-OTHER", "subject_code": "011"},
        ],
        "credentials": [
            {"assignment_id": DE_ASSIGNMENT_ID, "role": "DATA_ENTERER",
             "pin_hash": _ph.hash("1234"), "initials": "DE",
             "password_hash": None, "admin_username": None},
            {"assignment_id": ADMIN_ASSIGNMENT_ID, "role": "EXAM_ADMIN",
             "pin_hash": None, "initials": "ADMIN",
             "password_hash": _ph.hash("admin-pw"), "admin_username": "admin"},
        ],
    }
    manifest = {
        "contract_version": "station-package/v1",
        "package_id": "pkg-progress", "package_version": 1,
        "rules_version": "1.0", "software_min_version": "1.0.0",
        "station_code": "ST-1", "exam_id": "EX1",
        "configuration_hash": sha256_prefixed(seed),
        "issued_at": "2026-08-23T00:00:00Z",
        "scope": {"schools": ["C-MINE", "C-OTHER"], "subjects": ["011"], "papers": ["THEORY1"]},
        # The DE is restricted to C-MINE only.
        "data_enterers": [{
            "assignment_id": DE_ASSIGNMENT_ID,
            "school_centre_numbers": ["C-MINE"],
            "subject_codes": [],
        }],
    }

    conn = connect(db_path)
    try:
        apply_migrations(conn)
        import_package(conn, {"manifest": manifest, "seed": seed})
    finally:
        conn.close()

    cfg = StationConfig(data_dir=data_dir, db_path=db_path, secret_key="k" * 32,
                        station_code="ST-1", exam_id="EX1")
    return create_app(cfg), db_path


def _row_id_for(db_path, assignment_id: int) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute(
            "SELECT id FROM station_users WHERE assignment_id=?",
            (assignment_id,)).fetchone()["id"])
    finally:
        conn.close()


def test_de_progress_needs_no_user_id_and_is_scoped(app_and_db):
    app, _ = app_and_db

    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            assert (await c.post("/api/login/de",
                                 json={"initials": "DE", "pin": "1234"})).status_code == 200

            # The dashboard now calls it with no query string at all.
            r = await c.get("/api/progress")
            assert r.status_code == 200, r.text
            # Scoped to C-MINE: the out-of-scope centre is not counted.
            assert r.json()["total_scopes"] == 1, r.json()

    asyncio.run(_check())


def test_admin_progress_is_station_wide(app_and_db):
    app, _ = app_and_db

    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            assert (await c.post("/api/login/admin",
                                 json={"username": "admin", "password": "admin-pw"})
                    ).status_code == 200
            r = await c.get("/api/progress")
            assert r.status_code == 200, r.text
            # An admin sees both centres.
            assert r.json()["total_scopes"] == 2, r.json()

    asyncio.run(_check())


def test_de_stays_on_own_scope_whatever_user_id_is_passed(app_and_db):
    app, db_path = app_and_db
    admin_row_id = _row_id_for(db_path, ADMIN_ASSIGNMENT_ID)
    de_row_id = _row_id_for(db_path, DE_ASSIGNMENT_ID)

    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            await c.post("/api/login/de", json={"initials": "DE", "pin": "1234"})
            for probe in (admin_row_id, de_row_id, 99999):
                r = await c.get(f"/api/progress?user_id={probe}")
                assert r.status_code == 200, r.text
                assert r.json()["total_scopes"] == 1, (probe, r.json())

    asyncio.run(_check())
