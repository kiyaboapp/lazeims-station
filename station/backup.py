"""SQLite backup via the online backup API (never copy a live DB file directly).

Produces timestamped rolling snapshots and can restore/verify one. A pre-upgrade
backup is taken before any schema migration so marks/outbox are never at risk.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup_database(db_path: Path | str, backup_dir: Path | str, *, keep: int = 10) -> Path:
    """Create a consistent snapshot of ``db_path`` using SQLite's backup API.

    Returns the snapshot path. Prunes to the newest ``keep`` snapshots.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    snapshot = backup_dir / f"{db_path.stem}-{ts}.sqlite3"

    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(snapshot))
        try:
            src.backup(dst)  # online backup API — consistent even while in use
        finally:
            dst.close()
    finally:
        src.close()

    # prune old snapshots
    snaps = sorted(backup_dir.glob(f"{db_path.stem}-*.sqlite3"))
    for old in snaps[:-keep]:
        old.unlink(missing_ok=True)
    return snapshot


def list_snapshots(db_path: Path | str, backup_dir: Path | str) -> list[Path]:
    return sorted(Path(backup_dir).glob(f"{Path(db_path).stem}-*.sqlite3"))


def restore_database(snapshot: Path | str, db_path: Path | str) -> None:
    """Restore a snapshot over the live DB path (backup API into the target)."""
    src = sqlite3.connect(str(snapshot))
    try:
        dst = sqlite3.connect(str(db_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def verify_snapshot(snapshot: Path | str) -> bool:
    """Integrity-check a snapshot (PRAGMA integrity_check)."""
    conn = sqlite3.connect(str(snapshot))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    finally:
        conn.close()
