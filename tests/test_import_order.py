"""Pending-package import ordering.

A station accumulates many package versions in ``imports/pending`` (v1 … v11).
They MUST be imported in ascending package_version order so that the newest
package's credentials and scopes are the ones left in place.

Sorting the filenames as plain strings puts ``-v10`` and ``-v11`` *before*
``-v2``, so the last package applied would be v9 — silently reverting an
admin password that was changed in v11. This is the KIYABO-CONSORT symptom:
"I changed the password and regenerated the package but login still fails".
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from lazeims_common.hashing import sha256_prefixed
from station.auth import authenticate_admin
from station.auto_import import auto_import_pending
from station.config import invalidate_active_cfg, resolve_active_cfg
from station.db import connect
from station.migrations import apply_migrations

_ph = PasswordHasher()

STATION = "SIMIYU"
EXAM = "EX1"
ADMIN_ASSIGNMENT_ID = 7001
ADMIN_USERNAME = "SIMIYU"


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


def _signed_zip(*, version: int, admin_password: str) -> bytes:
    """Build a signed package whose admin credential carries ``admin_password``.

    Every version reuses the same ``assignment_id`` so the station upserts the
    same row — exactly how a regenerated package updates an existing login.
    """
    from lazeims_common.signing import sign_package_manifest

    seed = {
        "schools": [{"centre_number": "S-1", "name": "Simiyu School"}],
        "subjects": [{
            "subject_code": "011", "name": "History", "papers": ["THEORY1"],
            "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0},
            "groups": [], "questions": [],
        }],
        "students": [{
            "student_id": "STU-1", "centre_number": "S-1",
            "first_name": "A", "middle_name": None, "surname": "B", "sex": "M",
        }],
        "registrations": [{"student_id": "STU-1", "subject_code": "011"}],
        "credentials": [{
            "assignment_id": ADMIN_ASSIGNMENT_ID,
            "role": "EXAM_ADMIN",
            "pin_hash": None,
            "initials": ADMIN_USERNAME,
            "password_hash": _ph.hash(admin_password),
            "admin_username": ADMIN_USERNAME,
        }],
    }
    manifest = {
        "contract_version": "station-package/v1",
        "package_id": f"pkg-{STATION}-v{version}",
        "package_version": version,
        "rules_version": "1.0",
        "software_min_version": "1.0.0",
        "station_code": STATION,
        "exam_id": EXAM,
        "configuration_hash": sha256_prefixed(seed),
        "issued_at": "2026-08-23T00:00:00Z",
        "scope": {"schools": ["S-1"], "subjects": ["011"], "papers": ["THEORY1"]},
        "data_enterers": [],
    }
    signature = sign_package_manifest(manifest)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("seed.json", json.dumps(seed))
        zf.writestr("machine-credential.json", json.dumps({}))
        zf.writestr("signature", signature)
    return buf.getvalue()


def _stage(home: Path, version: int, admin_password: str) -> None:
    pending = home / "stations" / STATION / "exams" / EXAM / "imports" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    name = f"{STATION}-v{version}.lazeims-package.zip"
    try:
        data = _signed_zip(version=version, admin_password=admin_password)
    except Exception:  # pragma: no cover - signing key absent
        pytest.skip("lazeims_common signing keypair not available")
    (pending / name).write_bytes(data)


def test_newest_package_version_wins_even_past_v9(home):
    """v10/v11 must apply AFTER v2, so the newest password is the live one."""
    # Passwords are unique per version so we can tell which one landed last.
    passwords = {v: f"pw-v{v}" for v in [1, 2, 3, 9, 10, 11]}
    for v, pw in passwords.items():
        _stage(home, v, pw)

    cfg = resolve_active_cfg(home, force_reload=True)
    auto_import_pending(cfg)

    db_path = home / "stations" / STATION / "exams" / EXAM / "station.sqlite3"
    assert db_path.is_file(), "auto-import should have created the station DB"

    conn = connect(db_path)
    try:
        apply_migrations(conn)
        newest = authenticate_admin(conn, ADMIN_USERNAME, passwords[11])
        stale = authenticate_admin(conn, ADMIN_USERNAME, passwords[9])
    finally:
        conn.close()

    assert newest is not None, (
        "the v11 password must authenticate: packages have to be imported in "
        "ascending package_version order, not lexicographic filename order"
    )
    assert stale is None, "an older package's password must not survive"
