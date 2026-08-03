"""Local station authentication.

* Data Enterer: initials + PIN.
* Station Exam Admin: full username + password.

Only salted Argon2 hashes are stored (shipped in the package). Sessions are
signed tokens (itsdangerous); role/scope always derive from the local database
row on every request, never from client input.
"""

from __future__ import annotations

import sqlite3

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
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
    rows = conn.execute(
        "SELECT * FROM station_users WHERE role = 'DATA_ENTERER' AND active = 1 AND initials = ?",
        (initials,),
    ).fetchall()
    for row in rows:
        if _verify(row["pin_hash"], pin):
            return dict(row)
    return None


def authenticate_admin(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    """Match against the dedicated ``admin_username`` column."""
    rows = conn.execute(
        "SELECT * FROM station_users WHERE role = 'EXAM_ADMIN' AND active = 1"
        " AND (admin_username = ? OR initials = ?)",
        (username, username),
    ).fetchall()
    for row in rows:
        if _verify(row["password_hash"], password):
            return dict(row)
    return None


def de_scopes_for(conn: sqlite3.Connection, assignment_id: int) -> dict:
    """Return the DE's allowed centres and subjects.

    A missing row means the DE has no restriction on that dimension.
    """
    rows = conn.execute(
        "SELECT centre_number, subject_code FROM user_scopes WHERE assignment_id = ?",
        (assignment_id,),
    ).fetchall()
    centres: set[str] = set()
    subjects: set[str] = set()
    for r in rows:
        if r["centre_number"]:
            centres.add(r["centre_number"])
        if r["subject_code"]:
            subjects.add(r["subject_code"])
    return {"centres": centres, "subjects": subjects}


def is_scope_allowed(scopes: dict, *, centre_number: str, subject_code: str) -> bool:
    ok_centre = not scopes["centres"] or centre_number in scopes["centres"]
    ok_subject = not scopes["subjects"] or subject_code in scopes["subjects"]
    return ok_centre and ok_subject


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
