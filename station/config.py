"""Station configuration.

The Station uses canonical OS install paths (see :mod:`station.paths`) so that
data survives every downloaded exam-package folder and never lives inside a
Downloads directory. Multiple packages for the same (station_code, exam_id)
import into the SAME SQLite database — different exams get separate databases.

Environment overrides for advanced/testing use:
    LAZEIMS_HOME         override the LAZEIMS root (see paths.py)
    STATION_CODE         active station code (defaults to first discovered)
    STATION_EXAM_ID      active exam id (defaults to first discovered)
    STATION_DATA_DIR     force a specific data dir (legacy / tests)
    STATION_HOST         bind host (default 0.0.0.0)
    STATION_PORT         bind port  (default 8080)
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import paths


def _resolve_active_station(root: Path) -> tuple[str | None, str | None]:
    """Return (station_code, exam_id) for the active dataset.

    Priority: environment variables, then the newest station/exam directory
    that already contains a database. Returns (None, None) on a fresh install
    so the launcher/setup UI can guide the operator to import the first
    package.
    """
    code = os.environ.get("STATION_CODE", "").strip()
    exam = os.environ.get("STATION_EXAM_ID", "").strip()
    if code and exam:
        return code, exam

    stations_root = root / "stations"
    if not stations_root.is_dir():
        return code or None, exam or None

    candidates: list[tuple[float, str, str]] = []
    for station_path in stations_root.iterdir():
        if not station_path.is_dir():
            continue
        for exam_path in (station_path / "exams").glob("*") if (station_path / "exams").is_dir() else []:
            db_file = exam_path / "station.sqlite3"
            if db_file.is_file():
                candidates.append((db_file.stat().st_mtime, station_path.name, exam_path.name))
    if not candidates:
        return code or None, exam or None

    candidates.sort(reverse=True)
    _, latest_code, latest_exam = candidates[0]
    return code or latest_code, exam or latest_exam


@dataclass
class StationConfig:
    """Resolved runtime configuration."""

    lazeims_home: Path
    station_code: str | None
    exam_id: str | None
    data_dir: Path
    db_path: Path
    secret_key: str
    host: str = "0.0.0.0"
    port: int = 8080
    session_cookie: str = "lazeims_station_session"
    session_ttl_seconds: int = 43_200


def _legacy_data_dir() -> Path | None:
    """Support ``STATION_DATA_DIR`` override for existing tests/tools."""
    raw = os.environ.get("STATION_DATA_DIR", "").strip()
    if not raw:
        return None
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_or_create_secret(data_dir: Path) -> str:
    override = os.environ.get("STATION_SECRET_KEY", "").strip()
    if override:
        return override
    secret_file = data_dir / ".session-secret"
    legacy = data_dir / ".session_secret"
    if secret_file.exists():
        return secret_file.read_text().strip()
    if legacy.exists():
        text = legacy.read_text().strip()
        try:
            secret_file.write_text(text)
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return text
    key = secrets.token_urlsafe(32)
    secret_file.write_text(key)
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass
    return key


def load_config() -> StationConfig:
    root = paths.lazeims_home()

    # Legacy escape hatch: tests and older tools rely on STATION_DATA_DIR
    # pointing at a plain folder without station/exam segmentation.
    legacy = _legacy_data_dir()
    if legacy is not None:
        return StationConfig(
            lazeims_home=root,
            station_code=None,
            exam_id=None,
            data_dir=legacy,
            db_path=legacy / "station.sqlite3",
            secret_key=_load_or_create_secret(legacy),
            host=os.environ.get("STATION_HOST", "0.0.0.0"),
            port=int(os.environ.get("STATION_PORT", "8080")),
        )

    station_code, exam_id = _resolve_active_station(root)

    if station_code and exam_id:
        data_dir = paths.exam_dir(station_code, exam_id)
        db_path = paths.default_db_path(station_code, exam_id)
    else:
        # Fresh install: still need a stable place to hold the setup log/
        # session secret before the first package is imported.
        data_dir = root / "setup"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "station.sqlite3"

    return StationConfig(
        lazeims_home=root,
        station_code=station_code,
        exam_id=exam_id,
        data_dir=data_dir,
        db_path=db_path,
        secret_key=_load_or_create_secret(data_dir),
        host=os.environ.get("STATION_HOST", "0.0.0.0"),
        port=int(os.environ.get("STATION_PORT", "8080")),
    )
