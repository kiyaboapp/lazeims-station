"""HTTP sync transport using the package-bound machine credential.

Central authenticates each sync request via ``X-Package-Credential-Id`` and
``X-Package-Secret``. The Station reads the secret from protected local
storage (see :mod:`station.machine_credential`) so the operator never pastes
any key into the UI.

Central URL is either configured by the admin or seeded from the package's
``central_base_url`` field at import time.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from typing import Callable

from . import machine_credential
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
    """Non-secret view of the sync configuration."""
    station_code = _meta_get(conn, "station_code")
    exam_id = _meta_get(conn, "exam_id")
    central_url = _meta_get(conn, "central_url")
    has_credential = False
    if station_code and exam_id:
        has_credential = machine_credential.load(station_code, exam_id) is not None
    return {
        "central_url": central_url,
        "has_credential": has_credential,
        "configured": bool(central_url) and has_credential,
    }


def set_sync_config(conn: sqlite3.Connection, *, central_url: str | None = None) -> dict:
    """Admin can override the Central URL; the machine credential is never
    pasted — it comes from the imported package."""
    with transaction(conn):
        if central_url is not None:
            _meta_set(conn, "central_url", central_url.strip().rstrip("/"))
    return get_sync_config(conn)


def seed_central_url_if_unset(conn: sqlite3.Connection, central_url: str) -> None:
    if central_url and not _meta_get(conn, "central_url"):
        with transaction(conn):
            _meta_set(conn, "central_url", central_url.strip().rstrip("/"))


def seed_central_url_from_package(conn: sqlite3.Connection, central_url: str) -> None:
    """Always write the Central URL that comes from an imported package.

    Unlike :func:`seed_central_url_if_unset` (used at boot to avoid clobbering
    an admin override), this is called during a package import where the
    package is the authoritative source for the URL. It will overwrite a
    stale/wrong URL so sync works immediately after importing a new bundle.
    """
    if central_url and central_url.strip():
        with transaction(conn):
            _meta_set(conn, "central_url", central_url.strip().rstrip("/"))


def http_transport(central_url: str, credential_id: str, secret: str) -> Callable[[dict], dict]:
    endpoint = central_url.rstrip("/") + SYNC_PATH

    def _transport(body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Package-Credential-Id": credential_id,
                "X-Package-Secret": secret,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(f"sync HTTP {exc.code}: {payload}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"sync network error: {exc.reason}") from exc

    return _transport


def run_http_sync(conn: sqlite3.Connection) -> dict:
    """Push pending events to Central using the package-bound credential."""
    import logging
    log = logging.getLogger("station.sync")

    central_url = _meta_get(conn, "central_url")
    station_code = _meta_get(conn, "station_code")
    exam_id = _meta_get(conn, "exam_id")
    log.warning("[run_http_sync] central_url=%r  station_code=%r  exam_id=%r",
                central_url, station_code, exam_id)

    if not central_url or not station_code or not exam_id:
        msg = f"missing: central_url={central_url!r} station_code={station_code!r} exam_id={exam_id!r}"
        log.warning("[run_http_sync] NOT CONFIGURED — %s", msg)
        return {"configured": False, "reason": f"Central URL not configured or station not adopted ({msg})"}

    cred = machine_credential.load(station_code, exam_id)
    log.warning("[run_http_sync] credential=%r", cred)
    if not cred:
        from . import paths
        cred_path = paths.machine_credential_path(station_code, exam_id)
        log.warning("[run_http_sync] NO CREDENTIAL — looked at path=%r  exists=%r", str(cred_path), cred_path.exists())
        return {"configured": False, "reason": f"No package machine credential found (looked at {cred_path})"}

    transport = http_transport(central_url, cred["credential_id"], cred["secret"])
    try:
        result = run_sync(conn, transport)
        log.warning("[run_http_sync] run_sync result=%r", result)
        return result
    except RuntimeError as exc:
        log.warning("[run_http_sync] RuntimeError: %s", exc)
        return {"configured": True, "error": str(exc)}
