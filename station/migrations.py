"""Versioned SQLite schema + scope-only package import.

* Schema is versioned via ``PRAGMA user_version``; upgrades are explicit and
  never discard marks/outbox data.
* Import verifies the package targets THIS station/exam, that the rules and
  software versions are supported, and that the seed matches the manifest's
  ``configuration_hash`` (integrity check — needs no shared secret). It then
  seeds the local tables inside ONE transaction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from lazeims_common.hashing import sha256_prefixed

from . import SCHEMA_VERSION, SOFTWARE_VERSION, SUPPORTED_RULES_VERSIONS
from .db import get_user_version, set_user_version, transaction


class PackageImportError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# --- schema v1 DDL ---
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS station_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY, exam_id TEXT NOT NULL, station_code TEXT NOT NULL,
    package_version INTEGER NOT NULL, rules_version TEXT NOT NULL,
    configuration_hash TEXT NOT NULL, manifest_json TEXT NOT NULL, imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_users (
    id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL, role TEXT NOT NULL,
    pin_hash TEXT, initials TEXT, password_hash TEXT, active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS schools (centre_number TEXT PRIMARY KEY, name TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS subjects (
    subject_code TEXT PRIMARY KEY, name TEXT NOT NULL,
    total_theory1 INTEGER NOT NULL DEFAULT 0, total_theory2 INTEGER NOT NULL DEFAULT 0,
    total_practical INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS question_groups (
    id INTEGER PRIMARY KEY, subject_code TEXT NOT NULL, paper_type TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT, instruction TEXT, pick_count INTEGER NOT NULL,
    UNIQUE(subject_code, paper_type, code)
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY, subject_code TEXT NOT NULL, paper_type TEXT NOT NULL,
    question_number TEXT NOT NULL, group_code TEXT, max_marks REAL NOT NULL,
    UNIQUE(subject_code, paper_type, question_number)
);

CREATE TABLE IF NOT EXISTS question_topics (
    id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, weight REAL NOT NULL,
    FOREIGN KEY(question_id) REFERENCES questions(id)
);

CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY, centre_number TEXT NOT NULL,
    first_name TEXT NOT NULL, middle_name TEXT, surname TEXT NOT NULL, sex TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS student_subjects (
    student_id TEXT NOT NULL, subject_code TEXT NOT NULL,
    PRIMARY KEY(student_id, subject_code)
);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY, student_id TEXT NOT NULL, subject_code TEXT NOT NULL,
    paper_type TEXT NOT NULL, is_present INTEGER NOT NULL, source TEXT NOT NULL,
    transcribed_by INTEGER, transcribed_at TEXT,
    UNIQUE(student_id, subject_code, paper_type)
);

CREATE TABLE IF NOT EXISTS exam_incidents (
    id INTEGER PRIMARY KEY, student_id TEXT, subject_code TEXT NOT NULL, paper_type TEXT NOT NULL,
    incident_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', explanation TEXT,
    raised_by INTEGER, raised_at TEXT
);

CREATE TABLE IF NOT EXISTS total_marks (
    id INTEGER PRIMARY KEY, student_id TEXT NOT NULL, subject_code TEXT NOT NULL,
    paper_type TEXT NOT NULL, total_marks_obtained REAL NOT NULL,
    entered_by INTEGER, entered_at TEXT,
    UNIQUE(student_id, subject_code, paper_type)
);

CREATE TABLE IF NOT EXISTS item_marks (
    id INTEGER PRIMARY KEY, student_id TEXT NOT NULL, question_id INTEGER NOT NULL,
    marks_obtained REAL NOT NULL, entered_by INTEGER, entered_at TEXT,
    UNIQUE(student_id, question_id)
);

CREATE TABLE IF NOT EXISTS work_locks (
    scope_key TEXT PRIMARY KEY, owner INTEGER, status TEXT NOT NULL DEFAULT 'ACTIVE', acquired_at TEXT
);

CREATE TABLE IF NOT EXISTS finalized_scopes (
    scope_key TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1,
    finalized_by INTEGER, finalized_at TEXT, snapshot_json TEXT
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, operation TEXT NOT NULL,
    natural_key_json TEXT NOT NULL, value_json TEXT, local_version INTEGER NOT NULL,
    actor_assignment_id INTEGER, occurred_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, ack_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY, actor INTEGER, action TEXT NOT NULL, entity_type TEXT,
    entity_id TEXT, before_json TEXT, after_json TEXT, occurred_at TEXT NOT NULL
);
"""


# v1 -> v2 additive migration (columns only; never drops marks/outbox).
_MIGRATE_V2 = """
ALTER TABLE outbox_events ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE station_meta ADD COLUMN updated_at TEXT;
"""


def apply_migrations(conn: sqlite3.Connection, *, backup_before_upgrade: str | None = None) -> int:
    """Bring the DB up to SCHEMA_VERSION. Returns the resulting version.

    Upgrades are explicit and ADDITIVE — they never drop marks/outbox data. If
    ``backup_before_upgrade`` (a directory) is given and an upgrade of an
    existing populated DB is required, a snapshot is taken first.
    """
    version = get_user_version(conn)

    # Fresh database: create the full current schema in one shot.
    if version < 1:
        conn.executescript(_SCHEMA_V1)
        set_user_version(conn, 1)
        version = 1

    # Existing DB needing an upgrade: back up first (never risk marks/outbox).
    if version < SCHEMA_VERSION and backup_before_upgrade:
        from .backup import backup_database
        # Only back up if there is real data (avoid noise on fresh installs).
        has_data = conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] > 0
        if has_data:
            db_file = conn.execute("PRAGMA database_list").fetchone()[2]
            if db_file:
                backup_database(db_file, backup_before_upgrade)

    # v1 -> v2: additive columns only (data preserved).
    if version < 2:
        conn.executescript(_MIGRATE_V2)
        set_user_version(conn, 2)
        version = 2

    if version != SCHEMA_VERSION:
        raise PackageImportError(
            "UPGRADE_REQUIRED",
            f"Local schema version {version} is newer than this build ({SCHEMA_VERSION}).",
        )
    return version


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _meta_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM station_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO station_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def import_package(conn: sqlite3.Connection, bundle: dict) -> dict:
    """Validate and import a package bundle ({manifest, seed}).

    Raises :class:`PackageImportError` with a stable code on any mismatch.
    """
    manifest = dict(bundle.get("manifest") or {})
    seed = bundle.get("seed")
    if not manifest or seed is None:
        raise PackageImportError("CONFIGURATION_MISMATCH", "Bundle missing manifest or seed.")

    rules_version = manifest.get("rules_version")
    if rules_version not in SUPPORTED_RULES_VERSIONS:
        raise PackageImportError("UPGRADE_REQUIRED", f"Unsupported rules_version {rules_version}.")

    software_min = manifest.get("software_min_version", "0.0.0")
    if _version_tuple(SOFTWARE_VERSION) < _version_tuple(software_min):
        raise PackageImportError(
            "UPGRADE_REQUIRED",
            f"Package requires station software >= {software_min}, this build is {SOFTWARE_VERSION}.",
        )

    # Integrity: the seed must match the manifest's configuration_hash (no secret needed).
    if sha256_prefixed(seed) != manifest.get("configuration_hash"):
        raise PackageImportError("CONFIGURATION_MISMATCH", "Seed does not match manifest configuration_hash.")

    station_code = manifest.get("station_code")
    exam_id = manifest.get("exam_id")

    # Wrong-target rejection: once a station adopts an identity, all future
    # packages must match it.
    existing_station = _meta_get(conn, "station_code")
    existing_exam = _meta_get(conn, "exam_id")
    if existing_station is not None and existing_station != station_code:
        raise PackageImportError(
            "CONFIGURATION_MISMATCH",
            f"Package is for station {station_code}, but this station is {existing_station}.",
        )
    if existing_exam is not None and existing_exam != str(exam_id):
        raise PackageImportError(
            "CONFIGURATION_MISMATCH",
            f"Package is for exam {exam_id}, but this station holds {existing_exam}.",
        )

    now = datetime.now(timezone.utc).isoformat()
    with transaction(conn):
        _meta_set(conn, "station_code", station_code)
        _meta_set(conn, "exam_id", str(exam_id))
        _meta_set(conn, "rules_version", rules_version)

        conn.execute(
            "INSERT OR REPLACE INTO packages(package_id, exam_id, station_code, package_version,"
            " rules_version, configuration_hash, manifest_json, imported_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (manifest["package_id"], exam_id, station_code, manifest["package_version"],
             rules_version, manifest["configuration_hash"], json.dumps(manifest), now),
        )

        # schools
        for s in seed.get("schools", []):
            conn.execute("INSERT OR REPLACE INTO schools(centre_number, name) VALUES(?,?)",
                         (s["centre_number"], s["name"]))
        # subjects + scoring
        for subj in seed.get("subjects", []):
            tm = subj.get("total_marks", {})
            conn.execute(
                "INSERT OR REPLACE INTO subjects(subject_code, name, total_theory1, total_theory2, total_practical)"
                " VALUES(?,?,?,?,?)",
                (subj["subject_code"], subj["name"], tm.get("THEORY1", 0),
                 tm.get("THEORY2", 0), tm.get("PRACTICAL", 0)),
            )
            for g in subj.get("groups", []):
                conn.execute(
                    "INSERT OR REPLACE INTO question_groups(subject_code, paper_type, code, name, instruction, pick_count)"
                    " VALUES(?,?,?,?,?,?)",
                    (subj["subject_code"], g["paper_type"], g["code"], g.get("name"),
                     g.get("instruction"), g["pick_count"]),
                )
            for q in subj.get("questions", []):
                cur = conn.execute(
                    "INSERT OR REPLACE INTO questions(subject_code, paper_type, question_number, group_code, max_marks)"
                    " VALUES(?,?,?,?,?)",
                    (subj["subject_code"], q["paper_type"], q["question_number"],
                     q.get("group_code"), float(q["max_marks"])),
                )
                qid = cur.lastrowid
                for t in q.get("topics", []):
                    conn.execute(
                        "INSERT INTO question_topics(question_id, topic_id, weight) VALUES(?,?,?)",
                        (qid, t["topic_id"], float(t["weight"])),
                    )
        # students + registrations
        for st in seed.get("students", []):
            conn.execute(
                "INSERT OR REPLACE INTO students(student_id, centre_number, first_name, middle_name, surname, sex)"
                " VALUES(?,?,?,?,?,?)",
                (st["student_id"], st["centre_number"], st["first_name"],
                 st.get("middle_name"), st["surname"], st["sex"]),
            )
        for r in seed.get("registrations", []):
            conn.execute(
                "INSERT OR REPLACE INTO student_subjects(student_id, subject_code) VALUES(?,?)",
                (r["student_id"], r["subject_code"]),
            )
        # credentials (hashes only)
        for c in seed.get("credentials", []):
            conn.execute(
                "INSERT INTO station_users(assignment_id, role, pin_hash, initials, password_hash, active)"
                " VALUES(?,?,?,?,?,1)",
                (c["assignment_id"], c["role"], c.get("pin_hash"),
                 c.get("initials"), c.get("password_hash")),
            )

    return {
        "package_id": manifest["package_id"],
        "station_code": station_code,
        "exam_id": exam_id,
        "schools": len(seed.get("schools", [])),
        "subjects": len(seed.get("subjects", [])),
        "students": len(seed.get("students", [])),
        "credentials": len(seed.get("credentials", [])),
    }
