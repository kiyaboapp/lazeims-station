"""HTTP sync transport + configuration for pushing the station outbox to Central.

The station's ``sync.run_sync`` is transport-agnostic: it takes a callable
``(request_dict) -> response_dict``. Here we provide the production transport —
a plain stdlib HTTPS POST to Central's machine-authenticated endpoint
``POST {central_url}/api/v1/station/sync/events`` with the station's one-time
``X-Station-Key``. No third-party HTTP dependency is needed.

Config (Central URL + sync key) lives in the local ``station_meta`` table:
  * ``central_url``  — where to sync (may be seeded from the bundle's sync.json);
  * ``sync_key``     — the station's secret machine key (entered once by admin).

The key is stored locally so the offline station can sync whenever it is back
online; it is never shipped inside a downloadable package.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from typing import Callable

from .db import transaction
from .sync import run_sync

SYNC_PATH = "/api/v1/station/sync/events"


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM station_meta WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO station_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_sync_config(conn: sqlite3.Connection) -> dict:
    """Non-secret view of the sync configuration (never returns the key)."""
    central_url = _meta_get(conn, "central_url")
    return {
        "central_url": central_url,
        "has_key": _meta_get(conn, "sync_key") is not None,
        "configured": bool(central_url) and _meta_get(conn, "sync_key") is not None,
    }


def set_sync_config(
    conn: sqlite3.Connection, *, central_url: str | None = None, sync_key: str | None = None
) -> dict:
    with transaction(conn):
        if central_url is not None:
            _meta_set(conn, "central_url", central_url.strip().rstrip("/"))
        if sync_key is not None and sync_key.strip():
            _meta_set(conn, "sync_key", sync_key.strip())
    return get_sync_config(conn)


def seed_central_url_if_unset(conn: sqlite3.Connection, central_url: str) -> None:
    """Set a default Central URL only when the admin has not chosen one."""
    if central_url and not _meta_get(conn, "central_url"):
        with transaction(conn):
            _meta_set(conn, "central_url", central_url.strip().rstrip("/"))


def http_transport(central_url: str, sync_key: str) -> Callable[[dict], dict]:
    endpoint = central_url.rstrip("/") + SYNC_PATH

    def _transport(body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json", "X-Station-Key": sync_key},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted, operator-set URL)
            return json.loads(resp.read().decode("utf-8"))

    return _transport


def run_http_sync(conn: sqlite3.Connection) -> dict:
    """Run one sync pass over HTTP. Returns the run summary, or a not-configured
    / error marker (never raises for the caller)."""
    central_url = _meta_get(conn, "central_url")
    sync_key = _meta_get(conn, "sync_key")
    if not central_url or not sync_key:
        return {"configured": False, "sent": 0, "message": "Sync is not configured yet."}
    try:
        summary = run_sync(conn, http_transport(central_url, sync_key))
    except urllib.error.HTTPError as exc:
        return {"configured": True, "error": f"HTTP {exc.code}", "resumable": True}
    except Exception as exc:  # offline / DNS / refused — resume next time
        return {"configured": True, "error": str(exc), "resumable": True}
    return {"configured": True, **summary}
