"""Local entry service (station side).

All validation goes through ``lazeims_common`` — identical rules to Central.
Every committed change writes its domain row(s) AND the matching ``outbox_events``
row in ONE SQLite transaction, so a crash can never leave a domain change without
its sync event (or vice versa).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from lazeims_common.enums import FillingMode, PaperType, RejectionCode
from lazeims_common.errors import ValidationError
from lazeims_common.validation.attendance import (
    AttendanceRow,
    effective_attendance,
    has_specific_transcription,
)
from lazeims_common.validation.config import (
    PaperConfig,
    QuestionConfig,
    QuestionGroupConfig,
)
from lazeims_common.validation.marks import validate_marks_submission

from . import outbox
from .db import transaction
from .locking import scope_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def exam_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM station_meta WHERE key='exam_id'").fetchone()
    if row is None:
        raise ValidationError(RejectionCode.CONFIGURATION_MISMATCH, "No exam configured on this station.")
    return row["value"]


def _natural_key(conn, student_id, subject_code, paper_type: PaperType, question_number=None) -> dict:
    nk = {
        "exam_id": exam_id(conn),
        "student_id": student_id,
        "subject_code": subject_code,
        "paper_type": paper_type.value,
    }
    if question_number is not None:
        nk["question_number"] = question_number
    return nk


def _require_registered(conn, student_id, subject_code) -> None:
    row = conn.execute(
        "SELECT 1 FROM student_subjects WHERE student_id = ? AND subject_code = ?",
        (student_id, subject_code),
    ).fetchone()
    if row is None:
        raise ValidationError(
            RejectionCode.NOT_REGISTERED,
            f"Student {student_id} is not registered for subject {subject_code}.",
            {"student_id": student_id, "subject_code": subject_code},
        )


def _centre_of(conn, student_id) -> str:
    row = conn.execute("SELECT centre_number FROM students WHERE student_id = ?", (student_id,)).fetchone()
    if row is None:
        raise ValidationError(RejectionCode.NOT_REGISTERED, f"Unknown student {student_id}.")
    return row["centre_number"]


def _is_finalized(conn, centre, subject_code, paper_type: PaperType) -> bool:
    key = scope_key(centre, subject_code, paper_type.value)
    return conn.execute("SELECT 1 FROM finalized_scopes WHERE scope_key = ?", (key,)).fetchone() is not None


def build_paper_config(conn: sqlite3.Connection, subject_code: str, paper_type: PaperType) -> PaperConfig:
    subj = conn.execute("SELECT * FROM subjects WHERE subject_code = ?", (subject_code,)).fetchone()
    if subj is None:
        raise ValidationError(RejectionCode.CONFIGURATION_MISMATCH, f"Unknown subject {subject_code}.")
    paper_max = {
        PaperType.THEORY1: subj["total_theory1"],
        PaperType.THEORY2: subj["total_theory2"],
        PaperType.PRACTICAL: subj["total_practical"],
    }[paper_type]
    groups = conn.execute(
        "SELECT * FROM question_groups WHERE subject_code = ? AND paper_type = ?",
        (subject_code, paper_type.value),
    ).fetchall()
    questions = conn.execute(
        "SELECT * FROM questions WHERE subject_code = ? AND paper_type = ?",
        (subject_code, paper_type.value),
    ).fetchall()
    return PaperConfig(
        paper_type=paper_type,
        paper_max=Decimal(str(paper_max)),
        questions=tuple(
            QuestionConfig(
                question_number=q["question_number"],
                max_marks=Decimal(str(q["max_marks"])),
                group_code=q["group_code"],
            )
            for q in questions
        ),
        groups=tuple(QuestionGroupConfig(code=g["code"], pick_count=g["pick_count"]) for g in groups),
    )


def _attendance_rows(conn, student_id, subject_code) -> list[AttendanceRow]:
    rows = conn.execute(
        "SELECT paper_type, is_present FROM attendance WHERE student_id = ? AND subject_code = ?",
        (student_id, subject_code),
    ).fetchall()
    return [AttendanceRow(paper_type=PaperType(r["paper_type"]), is_present=bool(r["is_present"])) for r in rows]


def resolve_effective_attendance(conn, student_id, subject_code, paper_type: PaperType) -> bool:
    return effective_attendance(_attendance_rows(conn, student_id, subject_code), paper_type)


# ---------------- attendance ----------------

def transcribe_attendance(
    conn, *, student_id, subject_code, paper_type: PaperType,
    is_present: bool, source: str, actor_assignment_id: int | None,
) -> str:
    if paper_type == PaperType.ALL:
        raise ValidationError(RejectionCode.CONFIGURATION_MISMATCH, "Use a real paper for a DE transcription.")
    _require_registered(conn, student_id, subject_code)
    centre = _centre_of(conn, student_id)
    if _is_finalized(conn, centre, subject_code, paper_type):
        raise ValidationError(RejectionCode.SCOPE_ALREADY_FINALIZED, "Scope is finalized.")
    # Prevent marking absent if marks already exist for this student-subject-paper
    if not is_present:
        has_marks = conn.execute(
            "SELECT 1 FROM total_marks WHERE student_id=? AND subject_code=? AND paper_type=?",
            (student_id, subject_code, paper_type.value),
        ).fetchone()
        if has_marks:
            raise ValidationError(
                RejectionCode.SCOPE_ALREADY_FINALIZED,
                "Cannot mark absent — marks already entered for this paper.",
                {"student_id": student_id, "subject_code": subject_code, "paper_type": paper_type.value},
            )
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance(student_id, subject_code, paper_type, is_present, source, transcribed_by, transcribed_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(student_id, subject_code, paper_type) DO UPDATE SET"
            " is_present=excluded.is_present, source=excluded.source,"
            " transcribed_by=excluded.transcribed_by, transcribed_at=excluded.transcribed_at",
            (student_id, subject_code, paper_type.value, 1 if is_present else 0, source,
             actor_assignment_id, _now()),
        )
        event_id = outbox.add_event(
            conn, entity_type="ATTENDANCE_TRANSCRIBED",
            natural_key=_natural_key(conn, student_id, subject_code, paper_type),
            value={"is_present": is_present, "source": source},
            actor_assignment_id=actor_assignment_id,
        )
    return event_id


# ---------------- incidents ----------------

def raise_incident(
    conn, *, student_id: str | None, subject_code, paper_type: PaperType,
    incident_type: str, explanation: str | None, actor_assignment_id: int | None,
) -> str:
    if student_id is not None:
        _require_registered(conn, student_id, subject_code)
    with transaction(conn):
        conn.execute(
            "INSERT INTO exam_incidents(student_id, subject_code, paper_type, incident_type, status, explanation, raised_by, raised_at)"
            " VALUES(?,?,?,?, 'OPEN', ?,?,?)",
            (student_id, subject_code, paper_type.value, incident_type, explanation, actor_assignment_id, _now()),
        )
        nk = _natural_key(conn, student_id or "*", subject_code, paper_type)
        event_id = outbox.add_event(
            conn, entity_type="INCIDENT_RAISED", natural_key=nk,
            value={"incident_type": incident_type, "explanation": explanation, "student_id": student_id},
            actor_assignment_id=actor_assignment_id,
        )
    return event_id


# ---------------- marks ----------------

def apply_student_paper_marks(
    conn, *, student_id, subject_code, paper_type: PaperType, mode: FillingMode,
    total_marks_obtained=None, items: dict[str, object] | None = None,
    actor_assignment_id: int | None,
) -> dict:
    _require_registered(conn, student_id, subject_code)
    centre = _centre_of(conn, student_id)
    if _is_finalized(conn, centre, subject_code, paper_type):
        raise ValidationError(RejectionCode.SCOPE_ALREADY_FINALIZED, "Scope is finalized.")

    has_att = has_specific_transcription(_attendance_rows(conn, student_id, subject_code), paper_type)
    is_present = resolve_effective_attendance(conn, student_id, subject_code, paper_type)
    paper = build_paper_config(conn, subject_code, paper_type)

    # Validate through the shared rules (raises ValidationError with stable code).
    computed_total = validate_marks_submission(
        mode=mode, is_present=is_present, has_attendance_transcription=has_att,
        paper=paper, total_marks_obtained=total_marks_obtained, item_marks=items,
    )

    with transaction(conn):
        # --- Read before-state for audit ---
        before_total = None
        before_items = None
        if mode == FillingMode.TOTAL_MARKS:
            existing = conn.execute(
                "SELECT total_marks_obtained FROM total_marks WHERE student_id=? AND subject_code=? AND paper_type=?",
                (student_id, subject_code, paper_type.value)
            ).fetchone()
            before_total = existing["total_marks_obtained"] if existing else None
        else:  # ITEM_LEVEL
            existing_items = conn.execute(
                "SELECT q.question_number, im.marks_obtained FROM item_marks im"
                " JOIN questions q ON q.id = im.question_id"
                " WHERE im.student_id=? AND q.subject_code=? AND q.paper_type=?",
                (student_id, subject_code, paper_type.value)
            ).fetchall()
            if existing_items:
                before_items = json.dumps({r["question_number"]: r["marks_obtained"] for r in existing_items})

        if mode == FillingMode.TOTAL_MARKS:
            conn.execute(
                "DELETE FROM total_marks WHERE student_id=? AND subject_code=? AND paper_type=?",
                (student_id, subject_code, paper_type.value),
            )
            if is_present:
                conn.execute(
                    "INSERT INTO total_marks(student_id, subject_code, paper_type, total_marks_obtained, entered_by, entered_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (student_id, subject_code, paper_type.value, float(total_marks_obtained),
                     actor_assignment_id, _now()),
                )
            value = {"mode": mode.value, "total": None if total_marks_obtained is None else str(total_marks_obtained)}
        else:  # ITEM_LEVEL
            qids = [r["id"] for r in conn.execute(
                "SELECT id FROM questions WHERE subject_code=? AND paper_type=?",
                (subject_code, paper_type.value)).fetchall()]
            if qids:
                conn.execute(
                    f"DELETE FROM item_marks WHERE student_id=? AND question_id IN ({','.join('?'*len(qids))})",
                    (student_id, *qids),
                )
            if is_present:
                qid_by_num = {r["question_number"]: r["id"] for r in conn.execute(
                    "SELECT id, question_number FROM questions WHERE subject_code=? AND paper_type=?",
                    (subject_code, paper_type.value)).fetchall()}
                for qnum, mark in (items or {}).items():
                    conn.execute(
                        "INSERT INTO item_marks(student_id, question_id, marks_obtained, entered_by, entered_at)"
                        " VALUES(?,?,?,?,?)",
                        (student_id, qid_by_num[qnum], float(mark), actor_assignment_id, _now()),
                    )
            value = {"mode": mode.value, "items": {k: str(v) for k, v in (items or {}).items()}}

        event_id = outbox.add_event(
            conn, entity_type="STUDENT_PAPER_MARKS_REPLACED",
            natural_key=_natural_key(conn, student_id, subject_code, paper_type),
            value=value, actor_assignment_id=actor_assignment_id,
        )

        # --- Write audit row ---
        import json as _json
        if mode == FillingMode.TOTAL_MARKS:
            after_total = float(total_marks_obtained) if is_present and total_marks_obtained is not None else None
            operation = 'SET' if after_total is not None else 'CLEAR'
            after_items_json = None
        else:
            after_total = None
            after_items_json = _json.dumps({k: float(v) for k, v in (items or {}).items()}) if is_present and items else None
            operation = 'ITEM_SET' if after_items_json else 'ITEM_CLEAR'

        conn.execute(
            "INSERT INTO marks_audit(student_id, subject_code, paper_type, operation, mode,"
            " before_total, before_items, after_total, after_items,"
            " actor_assignment_id, station_occurred_at, event_id, scope_was_finalized)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (student_id, subject_code, paper_type.value, operation, mode.value,
             before_total, before_items, after_total, after_items_json,
             actor_assignment_id, _now(), event_id, 0)
        )

    return {
        "student_id": student_id, "subject_code": subject_code, "paper_type": paper_type.value,
        "is_present": is_present, "computed_total": str(computed_total) if computed_total is not None else None,
        "event_id": event_id,
    }
