"""Canonical install/data paths for LAZEIMS Station.

The Station is installed once per computer. Data is persistent across every
exam package for the same (station_code, exam_id) — never inside a downloaded
folder. Multiple exams on the same computer get separate SQLite databases.

Layout::

    %LOCALAPPDATA%\\LAZEIMS\\               (Windows)
    ~/.local/share/lazeims/                 (Linux/macOS)
    ├── launcher/
    ├── env/                                one managed virtualenv, reused
    ├── stations/<station-code>/exams/<exam-id>/
    │   ├── station.sqlite3
    │   ├── .session-secret
    │   ├── machine-credential.enc          per-package machine secret (protected)
    │   ├── imports/
    │   │   ├── pending/                    dropped ZIPs land here
    │   │   ├── imported/                   successful
    │   │   └── failed/                     quarantined
    │   ├── backups/
    │   ├── exports/
    │   └── logs/
    └── logs/

Any path can be overridden by ``LAZEIMS_HOME`` for testing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _platform_home() -> Path:
    """Return the platform-specific LAZEIMS root directory."""
    override = os.environ.get("LAZEIMS_HOME", "").strip()
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            return Path(base) / "LAZEIMS"
        return Path.home() / "AppData" / "Local" / "LAZEIMS"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / "lazeims"
    return Path.home() / ".local" / "share" / "lazeims"


def lazeims_home() -> Path:
    p = _platform_home()
    p.mkdir(parents=True, exist_ok=True)
    return p


def launcher_dir() -> Path:
    p = lazeims_home() / "launcher"
    p.mkdir(parents=True, exist_ok=True)
    return p


def env_dir() -> Path:
    """One managed virtualenv per computer, reused across every exam package."""
    p = lazeims_home() / "env"
    p.mkdir(parents=True, exist_ok=True)
    return p


def global_logs_dir() -> Path:
    p = lazeims_home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def station_dir(station_code: str) -> Path:
    p = lazeims_home() / "stations" / _safe(station_code)
    p.mkdir(parents=True, exist_ok=True)
    return p


def exam_dir(station_code: str, exam_id: str) -> Path:
    p = station_dir(station_code) / "exams" / _safe(exam_id)
    for sub in ("imports/pending", "imports/imported", "imports/failed",
                "backups", "exports", "logs", "packages"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def default_db_path(station_code: str, exam_id: str) -> Path:
    return exam_dir(station_code, exam_id) / "station.sqlite3"


def machine_credential_path(station_code: str, exam_id: str) -> Path:
    return exam_dir(station_code, exam_id) / "machine-credential.enc"


def _safe(name: str) -> str:
    """Filesystem-safe identifier component."""
    return "".join(c for c in name if c.isalnum() or c in "-_.") or "unknown"
