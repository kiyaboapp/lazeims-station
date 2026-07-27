from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from lazeims_common.hashing import sha256_prefixed

from station.config import StationConfig
from station.main import create_app

_ph = PasswordHasher()


def _bundle():
    seed = {
        "schools": [{"centre_number": "SCH-1", "name": "School One"}],
        "subjects": [{"subject_code": "011", "name": "History", "papers": ["THEORY1"],
                      "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
                      "groups": [], "questions": []}],
        "students": [
            {"student_id": "S-1", "centre_number": "SCH-1", "first_name": "A", "middle_name": None, "surname": "One", "sex": "M"},
            {"student_id": "S-2", "centre_number": "SCH-1", "first_name": "B", "middle_name": None, "surname": "Two", "sex": "F"},
        ],
        "registrations": [{"student_id": "S-1", "subject_code": "011"},
                          {"student_id": "S-2", "subject_code": "011"}],
        "credentials": [{"assignment_id": 42, "role": "DATA_ENTERER",
                         "pin_hash": _ph.hash("123456"), "initials": "JK", "password_hash": None},
                        {"assignment_id": 43, "role": "DATA_ENTERER",
                         "pin_hash": _ph.hash("654321"), "initials": "AB", "password_hash": None}],
        "processing_api_key": None,
    }
    return {"manifest": {
        "contract_version": "station-package/v1", "package_id": "pkg_a", "package_version": 1,
        "rules_version": "1.0", "software_min_version": "1.0.0", "station_code": "ST-1",
        "exam_code": "FTNA-2026", "configuration_hash": sha256_prefixed(seed),
        "issued_at": "2026-07-27T08:00:00Z",
        "scope": {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, "signature": "x",
    }, "seed": seed}


def _app(tmp_path):
    cfg = StationConfig(data_dir=tmp_path, db_path=tmp_path / "s.sqlite3",
                        secret_key="test-secret-0123456789")
    return create_app(cfg)


async def _setup(c):
    await c.post("/api/import", json=_bundle())
    await c.post("/api/login/de", json={"pin": "123456", "initials": "JK"})


@pytest.mark.asyncio
async def test_full_entry_flow_over_http(tmp_path):
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await _setup(c)
        # lock scope
        scope = {"centre_number": "SCH-1", "subject_code": "011", "paper_type": "THEORY1"}
        lk = await c.post("/api/locks/acquire", json=scope)
        assert lk.status_code == 200
        # roster
        r = await c.get("/api/roster", params={"subject_code": "011", "paper_type": "THEORY1", "centre_number": "SCH-1"})
        assert len(r.json()) == 2
        # attendance + marks for both
        for sid, mark in (("S-1", 40), ("S-2", 55)):
            a = await c.put("/api/attendance", json={"student_id": sid, "subject_code": "011",
                            "paper_type": "THEORY1", "is_present": True, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"})
            assert a.status_code == 200
            m = await c.put(f"/api/marks/students/{sid}", json={"subject_code": "011",
                            "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": mark})
            assert m.status_code == 200, m.text
        # finalize passes
        f = await c.post("/api/scopes/finalize", json=scope)
        assert f.status_code == 200 and f.json()["finalized"] is True
        # progress reflects it
        p = (await c.get("/api/progress")).json()
        assert p["finalized_scopes"] == 1 and p["total_marks"] == 2 and p["pending_events"] >= 3


@pytest.mark.asyncio
async def test_no_blank_rejected_over_http(tmp_path):
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await _setup(c)
        await c.put("/api/attendance", json={"student_id": "S-1", "subject_code": "011",
                    "paper_type": "THEORY1", "is_present": True, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"})
        # present but null total -> BLANK_MARK_NOT_ALLOWED
        m = await c.put("/api/marks/students/S-1", json={"subject_code": "011",
                        "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": None})
        assert m.status_code == 422 and m.json()["error"]["code"] == "BLANK_MARK_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_second_browser_blocked_by_lock(tmp_path):
    app = _app(tmp_path)
    scope = {"centre_number": "SCH-1", "subject_code": "011", "paper_type": "THEORY1"}
    # two independent browser sessions on the same station
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c1:
        await c1.post("/api/import", json=_bundle())
        await c1.post("/api/login/de", json={"pin": "123456", "initials": "JK"})
        assert (await c1.post("/api/locks/acquire", json=scope)).status_code == 200
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c2:
            await c2.post("/api/login/de", json={"pin": "654321", "initials": "AB"})
            r = await c2.post("/api/locks/acquire", json=scope)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "LOCKED"
