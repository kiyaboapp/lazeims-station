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

    On a fresh install (station_code/exam_id not yet known), scans every
    ``stations/<code>/exams/<id>/imports/pending/`` directory under the
    LAZEIMS home so that a pre-staged Complete Bundle package is discovered
    and imported automatically on first boot without any manual UI steps.
    """
    results: list[dict] = []

    if cfg.station_code and cfg.exam_id:
        # Known active station/exam — import into the configured DB
        exam_root = paths.exam_dir(cfg.station_code, cfg.exam_id)
        own_conn = conn is None
        if own_conn:
            conn = connect(cfg.db_path)
        try:
            _import_from_dir(conn, exam_root, results)
            _import_legacy_json(conn, cfg.data_dir, results)
        finally:
            if own_conn:
                conn.close()
        return results

    # Fresh install — scan every station/exam dir that has pending ZIPs.
    # Use the per-exam DB for each, not the global setup DB.
    stations_root = cfg.lazeims_home / "stations"
    if stations_root.is_dir():
        for stn in stations_root.iterdir():
            if not stn.is_dir():
                continue
            exams_dir = stn / "exams"
            if not exams_dir.is_dir():
                continue
            for ex in exams_dir.iterdir():
                if not ex.is_dir():
                    continue
                pending = ex / "imports" / "pending"
                if not pending.is_dir() or not any(pending.glob("*.zip")):
                    continue
                # Use the exam-specific DB so the import is in the right place
                exam_db_path = ex / "station.sqlite3"
                exam_conn = connect(exam_db_path)
                try:
                    from .migrations import apply_migrations
                    apply_migrations(exam_conn)
                    _import_from_dir(exam_conn, ex, results)
                finally:
                    exam_conn.close()

    # Also scan legacy JSON bundles from the setup dir
    own_conn = conn is None
    if own_conn:
        conn = connect(cfg.db_path)
    try:
        _import_legacy_json(conn, cfg.data_dir, results)
    finally:
        if own_conn:
            conn.close()

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
