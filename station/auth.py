"""Local station authentication.

* Data Enterer logs in with PIN + initials.
* Station Exam Admin logs in with username(assignment)+ password.
Only salted Argon2 hashes are stored (shipped in the package). Sessions are
signed tokens (itsdangerous) bound server-side; role/scope derive from the
station_users row, never from the client.
"""

from __future__ import annotations

import sqlite3

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from itsdangerous import BadSignature, URLSafeTimedSerializer

_ph = PasswordHasher()


def _verify(hashed: str | None, plaintext: str) -> bool:
    if not hashed:
        return False
    try:
        return _ph.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def authenticate_de(conn: sqlite3.Connection, pin: str, initials: str) -> dict | None:
    """Return the station_user row (as dict) for a matching DE PIN+initials."""
    rows = conn.execute(
        "SELECT * FROM station_users WHERE role = 'DATA_ENTERER' AND active = 1 AND initials = ?",
        (initials,),
    ).fetchall()
    for row in rows:
        if _verify(row["pin_hash"], pin):
            return dict(row)
    return None


def authenticate_admin(conn: sqlite3.Connection, password: str) -> dict | None:
    """Return the station_user row for a matching station EXAM_ADMIN password."""
    rows = conn.execute(
        "SELECT * FROM station_users WHERE role = 'EXAM_ADMIN' AND active = 1",
    ).fetchall()
    for row in rows:
        if _verify(row["password_hash"], password):
            return dict(row)
    return None


class SessionManager:
    def __init__(self, secret_key: str, max_age: int):
        self._s = URLSafeTimedSerializer(secret_key, salt="lazeims-station-session")
        self._max_age = max_age

    def issue(self, user_id: int, role: str) -> str:
        return self._s.dumps({"uid": user_id, "role": role})

    def resolve(self, token: str) -> dict | None:
        if not token:
            return None
        try:
            return self._s.loads(token, max_age=self._max_age)
        except BadSignature:
            return None
