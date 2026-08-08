"""Local finalize sweep for a scope ``(school, subject, paper)`` on the station.

Assembles per-student completeness from SQLite and evaluates it with the shared
``evaluate_scope_completeness`` rule. On zero blockers, writes a
``finalized_scopes`` row + a ``SCOPE_FINALIZED`` outbox event and converts the
work lock to FINALIZED — all in one transaction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from lazeims_common.enums import FillingMode, PaperType
from lazeims_common.validation.scope import (
    StudentScopeState,
    evaluate_scope_completeness,
)

from . import outbox
from .db import transaction
from .entry import _natural_key, exam_id, resolve_effective_attendance
from .locking import mark_finalized, scope_key


def _filling_mode(conn) -> FillingMode:
    # The station's package encodes mode implicitly via presence of questions;
    # default to TOTAL_MARKS unless the subject/paper has configured questions.
    return FillingMode.TOTAL_MARKS


def _students_in_scope(conn, centre_number, subject_code):
    return conn.execute(
        "SELECT s.student_id FROM students s"
        " JOIN student_subjects ss ON ss.student_id = s.student_id"
        " WHERE s.centre_number = ? AND ss.subject_code = ?",
        (centre_number, subject_code),
    ).fetchall()


def evaluate(conn: sqlite3.Connection, centre_number: str, subject_code: str, paper_type: PaperType) -> dict:
    # Mode per scope: item-level iff questions are configured for this subject/paper.
    qrows = conn.execute(
        "SELECT id FROM questions WHERE subject_code=? AND paper_type=?",
        (subject_code, paper_type.value),
    ).fetchall()
    is_item = len(qrows) > 0
    required_qids = [r["id"] for r in qrows]

    # open incidents for the scope
    inc_rows = conn.execute(
        "SELECT student_id FROM exam_incidents WHERE subject_code=? AND paper_type=?"
        " AND status IN ('OPEN','UNDER_REVIEW')",
        (subject_code, paper_type.value),
    ).fetchall()
    open_students = {r["student_id"] for r in inc_rows if r["student_id"]}
    scope_wide_incident = any(r["student_id"] is None for r in inc_rows)

    student_rows = _students_in_scope(conn, centre_number, subject_code)
    student_ids = [r["student_id"] for r in student_rows]

    if not student_ids:
        result = evaluate_scope_completeness([], has_unresolved_scope_incident=scope_wide_incident)
        out = result.as_dict()
        out["student_count"] = 0
        return out

    # Batch attendance for all students in this scope
    placeholders = ",".join("?" * len(student_ids))
    att_rows = conn.execute(
        f"SELECT student_id, paper_type, is_present FROM attendance"
        f" WHERE student_id IN ({placeholders}) AND subject_code = ?",
        (*student_ids, subject_code),
    ).fetchall()
    # Resolve per student: specific paper overrides ALL
    att_specific: dict[str, bool] = {}
    att_all: dict[str, bool] = {}
    for r in att_rows:
        if r["paper_type"] == paper_type.value:
            att_specific[r["student_id"]] = bool(r["is_present"])
        elif r["paper_type"] == "ALL":
            att_all[r["student_id"]] = bool(r["is_present"])
    att_map = {sid: att_specific.get(sid, att_all.get(sid, True)) for sid in student_ids}

    # Batch marks
    if is_item and required_qids:
        q_placeholders = ",".join("?" * len(required_qids))
        im_rows = conn.execute(
            f"SELECT student_id, COUNT(*) c FROM item_marks"
            f" WHERE student_id IN ({placeholders}) AND question_id IN ({q_placeholders})"
            f" GROUP BY student_id",
            (*student_ids, *required_qids),
        ).fetchall()
        im_counts = {r["student_id"]: r["c"] for r in im_rows}
    elif not is_item:
        tm_rows = conn.execute(
            f"SELECT student_id FROM total_marks"
            f" WHERE student_id IN ({placeholders}) AND subject_code = ? AND paper_type = ?",
            (*student_ids, subject_code, paper_type.value),
        ).fetchall()
        tm_set = {r["student_id"] for r in tm_rows}

    states: list[StudentScopeState] = []
    for sid in student_ids:
        present = att_map.get(sid, True)
        if is_item:
            if required_qids:
                complete = im_counts.get(sid, 0) == len(required_qids)
            else:
                complete = False
        else:
            complete = sid in tm_set
        if not present:
            complete = True
        states.append(StudentScopeState(
            student_id=sid, is_present=present, has_complete_marks=complete,
            has_open_incident=sid in open_students,
        ))

    result = evaluate_scope_completeness(states, has_unresolved_scope_incident=scope_wide_incident)
    out = result.as_dict()
    out["student_count"] = len(states)
    return out


def finalize(
    conn: sqlite3.Connection, *, centre_number: str, subject_code: str,
    paper_type: PaperType, finalized_by: int | None,
) -> tuple[bool, dict]:
    result = evaluate(conn, centre_number, subject_code, paper_type)
    if not result["complete"]:
        return False, result

    key = scope_key(centre_number, subject_code, paper_type.value)
    now = datetime.now(timezone.utc).isoformat()
    with transaction(conn):
        conn.execute(
            "INSERT INTO finalized_scopes(scope_key, revision, finalized_by, finalized_at, snapshot_json)"
            " VALUES(?, COALESCE((SELECT revision+1 FROM finalized_scopes WHERE scope_key=?),1), ?,?,?)"
            " ON CONFLICT(scope_key) DO UPDATE SET revision=finalized_scopes.revision+1,"
            " finalized_by=excluded.finalized_by, finalized_at=excluded.finalized_at, snapshot_json=excluded.snapshot_json",
            (key, key, finalized_by, now, json.dumps(result)),
        )
        mark_finalized(conn, key)
        nk = {
            "exam_id": exam_id(conn),
            "centre_number": centre_number,
            "subject_code": subject_code,
            "paper_type": paper_type.value,
        }
        outbox.add_event(
            conn, entity_type="SCOPE_FINALIZED", natural_key=nk,
            value={"student_count": result["student_count"]}, actor_assignment_id=finalized_by,
        )
    return True, result
