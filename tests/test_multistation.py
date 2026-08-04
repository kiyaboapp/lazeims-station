"""Multi-station integration tests.

Covers the specific requirement: one computer, many station identities,
switch between them without restart, per-station users, per-station
capabilities, sessions bound to a single station.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from lazeims_common.hashing import sha256_prefixed
from station import paths
from station.config import (
    invalidate_active_cfg,
    list_available_stations,
    read_active_station,
    resolve_active_cfg,
    set_active_station,
)
from station.db import connect
from station.main import SESSION_COOKIE, create_app
from station.migrations import apply_migrations, import_package

_ph = PasswordHasher()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point LAZEIMS_HOME at a clean temp dir and reset resolver caches."""
    root = tmp_path / "lazhome"
    root.mkdir()
    monkeypatch.setenv("LAZEIMS_HOME", str(root))
    monkeypatch.delenv("STATION_CODE", raising=False)
    monkeypatch.delenv("STATION_EXAM_ID", raising=False)
    monkeypatch.delenv("STATION_DATA_DIR", raising=False)
    invalidate_active_cfg(root)
    yield root
    invalidate_active_cfg(root)


def _make_station(home: Path, *, station_code: str, exam_id: str,
                  admin_pw: str = "admin-secret", de_pin: str = "1234",
                  de_initials: str = "AB") -> None:
    """Provision a per-station SQLite DB with one admin and one DE."""
    exam_dir = home / "stations" / station_code / "exams" / exam_id
    exam_dir.mkdir(parents=True, exist_ok=True)
    db_path = exam_dir / "station.sqlite3"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
        seed = {
            "schools": [{"centre_number": f"SCH-{station_code}", "name": f"{station_code} School"}],
            "subjects": [{
                "subject_code": "011", "name": "History",
                "papers": ["THEORY1"],
                "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
                "groups": [], "questions": [],
            }],
            "students": [{
                "student_id": f"{station_code}-STU-1",
                "centre_number": f"SCH-{station_code}",
                "first_name": "S", "middle_name": None, "surname": "One", "sex": "M",
            }],
            "registrations": [{"student_id": f"{station_code}-STU-1", "subject_code": "011"}],
            "credentials": [
                {"assignment_id": hash(station_code) % 10_000 + 1000, "role": "EXAM_ADMIN",
                 "pin_hash": None, "initials": "ADMIN", "password_hash": _ph.hash(admin_pw),
                 "admin_username": f"admin-{station_code.lower()}"},
                {"assignment_id": hash(station_code + de_initials) % 10_000 + 2000,
                 "role": "DATA_ENTERER", "pin_hash": _ph.hash(de_pin),
                 "initials": de_initials, "password_hash": None, "admin_username": None},
            ],
        }
        manifest = {
            "contract_version": "station-package/v1",
            "package_id": f"pkg-{station_code}", "package_version": 1,
            "rules_version": "1.0", "software_min_version": "1.0.0",
            "station_code": station_code, "exam_id": exam_id,
            "configuration_hash": sha256_prefixed(seed),
            "issued_at": "2026-08-04T00:00:00Z",
            "scope": {"schools": [f"SCH-{station_code}"], "subjects": ["011"], "papers": ["THEORY1"]},
            "data_enterers": [],
        }
        import_package(conn, {"manifest": manifest, "seed": seed})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Resolver correctness
# ---------------------------------------------------------------------------

def test_resolver_returns_none_when_multiple_stations_and_no_active(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    _make_station(home, station_code="GEITA-1",  exam_id="EX1")
    cfg = resolve_active_cfg(home, force_reload=True)
    assert cfg.station_code is None
    assert cfg.exam_id is None


def test_resolver_auto_selects_when_only_one_station(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    cfg = resolve_active_cfg(home, force_reload=True)
    assert cfg.station_code == "MWANZA-2"
    assert cfg.exam_id == "EX1"


def test_set_active_station_persists_and_invalidates_cache(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    _make_station(home, station_code="GEITA-1",  exam_id="EX1")

    set_active_station("GEITA-1", "EX1", home=home)
    assert read_active_station(home) == {"station_code": "GEITA-1", "exam_id": "EX1"}

    cfg = resolve_active_cfg(home)
    assert cfg.station_code == "GEITA-1"

    set_active_station("MWANZA-2", "EX1", home=home)
    cfg2 = resolve_active_cfg(home)
    assert cfg2.station_code == "MWANZA-2"
    # DBs must be different — no cross-station data leakage possible.
    assert cfg.db_path != cfg2.db_path


def test_set_active_station_rejects_missing_station(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    with pytest.raises(ValueError):
        set_active_station("NONEXISTENT", "EX1", home=home)


def test_list_available_stations_finds_every_dir(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    _make_station(home, station_code="GEITA-1",  exam_id="EX1")
    _make_station(home, station_code="GEITA-1",  exam_id="EX2")

    codes = {(s["station_code"], s["exam_id"]) for s in list_available_stations(home)}
    assert codes == {("MWANZA-2", "EX1"), ("GEITA-1", "EX1"), ("GEITA-1", "EX2")}


# ---------------------------------------------------------------------------
# 2. HTTP switching flow (the user's core requirement)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_switch_between_stations_no_restart(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    _make_station(home, station_code="GEITA-1",  exam_id="EX1")

    app = create_app()  # multi-station mode, no fixed cfg
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        # Initially chooser mode — /api/status has no station, /api/me is 401
        r = await c.get("/api/stations/available")
        assert r.status_code == 200
        available = r.json()
        assert len(available["stations"]) == 2
        assert available["active"] is None

        # Pick MWANZA-2
        sw = await c.post("/api/stations/switch",
                          json={"station_code": "MWANZA-2", "exam_id": "EX1"})
        assert sw.status_code == 200 and sw.json()["station_code"] == "MWANZA-2"

        # Now status shows MWANZA-2
        st = (await c.get("/api/status")).json()
        assert st["station_code"] == "MWANZA-2"

        # Log in as MWANZA-2 admin
        li = await c.post("/api/login/admin",
                          json={"username": "admin-mwanza-2", "password": "admin-secret"})
        assert li.status_code == 200, li.text
        me = (await c.get("/api/me")).json()
        assert me["station_code"] == "MWANZA-2"
        assert me["role"] == "EXAM_ADMIN"
        assert "admin.users.manage" in me["capabilities"]

        # Switch to GEITA-1 — session cookie must be cleared, no restart needed
        sw2 = await c.post("/api/stations/switch",
                           json={"station_code": "GEITA-1", "exam_id": "EX1"})
        assert sw2.status_code == 200 and sw2.json()["station_code"] == "GEITA-1"

        # /api/me now returns 401 (cookie was deleted)
        me2 = await c.get("/api/me")
        assert me2.status_code == 401

        # /api/status reflects the switch immediately
        st2 = (await c.get("/api/status")).json()
        assert st2["station_code"] == "GEITA-1"


@pytest.mark.asyncio
async def test_session_bound_to_originating_station(home):
    """A cookie signed by MWANZA's secret must never authenticate on GEITA."""
    _make_station(home, station_code="MWANZA-2", exam_id="EX1", admin_pw="mw-pw")
    _make_station(home, station_code="GEITA-1",  exam_id="EX1", admin_pw="ge-pw")

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        # Log in on MWANZA
        await c.post("/api/stations/switch",
                     json={"station_code": "MWANZA-2", "exam_id": "EX1"})
        li = await c.post("/api/login/admin",
                          json={"username": "admin-mwanza-2", "password": "mw-pw"})
        assert li.status_code == 200
        mw_cookie = c.cookies.get(SESSION_COOKIE)
        assert mw_cookie

        # Switch to GEITA — server tells us to drop the cookie. Simulate a
        # misbehaving client that keeps it anyway.
        await c.post("/api/stations/switch",
                     json={"station_code": "GEITA-1", "exam_id": "EX1"})

        # Manually restore the MWANZA cookie
        c.cookies.set(SESSION_COOKIE, mw_cookie)

        me = await c.get("/api/me")
        assert me.status_code == 401  # cookie fails GEITA's signature


@pytest.mark.asyncio
async def test_per_station_users_are_isolated(home):
    """A DE registered on station A cannot log into station B."""
    _make_station(home, station_code="MWANZA-2", exam_id="EX1",
                  de_initials="MW", de_pin="1111")
    _make_station(home, station_code="GEITA-1",  exam_id="EX1",
                  de_initials="GE", de_pin="2222")

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        # Pick MWANZA and try to log in as GEITA's DE — must fail
        await c.post("/api/stations/switch",
                     json={"station_code": "MWANZA-2", "exam_id": "EX1"})
        r = await c.post("/api/login/de", json={"initials": "GE", "pin": "2222"})
        assert r.status_code == 401

        # MWANZA's own DE logs in fine
        ok = await c.post("/api/login/de", json={"initials": "MW", "pin": "1111"})
        assert ok.status_code == 200
        assert ok.json()["role"] == "DATA_ENTERER"
        assert "entry.marks.enter" in ok.json()["capabilities"]
        assert "admin.users.manage" not in ok.json()["capabilities"]


@pytest.mark.asyncio
async def test_capabilities_differ_by_role(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        # Admin session
        li = await c.post("/api/login/admin",
                          json={"username": "admin-mwanza-2", "password": "admin-secret"})
        admin_caps = set(li.json()["capabilities"])
        assert "admin.users.manage" in admin_caps
        assert "admin.package.import" in admin_caps
        assert "entry.marks.enter" in admin_caps

        # DE session (fresh cookies)
        c.cookies.clear()
        li2 = await c.post("/api/login/de", json={"initials": "AB", "pin": "1234"})
        de_caps = set(li2.json()["capabilities"])
        assert "entry.marks.enter" in de_caps
        assert "admin.users.manage" not in de_caps
        assert "admin.package.import" not in de_caps


@pytest.mark.asyncio
async def test_endpoints_refuse_when_no_active_station(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    _make_station(home, station_code="GEITA-1",  exam_id="EX1")

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        # Two stations exist but none is active → chooser mode
        st = (await c.get("/api/status")).json()
        assert st["station_code"] is None

        # /api/status works (returns nulls) but auth'd endpoints refuse
        r = await c.post("/api/login/admin",
                         json={"username": "admin-mwanza-2", "password": "admin-secret"})
        # Login refuses in chooser mode because db() dependency returns 409
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "NO_ACTIVE_STATION"


@pytest.mark.asyncio
async def test_switch_clears_session_cookie(home):
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    _make_station(home, station_code="GEITA-1",  exam_id="EX1")

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await c.post("/api/stations/switch",
                     json={"station_code": "MWANZA-2", "exam_id": "EX1"})
        await c.post("/api/login/admin",
                     json={"username": "admin-mwanza-2", "password": "admin-secret"})
        assert c.cookies.get(SESSION_COOKIE)

        # Switch: server deletes the cookie on the Set-Cookie header
        await c.post("/api/stations/switch",
                     json={"station_code": "GEITA-1", "exam_id": "EX1"})
        assert c.cookies.get(SESSION_COOKIE) in (None, "")


# ---------------------------------------------------------------------------
# 3. Regression: pending packages for a NON-active station must still import
# ---------------------------------------------------------------------------

def _stage_pending_zip(home: Path, *, station_code: str, exam_id: str,
                       filename: str, zip_bytes: bytes) -> Path:
    """Simulate what setup.ps1 does: drop a package zip into that station's
    own imports/pending — regardless of which station is currently active.
    """
    pending = home / "stations" / station_code / "exams" / exam_id / "imports" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    target = pending / filename
    target.write_bytes(zip_bytes)
    return target


def _build_signed_zip_bytes(*, station_code: str, exam_id: str, de_initials: str,
                            de_pin: str) -> bytes:
    """Build an in-memory package ZIP shaped like package_import expects,
    signed with the real Ed25519 test key pair so import_signed_zip succeeds.
    """
    import io
    import json as _json
    import zipfile

    from lazeims_common.signing import sign_package_manifest

    seed = {
        "schools": [{"centre_number": f"SCH-{station_code}", "name": f"{station_code} School"}],
        "subjects": [{
            "subject_code": "011", "name": "History", "papers": ["THEORY1"],
            "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
            "groups": [], "questions": [],
        }],
        "students": [{
            "student_id": f"{station_code}-STU-9", "centre_number": f"SCH-{station_code}",
            "first_name": "Z", "middle_name": None, "surname": "Nine", "sex": "F",
        }],
        "registrations": [{"student_id": f"{station_code}-STU-9", "subject_code": "011"}],
        "credentials": [
            {"assignment_id": abs(hash(station_code + "adm")) % 10_000 + 3000,
             "role": "EXAM_ADMIN", "pin_hash": None,
             "password_hash": _ph.hash("zip-admin-pw"), "initials": "ADMIN",
             "admin_username": f"admin-{station_code.lower()}-zip"},
            {"assignment_id": abs(hash(station_code + de_initials)) % 10_000 + 4000,
             "role": "DATA_ENTERER", "pin_hash": _ph.hash(de_pin),
             "initials": de_initials, "password_hash": None, "admin_username": None},
        ],
    }
    manifest = {
        "contract_version": "station-package/v1",
        "package_id": f"pkg-{station_code}-zip", "package_version": 1,
        "rules_version": "1.0", "software_min_version": "1.0.0",
        "station_code": station_code, "exam_id": exam_id,
        "configuration_hash": sha256_prefixed(seed),
        "issued_at": "2026-08-04T00:00:00Z",
        "scope": {"schools": [f"SCH-{station_code}"], "subjects": ["011"], "papers": ["THEORY1"]},
        "data_enterers": [],
    }
    signature = sign_package_manifest(manifest)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _json.dumps(manifest))
        zf.writestr("seed.json", _json.dumps(seed))
        zf.writestr("machine-credential.json", _json.dumps({}))
        zf.writestr("signature", signature)
    return buf.getvalue()


def test_pending_package_for_inactive_station_still_imports_on_boot(home, monkeypatch):
    """Reproduces the KIYABO-CONSORT bug: MWANZA-2 is already the active
    station with data; a GEITA-1 package sitting in its own imports/pending
    must still be picked up on the next boot, even though GEITA-1 is not
    (and has never been) the active station.
    """
    # MWANZA-2 already fully provisioned and made active (simulates "ran once
    # already" from a prior boot).
    _make_station(home, station_code="MWANZA-2", exam_id="EX1")
    set_active_station("MWANZA-2", "EX1", home=home)

    # GEITA-1 has NO database yet — only a pending package, exactly like the
    # setup script staging a fresh Complete Bundle package before first boot.
    try:
        zip_bytes = _build_signed_zip_bytes(
            station_code="GEITA-1", exam_id="EX1", de_initials="GD", de_pin="9999")
    except Exception:
        pytest.skip("lazeims_common signing keypair not available in this environment")
    _stage_pending_zip(home, station_code="GEITA-1", exam_id="EX1",
                       filename="GEITA-1-v1.lazeims-package.zip", zip_bytes=zip_bytes)

    # Boot the app exactly as production does: no fixed cfg, home_root is
    # LAZEIMS_HOME (patched by the `home` fixture).
    app = create_app()

    import asyncio
    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            # MWANZA-2 remains active (untouched)...
            st = (await c.get("/api/status")).json()
            assert st["station_code"] == "MWANZA-2"

            # ...but GEITA-1's package was imported into GEITA-1's own DB on
            # boot, even though it was never the active station.
            avail = (await c.get("/api/stations/available")).json()
            geita = next(s for s in avail["stations"]
                        if s["station_code"] == "GEITA-1" and s["exam_id"] == "EX1")
            assert geita["students"] == 1  # the one seeded student imported

            # And it's reachable by switching to it — no reinstall needed.
            sw = await c.post("/api/stations/switch",
                              json={"station_code": "GEITA-1", "exam_id": "EX1"})
            assert sw.status_code == 200
            li = await c.post("/api/login/de", json={"initials": "GD", "pin": "9999"})
            assert li.status_code == 200, li.text

    asyncio.run(_check())
