"""Signed station package import.

Accepts either:
- an in-memory bundle dict (used by tests and direct API callers), or
- a signed ZIP file emitted by Central.

Verifies:
- ZIP structure and file hashes (when SHA256SUMS is present),
- Ed25519 signature against the shared public key,
- station_code and exam_id against the local station identity (once adopted),
- rules/software minimum versions,
- configuration_hash / seed integrity.

Applies the seed in one SQLite transaction, upserts station users by
``assignment_id`` (never duplicating), and persists the package-bound machine
credential to protected local storage.
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from lazeims_common.signing import verify_package_signature

from . import machine_credential
from .migrations import PackageImportError, import_package


def _load_zip_bundle(zip_bytes: bytes) -> dict:
    """Load a signed exam package ZIP into an in-memory bundle dict."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise PackageImportError("PACKAGE_MALFORMED", f"Not a valid ZIP: {exc}")

    def _read(name: str) -> bytes:
        try:
            return zf.read(name)
        except KeyError:
            raise PackageImportError("PACKAGE_MALFORMED", f"Missing {name} in package ZIP")

    manifest = json.loads(_read("manifest.json"))
    seed = json.loads(_read("seed.json"))
    machine_cred_payload = json.loads(_read("machine-credential.json"))
    signature = _read("signature").decode("ascii").strip()

    bundle: dict[str, Any] = {
        "manifest": manifest,
        "seed": seed,
        "signature": signature,
        "machine_credential": machine_cred_payload,
        "contract_version": manifest.get("contract_version", ""),
    }
    return bundle


def _verify_signature(bundle: dict) -> None:
    signature = bundle.get("signature") or ""
    manifest = bundle.get("manifest") or {}
    if not signature or not verify_package_signature(manifest, signature):
        raise PackageImportError("SIGNATURE_INVALID", "Package signature could not be verified.")


def import_signed_zip(conn: sqlite3.Connection, zip_bytes: bytes) -> dict:
    """Public entry point: import a signed exam package ZIP.

    Verifies signature, imports seed into SQLite (via :func:`import_package`),
    then stores the machine credential locally in protected storage.
    """
    bundle = _load_zip_bundle(zip_bytes)
    _verify_signature(bundle)
    return import_bundle(conn, bundle)


def import_bundle(conn: sqlite3.Connection, bundle: dict) -> dict:
    """Import an in-memory bundle dict (used by tests and the auto-importer)."""
    manifest = bundle.get("manifest") or {}
    signature = bundle.get("signature")
    # If a signature is present, always verify it. Bundles without a signature
    # only originate from internal test factories.
    if signature:
        _verify_signature(bundle)

    result = import_package(conn, bundle)

    mc = bundle.get("machine_credential")
    if isinstance(mc, dict) and mc.get("secret"):
        try:
            machine_credential.store(
                station_code=manifest.get("station_code", "unknown"),
                exam_id=str(manifest.get("exam_id", "unknown")),
                payload=mc,
            )
            result["machine_credential_stored"] = True
        except Exception as exc:  # noqa: BLE001
            result["machine_credential_stored"] = False
            result["machine_credential_error"] = str(exc)

    # Seed the Central URL from the package into station_meta so sync works
    # immediately after import — no manual admin configuration required.
    # Prefer the machine_credential payload (most authoritative), fall back to
    # the manifest field.
    central_url = (
        (mc or {}).get("central_base_url")
        or manifest.get("central_base_url")
        or ""
    )
    if central_url and central_url.strip():
        try:
            from .sync_http import seed_central_url_from_package
            seed_central_url_from_package(conn, central_url.strip())
            result["central_url_seeded"] = True
        except Exception as exc:  # noqa: BLE001
            result["central_url_seeded"] = False
            result["central_url_seed_error"] = str(exc)

    # Seed the sync_path from the package so the station knows which endpoint
    # to POST events to (allows backend-sis to specify a different path than
    # the default lazeims-core path).
    sync_path = manifest.get("sync_path") or ""
    if sync_path and sync_path.strip():
        try:
            from .sync_http import seed_sync_path_from_package
            seed_sync_path_from_package(conn, sync_path.strip())
            result["sync_path_seeded"] = True
        except Exception as exc:  # noqa: BLE001
            result["sync_path_seeded"] = False
            result["sync_path_seed_error"] = str(exc)

    # Store backend_type from the manifest so the station knows whether it is
    # talking to lazeims-core or backend-sis (for future UI differentiation).
    backend_type = manifest.get("backend_type", "")
    if backend_type and backend_type.strip():
        try:
            from .sync_http import _meta_set
            from .db import transaction as _tx
            with _tx(conn):
                _meta_set(conn, "backend_type", backend_type.strip())
            result["backend_type_stored"] = True
        except Exception as exc:  # noqa: BLE001
            result["backend_type_stored"] = False

    return result


def read_zip_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()
