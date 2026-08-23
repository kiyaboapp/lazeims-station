"""Setup hand-off to the station it was downloaded for.

Two guarantees are locked in here:

1. Installing a Complete Bundle for station X makes X the station the login
   page opens on — writing ``stations/.active`` the way the setup scripts do,
   including a UTF-8 BOM (Windows PowerShell 5.1 writes one by default).

2. Doing so NEVER touches any other station's data. One computer serves many
   stations in sequence (GEITA, then SIMIYU, ...); marks already entered for
   the others must still be there and still reachable by switching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from lazeims_common.hashing import sha256_prefixed
from station.config import (
    invalidate_active_cfg,
    list_available_stations,
    read_active_station,
    resolve_active_cfg,
)
from station.db import connect
from station.migrations import apply_migrations, import_package

_ph = PasswordHasher()


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


def _provision(home: Path, station_code: str, exam_id: str) -> Path:
    """Create a station DB with one student, as a real import would."""
    exam_dir = home / "stations" / station_code / "exams" / exam_id
    exam_dir.mkdir(parents=True, exist_ok=True)
    db_path = exam_dir / "station.sqlite3"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
        seed = {
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
            "credentials": [],
        }
        manifest = {
            "contract_version": "station-package/v1",
            "package_id": f"pkg-{station_code}", "package_version": 1,
            "rules_version": "1.0", "software_min_version": "1.0.0",
            "station_code": station_code, "exam_id": exam_id,
            "configuration_hash": sha256_prefixed(seed),
            "issued_at": "2026-08-23T00:00:00Z",
            "scope": {"schools": [f"C-{station_code}"], "subjects": ["011"], "papers": ["THEORY1"]},
            "data_enterers": [],
        }
        import_package(conn, {"manifest": manifest, "seed": seed})
    finally:
        conn.close()
    return db_path


def _enter_marks(db_path: Path, student_id: str, total: float) -> None:
    """Simulate marks already entered at this station."""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO total_marks"
            "(student_id, subject_code, paper_type, total_marks_obtained) VALUES(?,?,?,?)",
            (student_id, "011", "THEORY1", total),
        )
        conn.commit()
    finally:
        conn.close()


def _count_marks(db_path: Path) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) c FROM total_marks").fetchone()["c"])
    finally:
        conn.close()


def _write_active_like_setup(home: Path, station_code: str, exam_id: str, *, bom: bool) -> None:
    """Write stations/.active exactly as the setup scripts do."""
    stations = home / "stations"
    stations.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"station_code": station_code, "exam_id": exam_id})
    data = payload.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    (stations / ".active").write_bytes(data)


@pytest.mark.parametrize("bom", [False, True], ids=["no-bom", "utf8-bom"])
def test_bundle_station_becomes_active(home, bom):
    """The station the bundle was downloaded for is the one that opens."""
    _provision(home, "GEITA", "EX1")
    _provision(home, "SIMIYU", "EX1")

    _write_active_like_setup(home, "SIMIYU", "EX1", bom=bom)

    assert read_active_station(home) == {"station_code": "SIMIYU", "exam_id": "EX1"}
    cfg = resolve_active_cfg(home, force_reload=True)
    assert cfg.station_code == "SIMIYU"
    assert cfg.exam_id == "EX1"


def test_activating_one_station_preserves_every_other_stations_marks(home):
    """Switching the active pointer must not disturb data already entered.

    One computer fills GEITA, then a SIMIYU bundle is installed. GEITA's
    entered marks must survive and GEITA must stay listed and selectable.
    """
    geita_db = _provision(home, "GEITA", "EX1")
    mwanza_db = _provision(home, "MWANZA-2", "EX1")
    _provision(home, "SIMIYU", "EX1")

    _enter_marks(geita_db, "GEITA-STU-1", 71.0)
    _enter_marks(mwanza_db, "MWANZA-2-STU-1", 64.0)

    # Installing the SIMIYU bundle points .active at SIMIYU.
    _write_active_like_setup(home, "SIMIYU", "EX1", bom=True)
    assert resolve_active_cfg(home, force_reload=True).station_code == "SIMIYU"

    # Other stations' databases and marks are untouched.
    assert geita_db.is_file() and mwanza_db.is_file()
    assert _count_marks(geita_db) == 1
    assert _count_marks(mwanza_db) == 1

    # And all three remain listed for the operator to switch between.
    listed = {(s["station_code"], s["exam_id"]) for s in list_available_stations(home)}
    assert listed == {("GEITA", "EX1"), ("MWANZA-2", "EX1"), ("SIMIYU", "EX1")}
