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


# --- schema baseline ---
_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY, exam_id TEXT NOT NULL, station_code TEXT NOT NULL,
    package_version INTEGER NOT NULL, rules_version TEXT NOT NULL,
    configuration_hash TEXT NOT NULL, manifest_json TEXT NOT NULL, imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_users (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL UNIQUE,
    role TEXT NOT NULL,
    pin_hash TEXT,
    initials TEXT,
    password_hash TEXT,
    admin_username TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    first_name TEXT,
    middle_name TEXT,
    surname TEXT,
    phone TEXT
);

CREATE TABLE IF NOT EXISTS user_scopes (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL,
    centre_number TEXT,
    subject_code TEXT,
    UNIQUE(assignment_id, centre_number, subject_code)
);

CREATE TABLE IF NOT EXISTS machine_credentials (
    credential_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    stored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schools (centre_number TEXT PRIMARY KEY, name TEXT NOT NULL, council_name TEXT, region_name TEXT);

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
    last_error TEXT, ack_at TEXT,
    priority INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY, actor INTEGER, action TEXT NOT NULL, entity_type TEXT,
    entity_id TEXT, before_json TEXT, after_json TEXT, occurred_at TEXT NOT NULL
);
"""
def apply_migrations(conn: sqlite3.Connection, *, backup_before_upgrade: str | None = None) -> int:
    """Bring the DB up to SCHEMA_VERSION. Returns the resulting version.

    Fresh install: create the full baseline schema. Reject any newer
    ``user_version`` (means the DB came from a later build).
    """
    version = get_user_version(conn)

    if version < 1:
        conn.executescript(_SCHEMA)
        set_user_version(conn, 1)
        version = 1

    # Idempotent column additions for existing DBs (SQLite has no IF NOT EXISTS on ADD COLUMN)
    for col in ("first_name TEXT", "middle_name TEXT", "surname TEXT", "phone TEXT"):
        col_name = col.split()[0]
        try:
            conn.execute(f"ALTER TABLE station_users ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass  # column already exists

    if version < 2:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS marks_audit (
                id          INTEGER PRIMARY KEY,
                student_id  TEXT    NOT NULL,
                subject_code TEXT   NOT NULL,
                paper_type  TEXT    NOT NULL,
                operation   TEXT    NOT NULL,
                mode        TEXT    NOT NULL,
                before_total REAL,
                before_items TEXT,
                after_total  REAL,
                after_items  TEXT,
                actor_assignment_id INTEGER,
                station_occurred_at TEXT NOT NULL,
                event_id    TEXT,
                scope_was_finalized INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_marks_audit_student
                ON marks_audit (student_id, subject_code, paper_type);
            CREATE INDEX IF NOT EXISTS idx_marks_audit_actor
                ON marks_audit (actor_assignment_id, station_occurred_at);
            CREATE INDEX IF NOT EXISTS idx_marks_audit_event
                ON marks_audit (event_id);
        """)
        set_user_version(conn, 2)
        version = 2

    if version < 3:
        # Revert orphan SENDING events that survived a crash
        conn.execute(
            "UPDATE outbox_events SET status='PENDING', "
            "last_error='reverted_from_sending_at_startup_v3' "
            "WHERE status='SENDING'"
        )
        conn.commit()
        set_user_version(conn, 3)
        version = 3

    # Always run at startup: revert any SENDING events left by a previous crash
    conn.execute(
        "UPDATE outbox_events SET status='PENDING', last_error='reverted_at_startup'"
        " WHERE status='SENDING'"
    )
    conn.commit()

    if version < 4:
        # Add council_name and region_name to schools table
        for col in ("council_name TEXT", "region_name TEXT"):
            try:
                conn.execute(f"ALTER TABLE schools ADD COLUMN {col}")
                conn.commit()
            except Exception:
                pass  # column already exists
        set_user_version(conn, 4)
        version = 4

    if version > SCHEMA_VERSION:
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
            conn.execute("INSERT OR REPLACE INTO schools(centre_number, name, council_name, region_name) VALUES(?,?,?,?)",
                         (s["centre_number"], s["name"], s.get("council_name"), s.get("region_name")))
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
        # Seed marks: only insert if no local marks exist for that scope.
        # This preserves any locally-entered data while seeding initial state
        # from existing online marks.
        for m in seed.get("marks", []):
            existing = conn.execute(
                "SELECT 1 FROM total_marks WHERE student_id=? AND subject_code=? AND paper_type=?",
                (m["student_id"], m["subject_code"], m["paper_type"]),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO total_marks(student_id, subject_code, paper_type,"
                    " total_marks_obtained, entered_by, entered_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (m["student_id"], m["subject_code"], m["paper_type"],
                     float(m["total_marks_obtained"]), None, now),
                )

        for im in seed.get("item_marks", []):
            # Resolve question_id from subject_code + paper_type + question_number
            q_row = conn.execute(
                "SELECT id FROM questions WHERE subject_code=? AND paper_type=? AND question_number=?",
                (im["subject_code"], im["paper_type"], im["question_number"]),
            ).fetchone()
            if q_row:
                existing = conn.execute(
                    "SELECT 1 FROM item_marks WHERE student_id=? AND question_id=?",
                    (im["student_id"], q_row["id"]),
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO item_marks(student_id, question_id, marks_obtained,"
                        " entered_by, entered_at)"
                        " VALUES(?,?,?,?,?)",
                        (im["student_id"], q_row["id"], float(im["marks_obtained"]),
                         None, now),
                    )
        # credentials (hashes only) — upsert by assignment_id so re-imports do
        # not duplicate the same person.
        for c in seed.get("credentials", []):
            conn.execute(
                "INSERT INTO station_users(assignment_id, role, pin_hash, initials,"
                " password_hash, admin_username, active) VALUES(?,?,?,?,?,?,1)"
                " ON CONFLICT(assignment_id) DO UPDATE SET"
                "   role = excluded.role,"
                "   pin_hash = COALESCE(excluded.pin_hash, station_users.pin_hash),"
                "   initials = COALESCE(excluded.initials, station_users.initials),"
                "   password_hash = COALESCE(excluded.password_hash, station_users.password_hash),"
                "   admin_username = COALESCE(excluded.admin_username, station_users.admin_username),"
                "   active = 1",
                (c["assignment_id"], c["role"], c.get("pin_hash"), c.get("initials"),
                 c.get("password_hash"), c.get("admin_username")),
            )

        # Data enterer scopes: replace-per-assignment so package updates take
        # effect immediately without leaking stale scopes.
        for de in manifest.get("data_enterers", []) or []:
            aid = de.get("assignment_id")
            if aid is None:
                continue
            conn.execute("DELETE FROM user_scopes WHERE assignment_id = ?", (aid,))
            for cn in de.get("school_centre_numbers") or []:
                conn.execute(
                    "INSERT OR IGNORE INTO user_scopes(assignment_id, centre_number, subject_code)"
                    " VALUES(?,?,NULL)",
                    (aid, cn),
                )
            for sc in de.get("subject_codes") or []:
                conn.execute(
                    "INSERT OR IGNORE INTO user_scopes(assignment_id, centre_number, subject_code)"
                    " VALUES(?,NULL,?)",
                    (aid, sc),
                )

        # Machine credential bookkeeping (only the credential_id + package
        # linkage is stored here; the plaintext secret lives in protected
        # local storage, see machine_credential.py).
        mc_meta = manifest.get("machine_credential") or {}
        if mc_meta.get("credential_id"):
            conn.execute(
                "INSERT OR REPLACE INTO machine_credentials(credential_id, package_id, stored_at)"
                " VALUES(?,?,?)",
                (mc_meta["credential_id"], manifest["package_id"], now),
            )

    return {
        "package_id": manifest["package_id"],
        "station_code": station_code,
        "exam_id": exam_id,
        "schools": len(seed.get("schools", [])),
        "subjects": len(seed.get("subjects", [])),
        "students": len(seed.get("students", [])),
        "credentials": len(seed.get("credentials", [])),
        "marks": len(seed.get("marks", [])),
        "item_marks": len(seed.get("item_marks", [])),
    }
