"""Station configuration.

Deliberately tiny and dependency-light. Paths default next to the package data
directory so the station is self-contained on a USB stick / offline PC.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DATA_DIR = Path(os.environ.get("STATION_DATA_DIR", Path.cwd() / "station_data"))


@dataclass
class StationConfig:
    data_dir: Path = _DATA_DIR
    db_path: Path = _DATA_DIR / "station.sqlite3"
    host: str = os.environ.get("STATION_HOST", "0.0.0.0")
    port: int = int(os.environ.get("STATION_PORT", "8080"))
    # Local session cookie signing secret. Generated per-station on first run and
    # persisted in the data dir (never shipped in the package).
    secret_key: str = os.environ.get("STATION_SECRET_KEY", "")
    session_cookie: str = "lazeims_station_session"
    session_ttl_seconds: int = 43_200

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> StationConfig:
    cfg = StationConfig()
    cfg.ensure_dirs()
    if not cfg.secret_key:
        secret_file = cfg.data_dir / ".session_secret"
        if secret_file.exists():
            cfg.secret_key = secret_file.read_text().strip()
        else:
            import secrets
            cfg.secret_key = secrets.token_urlsafe(32)
            secret_file.write_text(cfg.secret_key)
            secret_file.chmod(0o600)
    return cfg
