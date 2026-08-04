from __future__ import annotations

import copy
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from lazeims_common.hashing import sha256_prefixed

from station.config import StationConfig
from station.db import connect, get_user_version
from station.main import create_app
from station.migrations import PackageImportError, apply_migrations, import_package

_ph = PasswordHasher()
KNOWN_PIN = "123456"


def _seed(with_credential=True):
    seed = {
        "schools": [{"centre_number": "SCH-1", "name": "School One"}],
        "subjects": [{
            "subject_code": "011", "name": "History",
            "papers": ["THEORY1"],
            "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
            "groups": [],
            "questions": [
                {"paper_type": "THEORY1", "question_number": "1", "max_marks": "10",
                 "group_code": None, "topics": []},
            ],
        }],
        "students": [{"student_id": "S1-IN", "centre_number": "SCH-1",
                      "first_name": "In", "middle_name": None, "surname": "Scope", "sex": "M"}],
        "registrations": [{"student_id": "S1-IN", "subject_code": "011"}],
        "credentials": [],
        "processing_api_key": None,
    }
    if with_credential:
        seed["credentials"].append({
            "assignment_id": 42, "role": "DATA_ENTERER",
            "pin_hash": _ph.hash(KNOWN_PIN), "initials": "JK", "password_hash": None,
        })
    return seed


def _bundle(seed=None, *, rules="1.0", software_min="1.0.0",
            station_code="ST-1", exam_id="FTNA-2026", break_hash=False):
    seed = seed if seed is not None else _seed()
    config_hash = sha256_prefixed(seed)
    if break_hash:
        config_hash = "sha256:deadbeef"
    manifest = {
        "contract_version": "station-package/v1",
        "package_id": "pkg_test1", "package_version": 1,
        "rules_version": rules, "software_min_version": software_min,
        "station_code": station_code, "exam_id": exam_id,
        "configuration_hash": config_hash,
        "issued_at": "2026-07-27T08:00:00Z",
        "scope": {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]},
        "signature": "hmac-sha256:ignored-by-station",
    }
    return {"manifest": manifest, "seed": seed}


def _fresh_conn(tmp_path: Path):
    conn = connect(tmp_path / "s.sqlite3")
    apply_migrations(conn)
    return conn


# ---------- migration ----------

def test_migration_sets_user_version_and_tables(tmp_path):
    conn = _fresh_conn(tmp_path)
    from station import SCHEMA_VERSION as _SV; assert get_user_version(conn) == _SV
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ["station_meta", "packages", "station_users", "schools", "subjects",
              "questions", "students", "attendance", "total_marks", "item_marks", "outbox_events"]:
        assert t in tables


# ---------- import ----------

def test_import_valid_bundle(tmp_path):
    conn = _fresh_conn(tmp_path)
    result = import_package(conn, _bundle())
    assert result["schools"] == 1 and result["subjects"] == 1 and result["students"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM students").fetchone()["c"] == 1
    assert conn.execute("SELECT value FROM station_meta WHERE key='station_code'").fetchone()["value"] == "ST-1"


def test_import_rejects_unsupported_rules(tmp_path):
    conn = _fresh_conn(tmp_path)
    with pytest.raises(PackageImportError) as exc:
        import_package(conn, _bundle(rules="9.9"))
    assert exc.value.code == "UPGRADE_REQUIRED"


def test_import_rejects_newer_software_requirement(tmp_path):
    conn = _fresh_conn(tmp_path)
    with pytest.raises(PackageImportError) as exc:
        import_package(conn, _bundle(software_min="99.0.0"))
    assert exc.value.code == "UPGRADE_REQUIRED"


def test_import_rejects_hash_mismatch(tmp_path):
    conn = _fresh_conn(tmp_path)
    with pytest.raises(PackageImportError) as exc:
        import_package(conn, _bundle(break_hash=True))
    assert exc.value.code == "CONFIGURATION_MISMATCH"


def test_import_rejects_wrong_station_after_adoption(tmp_path):
    conn = _fresh_conn(tmp_path)
    import_package(conn, _bundle(station_code="ST-1"))
    with pytest.raises(PackageImportError) as exc:
        import_package(conn, _bundle(station_code="ST-2"))
    assert exc.value.code == "CONFIGURATION_MISMATCH"


def test_import_rejects_wrong_exam_after_adoption(tmp_path):
    conn = _fresh_conn(tmp_path)
    import_package(conn, _bundle(exam_id="FTNA-2026"))
    with pytest.raises(PackageImportError) as exc:
        import_package(conn, _bundle(exam_id="CSEE-2026"))
    assert exc.value.code == "CONFIGURATION_MISMATCH"


# ---------- offline app smoke + local auth ----------

def _app(tmp_path):
    cfg = StationConfig(data_dir=tmp_path, db_path=tmp_path / "s.sqlite3",
                        secret_key="test-station-secret-key-0123456789")
    return create_app(cfg), cfg


@pytest.mark.asyncio
async def test_health_and_status_offline(tmp_path):
    app, cfg = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        h = await c.get("/health")
        assert h.status_code == 200 and h.json()["status"] == "ok"
        s = await c.get("/api/status")
        assert s.status_code == 200 and s.json()["station_code"] is None  # nothing imported yet


@pytest.mark.asyncio
async def test_import_then_de_login_offline(tmp_path):
    app, cfg = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        imp = await c.post("/api/import", json=_bundle())
        assert imp.status_code == 200, imp.text
        assert imp.json()["students"] == 1

        # DE login with correct PIN+initials
        ok = await c.post("/api/login/de", json={"pin": KNOWN_PIN, "initials": "JK"})
        assert ok.status_code == 200 and ok.json()["role"] == "DATA_ENTERER"
        # /me resolves the session
        me = await c.get("/api/me")
        assert me.status_code == 200 and me.json()["role"] == "DATA_ENTERER"

        # wrong PIN rejected
        bad = await c.post("/api/login/de", json={"pin": "000000", "initials": "JK"})
        assert bad.status_code == 401


@pytest.mark.asyncio
async def test_import_via_api_rejects_wrong_target(tmp_path):
    app, cfg = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sta") as c:
        await c.post("/api/import", json=_bundle(station_code="ST-1"))
        r = await c.post("/api/import", json=_bundle(station_code="ST-2"))
        assert r.status_code == 422 and r.json()["error"]["code"] == "CONFIGURATION_MISMATCH"


# ---------- offline asset check (no CDN) ----------

def test_static_assets_have_no_cdn_references():
    """HTML pages may use known CDN resources (Tailwind, Chart.js).
    JS/CSS assets must not reference arbitrary remote URLs."""
    static = Path(__file__).resolve().parents[1] / "station" / "static"
    allowed_cdn_hosts = {"cdn.tailwindcss.com", "cdn.jsdelivr.net"}
    for name in ["app.css", "app.js"]:
        p = static / name
        if not p.exists():
            continue  # removed in multi-page rewrite
        text = p.read_text()
        assert "http://" not in text and "https://" not in text, f"{name} references a remote URL"
    # HTML pages are allowed to reference known CDN hosts only
    for html_file in static.glob("*.html"):
        text = html_file.read_text()
        import re
        urls = re.findall(r'https?://([^/\s"\'<>]+)', text)
        for url_host in urls:
            assert any(url_host == cdn for cdn in allowed_cdn_hosts), (
                f"{html_file.name} references unexpected remote URL: {url_host}"
            )
