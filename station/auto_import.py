"""Auto-import bundled/signed packages on station boot.

Scans ``imports/pending/*.zip`` (signed exam packages) and legacy
``station_data/import/*.json`` bundle files, imports each, and moves the file
to ``imports/imported/`` or ``imports/failed/``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from . import package_import, paths
from .config import StationConfig
from .db import connect
from .migrations import PackageImportError


def _move(path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / path.name
    if target.exists():
        target.unlink()
    shutil.move(str(path), str(target))


def _pending_zips(root: Path) -> list[Path]:
    p = root / "imports" / "pending"
    return sorted(p.glob("*.zip")) if p.is_dir() else []


def _legacy_json_bundles(data_dir: Path) -> list[Path]:
    p = data_dir / "import"
    return sorted(p.glob("*.json")) if p.is_dir() else []


def _already_imported(conn: sqlite3.Connection, package_id: str | None) -> bool:
    if not package_id:
        return False
    return conn.execute(
        "SELECT 1 FROM packages WHERE package_id = ?", (package_id,)
    ).fetchone() is not None


def auto_import_pending(cfg: StationConfig, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Import signed ZIPs first, then any legacy JSON bundles.

    Always sweeps **every** ``stations/<code>/exams/<id>/imports/pending/``
    directory under the LAZEIMS home, not just the currently-active one. This
    matters because a computer running multiple stations may receive a fresh
    package for a station that is not currently selected — e.g. GEITA-1
    packages arriving while MWANZA-2 is active. If we only imported into the
    active station's DB, those packages would sit untouched forever and the
    operator would never see them, even though the setup script correctly
    staged them into GEITA-1's own ``imports/pending``.

    A pre-staged Complete Bundle package is therefore discovered and imported
    automatically on first boot (or any boot) without manual UI steps,
    regardless of which station happens to be active at that moment.
    """
    results: list[dict] = []

    home = cfg.lazeims_home or paths.lazeims_home()
    stations_root = home / "stations"
    if stations_root.is_dir():
        for stn in sorted(stations_root.iterdir()):
            if not stn.is_dir() or stn.name.startswith("."):
                continue
            exams_dir = stn / "exams"
            if not exams_dir.is_dir():
                continue
            for ex in sorted(exams_dir.iterdir()):
                if not ex.is_dir():
                    continue
                pending = ex / "imports" / "pending"
                if not pending.is_dir() or not any(pending.glob("*.zip")):
                    continue
                # Import into THIS station/exam's own DB — never the active
                # station's DB unless they happen to be the same directory.
                exam_db_path = ex / "station.sqlite3"
                if cfg.station_code == stn.name and cfg.exam_id == ex.name and conn is not None:
                    # Caller already has a live connection open to this exact
                    # DB (used by tests) — reuse it instead of opening a
                    # second connection to the same SQLite file.
                    _import_from_dir(conn, ex, results)
                    continue
                exam_conn = connect(exam_db_path)
                try:
                    from .migrations import apply_migrations
                    apply_migrations(exam_conn)
                    _import_from_dir(exam_conn, ex, results)
                finally:
                    exam_conn.close()

    # Legacy JSON bundles live under the currently-resolved data_dir only
    # (this path predates multi-station support and is not per-station).
    own_conn = conn is None
    if cfg.station_code and cfg.exam_id:
        target_conn = conn if conn is not None else connect(cfg.db_path)
    else:
        target_conn = conn if conn is not None else connect(cfg.db_path)
    try:
        _import_legacy_json(target_conn, cfg.data_dir, results)
    finally:
        if own_conn:
            target_conn.close()

    return results


def _import_from_dir(conn: sqlite3.Connection, exam_root: Path, results: list[dict]) -> None:
    imported_dir = exam_root / "imports" / "imported"
    failed_dir   = exam_root / "imports" / "failed"
    for zip_path in _pending_zips(exam_root):
        try:
            res = package_import.import_signed_zip(conn, zip_path.read_bytes())
            results.append({"file": zip_path.name, "status": "imported", **res})
            _move(zip_path, imported_dir)
        except PackageImportError as exc:
            results.append({"file": zip_path.name, "status": "rejected",
                            "code": exc.code, "message": exc.message})
            _move(zip_path, failed_dir)
        except Exception as exc:  # noqa: BLE001
            results.append({"file": zip_path.name, "status": "error", "error": str(exc)})
            _move(zip_path, failed_dir)


def _import_legacy_json(conn: sqlite3.Connection, data_dir: Path, results: list[dict]) -> None:
    imported_dir = data_dir / "imports" / "imported"
    failed_dir   = data_dir / "imports" / "failed"
    for path in _legacy_json_bundles(data_dir):
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            results.append({"file": path.name, "status": "error", "error": f"invalid json: {exc}"})
            _move(path, failed_dir)
            continue
        manifest = (bundle or {}).get("manifest") or {}
        package_id = manifest.get("package_id")
        if _already_imported(conn, package_id):
            results.append({"file": path.name, "status": "skipped", "package_id": package_id})
            _move(path, imported_dir)
            continue
        try:
            res = package_import.import_bundle(conn, bundle)
            results.append({"file": path.name, "status": "imported", **res})
            _move(path, imported_dir)
        except PackageImportError as exc:
            results.append({"file": path.name, "status": "rejected",
                            "code": exc.code, "message": exc.message})
            _move(path, failed_dir)
