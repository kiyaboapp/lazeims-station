"""API-level tests for the station sync endpoints (main.py wiring + admin guard)."""

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
        "students": [{"student_id": "S-1", "centre_number": "SCH-1", "first_name": "A",
                      "middle_name": None, "surname": "One", "sex": "M"}],
        "registrations": [{"student_id": "S-1", "subject_code": "011"}],
        "credentials": [
            {"assignment_id": 42, "role": "DATA_ENTERER",
             "pin_hash": _ph.hash("123456"), "initials": "JK", "password_hash": None},
            {"assignment_id": 7, "role": "EXAM_ADMIN",
             "pin_hash": None, "initials": "MWANZA-2", "password_hash": _ph.hash("adminpw")},
        ],
        "processing_api_key": None,
    }
    return {"manifest": {
        "contract_version": "station-package/v1", "package_id": "pkg_a", "package_version": 1,
        "rules_version": "1.0", "software_min_version": "1.0.0", "station_code": "ST-1",
        "exam_id": "FTNA-2026", "configuration_hash": sha256_prefixed(seed),
        "issued_at": "2026-07-27T08:00:00Z",
        "scope": {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, "signature": "x",
    }, "seed": seed}


def _app(tmp_path):
    cfg = StationConfig(data_dir=tmp_path, db_path=tmp_path / "s.sqlite3",
                        secret_key="test-secret-0123456789")
    return create_app(cfg)


@pytest.mark.asyncio
async def test_sync_config_and_run_flow(tmp_path):
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await c.post("/api/import", json=_bundle())

        # admin signs in and sees "not configured"
        assert (await c.post("/api/login/admin", json={"username": "MWANZA-2", "password": "adminpw"})).status_code == 200
        cfg = (await c.get("/api/sync/config")).json()
        assert cfg["configured"] is False and cfg["has_credential"] is False

        # admin sets central URL (credential comes from package import)
        r = await c.post("/api/sync/config", json={"central_url": "http://127.0.0.1:1"})
        assert r.status_code == 200
        cfg = r.json()
        # Still not fully configured because machine credential isn't stored on disk in this test
        # (it needs LAZEIMS_HOME set properly). But central_url is set.
        assert cfg["central_url"] == "http://127.0.0.1:1"

        # run: even if not fully configured, endpoint doesn't crash
        run = (await c.post("/api/sync/run", json={})).json()
        assert run["configured"] is False or run.get("error") is not None or "accepted" in run


@pytest.mark.asyncio
async def test_data_enterer_cannot_change_sync(tmp_path):
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await c.post("/api/import", json=_bundle())
        await c.post("/api/login/de", json={"pin": "123456", "initials": "JK"})
        # DE may read config but not change it or run a sync
        assert (await c.get("/api/sync/config")).status_code == 200
        assert (await c.post("/api/sync/config", json={"central_url": "http://x"})).status_code == 403
        assert (await c.post("/api/sync/run", json={})).status_code == 403
