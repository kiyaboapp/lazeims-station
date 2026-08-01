"""Auto-import bundled packages on station boot.

A "Complete Station Bundle" ships the signed package(s) inside
``station_data/import/``. On startup the station scans that folder and imports
any package it has not already adopted, then moves the file aside so it is not
re-processed. This is what makes a freshly-downloaded bundle run *ready* — the
operator never has to POST the package by hand.

Idempotent by design:
  * a package whose ``package_id`` is already in the local ``packages`` table is
    skipped (never re-imported — credentials would otherwise duplicate);
  * a successfully imported (or skipped) file is moved to ``import/imported/``;
  * a rejected/invalid file is moved to ``import/failed/`` with the reason, so a
    wrong-target/version bundle never blocks boot.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from .config import StationConfig
from .db import connect
from .migrations import PackageImportError, import_package


def _move(path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / path.name
    if target.exists():
        target.unlink()
    shutil.move(str(path), str(target))


def _already_imported(conn: sqlite3.Connection, package_id: str | None) -> bool:
    if not package_id:
        return False
    return conn.execute(
        "SELECT 1 FROM packages WHERE package_id = ?", (package_id,)
    ).fetchone() is not None


def auto_import_pending(cfg: StationConfig, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Import every not-yet-adopted package in ``station_data/import/``.

    Returns a list of per-file result dicts (for logging). Never raises — a bad
    bundle is quarantined in ``import/failed/`` instead of stopping the server.
    """
    import_dir = cfg.data_dir / "import"
    if not import_dir.is_dir():
        return []

    own_conn = conn is None
    if own_conn:
        conn = connect(cfg.db_path)

    imported_dir = import_dir / "imported"
    failed_dir = import_dir / "failed"
    results: list[dict] = []

    try:
        for path in sorted(import_dir.glob("*.json")):
            try:
                bundle = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # malformed file — quarantine, keep booting
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
                res = import_package(conn, bundle)
                results.append({"file": path.name, "status": "imported", **res})
                _move(path, imported_dir)
            except PackageImportError as exc:
                results.append({"file": path.name, "status": "rejected", "code": exc.code, "message": exc.message})
                _move(path, failed_dir)
    finally:
        if own_conn:
            conn.close()

    return results
