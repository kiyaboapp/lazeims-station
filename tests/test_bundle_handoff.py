"""End-to-end reproduction of the KIYABO-CONSORT bundle hand-off.

The real situation on that computer:

* GEITA had been in use for a while and was the recorded active station,
  with marks already entered.
* A Complete Bundle for SIMIYU was downloaded and setup was re-run.
* The SIMIYU package imported fine, but the login page still opened on GEITA,
  so SIMIYU's username/password was checked against GEITA's user table and
  rejected.

Both stations deliberately use the SAME exam_id here, which is the normal case
for one exam collected by several stations. Nothing may cross-reference: each
(station_code, exam_id) pair owns its own database, its own users and its own
marks.

The setup script writes ``stations/.active`` BEFORE the new station's database
exists (the database is created later, by auto-import at boot). These tests
pin down that this ordering still ends up on the right station.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from lazeims_common.hashing import sha256_prefixed
from station.config import invalidate_active_cfg
from station.db import connect
from station.main import create_app
from station.migrations import apply_migrations, import_package

_ph = PasswordHasher()

EXAM = "a18722b2-22e6-40e1-b004-c9998db8e07f"  # one exam, several stations
NEW_ADMIN_PW = "changed-at-generation-time"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "lazhome"
    root.mkdir()
    monkeypatch.setenv("LAZEIMS_HOME", str(root))
    monkeypatch.delenv("STATION_CODE", raising=False)
    monkeypatch.delenv("STATION_EXAM_ID", raising=False)
    monkeypatch.delenv("STATION_DATA_DIR", raising=False)
    invalidate_active_cfg(root)
    yield root
    invalidate_active_cfg(root)


def _seed_for(station_code: str, *, admin_username: str, admin_password: str,
              assignment_id: int) -> dict:
    return {
        "schools": [{"centre_number": f"C-{station_code}", "name": f"{station_code} School"}],
        "subjects": [{
            "subject_code": "011", "name": "History", "papers": ["THEORY1"],
            "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
            "groups": [], "questions": [],
        }],
        "students": [{
            "student_id": f"{station_code}-STU-1", "centre_number": f"C-{station_code}",
            "first_name": "A", "middle_name": None, "surname": "B", "sex": "M",
        }],
        "registrations": [{"student_id": f"{station_code}-STU-1", "subject_code": "011"}],
        "credentials": [{
            "assignment_id": assignment_id, "role": "EXAM_ADMIN", "pin_hash": None,
            "initials": admin_username, "password_hash": _ph.hash(admin_password),
            "admin_username": admin_username,
        }],
    }


def _manifest_for(station_code: str, seed: dict, version: int) -> dict:
    return {
        "contract_version": "station-package/v1",
        "package_id": f"pkg-{station_code}-v{version}",
        "package_version": version,
        "rules_version": "1.0", "software_min_version": "1.0.0",
        "station_code": station_code, "exam_id": EXAM,
        "configuration_hash": sha256_prefixed(seed),
        "issued_at": "2026-08-23T00:00:00Z",
        "scope": {"schools": [f"C-{station_code}"], "subjects": ["011"], "papers": ["THEORY1"]},
        "data_enterers": [],
    }


def _provision_existing(home: Path, station_code: str, *, admin_username: str,
                        admin_password: str, assignment_id: int) -> Path:
    """A station already installed and in use on this computer."""
    exam_dir = home / "stations" / station_code / "exams" / EXAM
    exam_dir.mkdir(parents=True, exist_ok=True)
    db_path = exam_dir / "station.sqlite3"
    seed = _seed_for(station_code, admin_username=admin_username,
                     admin_password=admin_password, assignment_id=assignment_id)
    conn = connect(db_path)
    try:
        apply_migrations(conn)
        import_package(conn, {"manifest": _manifest_for(station_code, seed, 1), "seed": seed})
        # Marks already entered at this station — must survive everything.
        conn.execute(
            "INSERT OR REPLACE INTO total_marks"
            "(student_id, subject_code, paper_type, total_marks_obtained) VALUES(?,?,?,?)",
            (f"{station_code}-STU-1", "011", "THEORY1", 77.0),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _stage_bundle(home: Path, station_code: str, *, admin_username: str,
                  admin_password: str, assignment_id: int, version: int) -> None:
    """What setup.ps1 does: stage the package, then point .active at it.

    Note the ordering: .active is written while that station has NO database
    yet. It is created later, during auto-import at boot.
    """
    from lazeims_common.signing import sign_package_manifest

    seed = _seed_for(station_code, admin_username=admin_username,
                     admin_password=admin_password, assignment_id=assignment_id)
    manifest = _manifest_for(station_code, seed, version)
    try:
        signature = sign_package_manifest(manifest)
    except Exception:  # pragma: no cover
        pytest.skip("lazeims_common signing keypair not available")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("seed.json", json.dumps(seed))
        zf.writestr("machine-credential.json", json.dumps({}))
        zf.writestr("signature", signature)

    pending = home / "stations" / station_code / "exams" / EXAM / "imports" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / f"{station_code}-v{version}.lazeims-package.zip").write_bytes(buf.getvalue())

    stations = home / "stations"
    stations.mkdir(parents=True, exist_ok=True)
    # BOM included: Windows PowerShell 5.1 writes one.
    (stations / ".active").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(
            {"station_code": station_code, "exam_id": EXAM}).encode("utf-8"))


def _marks(db_path: Path) -> list[tuple]:
    conn = connect(db_path)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT student_id, total_marks_obtained FROM total_marks ORDER BY student_id")]
    finally:
        conn.close()


def test_downloaded_bundle_opens_on_its_own_station(home):
    """SIMIYU bundle installed while GEITA is active -> opens on SIMIYU."""
    geita_db = _provision_existing(
        home, "GEITA", admin_username="GEITA", admin_password="geita-pw", assignment_id=101)
    (home / "stations").mkdir(parents=True, exist_ok=True)
    (home / "stations" / ".active").write_text(
        json.dumps({"station_code": "GEITA", "exam_id": EXAM}), encoding="utf-8")

    _stage_bundle(home, "SIMIYU", admin_username="SIMIYU",
                  admin_password=NEW_ADMIN_PW, assignment_id=202, version=2)

    app = create_app()  # boots: auto-imports, then re-resolves the active station

    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            status = (await c.get("/api/status")).json()
            assert status["station_code"] == "SIMIYU", (
                "the station whose bundle was just installed must be the one "
                f"the login page opens on, got {status['station_code']}")

            # The password set while generating the package authenticates.
            r = await c.post("/api/login/admin",
                             json={"username": "SIMIYU", "password": NEW_ADMIN_PW})
            assert r.status_code == 200, r.text
            assert r.json()["station_code"] == "SIMIYU"

    asyncio.run(_check())

    # GEITA's entered marks are untouched.
    assert _marks(geita_db) == [("GEITA-STU-1", 77.0)]


def test_same_exam_stations_do_not_cross_reference(home):
    """Two stations on one exam keep separate users, students and marks."""
    geita_db = _provision_existing(
        home, "GEITA", admin_username="GEITA", admin_password="geita-pw", assignment_id=101)
    _stage_bundle(home, "SIMIYU", admin_username="SIMIYU",
                  admin_password=NEW_ADMIN_PW, assignment_id=202, version=2)

    app = create_app()
    simiyu_db = home / "stations" / "SIMIYU" / "exams" / EXAM / "station.sqlite3"

    async def _check():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
            # GEITA's admin must NOT authenticate on SIMIYU...
            bad = await c.post("/api/login/admin",
                               json={"username": "GEITA", "password": "geita-pw"})
            assert bad.status_code == 401

            # ...and after switching to GEITA, SIMIYU's admin must not either.
            sw = await c.post("/api/stations/switch",
                              json={"station_code": "GEITA", "exam_id": EXAM})
            assert sw.status_code == 200
            bad2 = await c.post("/api/login/admin",
                                json={"username": "SIMIYU", "password": NEW_ADMIN_PW})
            assert bad2.status_code == 401
            good = await c.post("/api/login/admin",
                                json={"username": "GEITA", "password": "geita-pw"})
            assert good.status_code == 200

    asyncio.run(_check())

    # Separate databases, separate rows — same exam id, no bleed.
    assert geita_db != simiyu_db and simiyu_db.is_file()
    assert _marks(geita_db) == [("GEITA-STU-1", 77.0)]
    assert _marks(simiyu_db) == []
