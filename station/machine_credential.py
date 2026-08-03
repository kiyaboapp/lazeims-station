"""Package-bound machine credential storage (protected local file).

Each exam package carries a random secret (with its ``credential_id``) that the
Station uses to authenticate its sync to Central. Only the Argon2id hash is
stored centrally.

Locally we store the plaintext in a per-station/exam file with 0600 permissions
and a lightweight XOR-with-machine-key obfuscation using a machine-persistent
random key (kept in the same directory). This is not proper cryptography — it
is anti-casual-inspection while the OS filesystem permission is the actual
access control. Where available, we should later back this with OS keychains
(Windows DPAPI, Linux keyring); this module isolates that decision.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path

from . import paths


def _obfuscation_key(station_code: str, exam_id: str) -> bytes:
    """Return a machine-persistent obfuscation key for this station+exam."""
    key_file = paths.exam_dir(station_code, exam_id) / ".obfuscation-key"
    if key_file.exists():
        return key_file.read_bytes()
    k = secrets.token_bytes(32)
    key_file.write_bytes(k)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return k


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def store(station_code: str, exam_id: str, payload: dict) -> None:
    """Persist the machine credential payload for this station+exam.

    ``payload`` contains at least: credential_id, package_id, secret, central_base_url.
    """
    path = paths.machine_credential_path(station_code, exam_id)
    key = _obfuscation_key(station_code, exam_id)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    obf = base64.b64encode(_xor(body, key))
    path.write_bytes(obf)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load(station_code: str, exam_id: str) -> dict | None:
    path = paths.machine_credential_path(station_code, exam_id)
    if not path.exists():
        return None
    try:
        key = _obfuscation_key(station_code, exam_id)
        body = _xor(base64.b64decode(path.read_bytes()), key)
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def clear(station_code: str, exam_id: str) -> None:
    path = paths.machine_credential_path(station_code, exam_id)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
