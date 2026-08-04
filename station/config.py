"""Station configuration — live-resolved per request.

The Station uses canonical OS install paths (see :mod:`station.paths`) so that
data survives every downloaded exam-package folder and never lives inside a
Downloads directory. Multiple packages for the same (station_code, exam_id)
import into the SAME SQLite database — different exams get separate databases.

A single computer can host many station identities (e.g. MWANZA-2, GEITA-1,
GEITA-2). At any moment **exactly one** of them is *active*. The active
selection is recorded in ``stations/.active`` and can be switched at runtime
without restarting the server.

To make switching seamless, callers should never cache a :class:`StationConfig`
across requests. Use :func:`resolve_active_cfg` on every request; it uses the
mtime of ``stations/.active`` to avoid rebuilding the config when nothing has
changed, but rebuilds instantly the moment it does.

Environment overrides (advanced/testing):
    LAZEIMS_HOME         override the LAZEIMS root (see paths.py)
    STATION_CODE         pin active station code (bypass .active)
    STATION_EXAM_ID      pin active exam id (bypass .active)
    STATION_DATA_DIR     force a specific data dir (legacy / tests)
    STATION_HOST         bind host (default 0.0.0.0)
    STATION_PORT         bind port  (default 8080)
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import paths


_ACTIVE_FILENAME = ".active"


def _resolve_active_station(root: Path) -> tuple[str | None, str | None]:
    """Return (station_code, exam_id) for the active dataset.

    Priority:
      1. Environment variables (STATION_CODE + STATION_EXAM_ID)
      2. Explicit choice file: stations/.active
      3. Auto-select if exactly one station/exam DB exists
      4. Otherwise (None, None) → chooser mode
    """
    code = os.environ.get("STATION_CODE", "").strip()
    exam = os.environ.get("STATION_EXAM_ID", "").strip()
    if code and exam:
        return code, exam

    stations_root = root / "stations"

    active_file = stations_root / _ACTIVE_FILENAME
    if active_file.is_file():
        try:
            active = json.loads(active_file.read_text(encoding="utf-8"))
            a_code = (active.get("station_code") or "").strip()
            a_exam = (active.get("exam_id") or "").strip()
            if a_code and a_exam:
                db_file = stations_root / a_code / "exams" / a_exam / "station.sqlite3"
                if db_file.is_file():
                    return a_code, a_exam
        except (OSError, ValueError):
            pass

    if not stations_root.is_dir():
        return code or None, exam or None

    candidates: list[tuple[float, str, str]] = []
    for station_path in stations_root.iterdir():
        if not station_path.is_dir() or station_path.name.startswith("."):
            continue
        exams_dir = station_path / "exams"
        if not exams_dir.is_dir():
            continue
        for exam_path in exams_dir.iterdir():
            db_file = exam_path / "station.sqlite3"
            if db_file.is_file():
                candidates.append((db_file.stat().st_mtime, station_path.name, exam_path.name))
    if not candidates:
        return code or None, exam or None

    if len(candidates) == 1:
        _, only_code, only_exam = candidates[0]
        return code or only_code, exam or only_exam

    # Multiple stations exist — require an explicit choice.
    return None, None


@dataclass
class StationConfig:
    """Resolved runtime configuration.

    Fields default to sensible values so tests that only need a data_dir + db_path
    can construct one directly. In production, :func:`load_config` /
    :func:`resolve_active_cfg` always populate every field.
    """

    data_dir: Path
    db_path: Path
    secret_key: str
    lazeims_home: Path | None = None
    station_code: str | None = None
    exam_id: str | None = None
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
    """Load (or create) the HMAC secret used to sign session cookies for one
    station+exam. Each station has its own secret file — switching stations
    means old cookies fail signature verification and cannot cross over.
    """
    override = os.environ.get("STATION_SECRET_KEY", "").strip()
    if override:
        return override
    data_dir.mkdir(parents=True, exist_ok=True)
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


def load_config(*, home: Path | None = None) -> StationConfig:
    """Fresh, unconditional config resolution. Prefer :func:`resolve_active_cfg`."""
    root = home or paths.lazeims_home()

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


# ---------------------------------------------------------------------------
# Live resolution with mtime caching
# ---------------------------------------------------------------------------

class _ActiveConfigCache:
    """Rebuilds :class:`StationConfig` whenever the ``.active`` file changes.

    The lookup is O(1) after a stable window: we stat the file and compare
    mtime+size. If anything shifted, we invalidate and reload. This means a
    switch takes effect on the next request with zero downtime — no process
    restart, no stale ``app.state.cfg``.
    """

    def __init__(self, home: Path):
        self._home = home
        self._lock = threading.Lock()
        self._cfg: StationConfig | None = None
        self._stamp: tuple[float, int, str] | None = None  # (mtime, size, "code|exam")

    def _fingerprint(self) -> tuple[float, int, str]:
        active = self._home / "stations" / _ACTIVE_FILENAME
        env_code = os.environ.get("STATION_CODE", "").strip()
        env_exam = os.environ.get("STATION_EXAM_ID", "").strip()
        env_key = f"{env_code}|{env_exam}"
        try:
            st = active.stat()
            return (st.st_mtime, st.st_size, env_key)
        except FileNotFoundError:
            return (0.0, 0, env_key)

    def get(self, *, force_reload: bool = False) -> StationConfig:
        with self._lock:
            fp = self._fingerprint()
            if force_reload or self._cfg is None or fp != self._stamp:
                self._cfg = load_config(home=self._home)
                self._stamp = fp
            return self._cfg

    def invalidate(self) -> None:
        with self._lock:
            self._cfg = None
            self._stamp = None


_CACHE: dict[str, _ActiveConfigCache] = {}
_CACHE_LOCK = threading.Lock()


def resolve_active_cfg(home: Path | None = None, *, force_reload: bool = False) -> StationConfig:
    """Return the currently-active :class:`StationConfig`.

    Callers should treat the returned object as read-only. It is safe to call
    on every request — internally uses an mtime cache keyed by ``home``.
    """
    root = home or paths.lazeims_home()
    key = str(root.resolve())
    with _CACHE_LOCK:
        cache = _CACHE.get(key)
        if cache is None:
            cache = _ActiveConfigCache(root)
            _CACHE[key] = cache
    return cache.get(force_reload=force_reload)


def invalidate_active_cfg(home: Path | None = None) -> None:
    """Force the next :func:`resolve_active_cfg` to rebuild from disk."""
    root = home or paths.lazeims_home()
    key = str(root.resolve())
    with _CACHE_LOCK:
        cache = _CACHE.get(key)
    if cache is not None:
        cache.invalidate()


# ---------------------------------------------------------------------------
# Station directory helpers (pure — no side effects on the live cfg)
# ---------------------------------------------------------------------------

def list_available_stations(home: Path | None = None) -> list[dict]:
    """Scan disk for every (station_code, exam_id) with a station.sqlite3.

    Read-only: safe for the switcher endpoint. Returns entries even when
    ``.active`` points nowhere. Each entry has ``station_code``, ``exam_id``,
    ``exam_name`` (best-effort read from ``station_meta``), and ``students``.
    """
    root = home or paths.lazeims_home()
    stations_root = root / "stations"
    if not stations_root.is_dir():
        return []

    from .db import connect  # local import avoids cycle

    found: list[dict] = []
    for station_path in sorted(stations_root.iterdir()):
        if not station_path.is_dir() or station_path.name.startswith("."):
            continue
        exams_dir = station_path / "exams"
        if not exams_dir.is_dir():
            continue
        for exam_path in sorted(exams_dir.iterdir()):
            db_file = exam_path / "station.sqlite3"
            if not db_file.is_file():
                continue
            exam_name = None
            student_count = 0
            try:
                c = connect(db_file)
                row = c.execute(
                    "SELECT value FROM station_meta WHERE key='exam_name'"
                ).fetchone()
                if row is not None:
                    exam_name = row["value"]
                cnt = c.execute("SELECT COUNT(*) c FROM students").fetchone()
                if cnt is not None:
                    student_count = int(cnt["c"])
                c.close()
            except Exception:
                pass
            found.append({
                "station_code": station_path.name,
                "exam_id": exam_path.name,
                "exam_name": exam_name,
                "students": student_count,
            })
    return found


def read_active_station(home: Path | None = None) -> dict | None:
    """Read the currently-recorded active station without side effects.

    Returns ``None`` if no active file exists or it's malformed.
    """
    root = home or paths.lazeims_home()
    active_file = root / "stations" / _ACTIVE_FILENAME
    if not active_file.is_file():
        return None
    try:
        data = json.loads(active_file.read_text(encoding="utf-8"))
        code = (data.get("station_code") or "").strip()
        exam = (data.get("exam_id") or "").strip()
        if code and exam:
            return {"station_code": code, "exam_id": exam}
    except (OSError, ValueError):
        return None
    return None


def set_active_station(station_code: str, exam_id: str, *, home: Path | None = None) -> None:
    """Persist the choice of active station (atomic write) and invalidate cache.

    Verifies the target station.sqlite3 exists on disk. Raises :class:`ValueError`
    otherwise. Uses an atomic rename so a torn write cannot leave a partial file.
    """
    root = home or paths.lazeims_home()
    stations_root = root / "stations"
    target_db = stations_root / station_code / "exams" / exam_id / "station.sqlite3"
    if not target_db.is_file():
        raise ValueError(
            f"Station '{station_code}' / exam '{exam_id}' is not present on this device."
        )
    stations_root.mkdir(parents=True, exist_ok=True)
    active_file = stations_root / _ACTIVE_FILENAME
    tmp = active_file.with_suffix(".active.tmp")
    payload = json.dumps({"station_code": station_code, "exam_id": exam_id})
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, active_file)
    invalidate_active_cfg(root)


def clear_active_station(*, home: Path | None = None) -> None:
    """Remove the active-station selection (returns to chooser mode)."""
    root = home or paths.lazeims_home()
    active_file = root / "stations" / _ACTIVE_FILENAME
    try:
        active_file.unlink()
    except FileNotFoundError:
        pass
    invalidate_active_cfg(root)
