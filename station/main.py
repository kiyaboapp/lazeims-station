"""Station FastAPI app.

Serves a fully-local static shell (no CDN) and a small local API:
    GET  /                 -> offline UI shell
    GET  /health           -> liveness
    POST /api/import       -> import a scope-only package bundle
    POST /api/login/de     -> Data Enterer login (PIN + initials)
    POST /api/login/admin  -> Station Exam Admin login (password)
    GET  /api/me           -> current local session identity
    GET  /api/status       -> imported package summary
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import SOFTWARE_VERSION
from . import entry as entry_svc
from . import finalize as finalize_svc
from . import locking, outbox
from .auth import SessionManager, authenticate_admin, authenticate_de
from .config import StationConfig, load_config
from .db import connect
from .migrations import PackageImportError, apply_migrations, import_package

from lazeims_common.enums import FillingMode, PaperType
from lazeims_common.errors import ValidationError

_STATIC = Path(__file__).parent / "static"


class DeLogin(BaseModel):
    pin: str
    initials: str


class AdminLogin(BaseModel):
    username: str
    password: str


class LockIn(BaseModel):
    centre_number: str
    subject_code: str
    paper_type: PaperType


class ForceReleaseIn(LockIn):
    reason: str


class AttendanceIn(BaseModel):
    student_id: str
    subject_code: str
    paper_type: PaperType
    is_present: bool
    source: str = "INVIGILATOR_ISAL_TRANSCRIPTION"


class IncidentIn(BaseModel):
    subject_code: str
    paper_type: PaperType
    student_id: str | None = None
    incident_type: str = "MISSING_SCRIPT"
    explanation: str | None = None


class ItemEntry(BaseModel):
    question_number: str
    marks: float


class MarksIn(BaseModel):
    subject_code: str
    paper_type: PaperType
    mode: FillingMode
    total_marks_obtained: float | None = None
    items: list[ItemEntry] | None = None


class ScopeIn(BaseModel):
    centre_number: str
    subject_code: str
    paper_type: PaperType


class SyncConfigIn(BaseModel):
    central_url: str | None = None


class CreateUserIn(BaseModel):
    initials: str
    pin: str
    first_name: str | None = None
    middle_name: str | None = None
    surname: str | None = None
    phone: str | None = None
    centre_numbers: list[str] | None = None
    subject_codes: list[str] | None = None


def create_app(config: StationConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    # Prepare DB + schema at startup (safe to call repeatedly).
    conn = connect(cfg.db_path)
    apply_migrations(conn)
    conn.close()

    # Auto-import any package(s) shipped inside station_data/import/ or
    # pre-staged in stations/<code>/exams/<id>/imports/pending/.
    from .auto_import import auto_import_pending

    import_results = auto_import_pending(cfg)
    for r in import_results:
        print(f"[auto-import] {r.get('file')}: {r.get('status')}"
              + (f" ({r.get('students')} students)" if r.get("status") == "imported" else "")
              + (f" — {r.get('code')}: {r.get('message')}" if r.get("status") in {"rejected", "error"} else ""))

    # If we were on a fresh install (no station known) and just imported a
    # package, reload config so we use the correct per-exam database rather
    # than the temporary setup/ database.
    if config is None and any(r.get("status") == "imported" for r in import_results):
        cfg = load_config()
        conn = connect(cfg.db_path)
        apply_migrations(conn)
        conn.close()
        print(f"[auto-import] config reloaded: station={cfg.station_code} exam={cfg.exam_id}")

    # Seed a default Central URL from the bundle (station_data/sync.json) so the
    # station knows where to sync; the admin still supplies the secret sync key.
    try:
        import json as _json
        sync_file = cfg.data_dir / "sync.json"
        if sync_file.is_file():
            url = (_json.loads(sync_file.read_text() or "{}") or {}).get("central_url")
            if url:
                from .sync_http import seed_central_url_if_unset
                _c = connect(cfg.db_path)
                try:
                    seed_central_url_if_unset(_c, url)
                finally:
                    _c.close()
    except Exception as exc:  # never block boot on this
        print(f"[sync] could not seed central_url: {exc}")

    sessions = SessionManager(cfg.secret_key, cfg.session_ttl_seconds)
    app = FastAPI(title="LAZEIMS Station", version=SOFTWARE_VERSION)
    app.state.cfg = cfg
    app.state.sessions = sessions

    def db():
        conn = connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def current(session=Cookie(default=None, alias=cfg.session_cookie)):
        data = sessions.resolve(session or "")
        if data is None:
            raise HTTPException(401, "Not authenticated")
        return data

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok", "software_version": SOFTWARE_VERSION}

    @app.get("/api/status", tags=["ops"])
    def status(conn=Depends(db)):
        row = conn.execute("SELECT value FROM station_meta WHERE key='station_code'").fetchone()
        exam = conn.execute("SELECT value FROM station_meta WHERE key='exam_id'").fetchone()
        pkgs = conn.execute("SELECT COUNT(*) c FROM packages").fetchone()["c"]
        students = conn.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
        return {
            "station_code": row["value"] if row else None,
            "exam_id": exam["value"] if exam else None,
            "packages": pkgs,
            "students": students,
            "software_version": SOFTWARE_VERSION,
        }

    @app.post("/api/import", tags=["setup"])
    async def do_import(
        request_body: dict | None = None,
        file: UploadFile | None = File(default=None),
        conn=Depends(db),
    ):
        """Import a signed exam package.

        Accepts either a JSON bundle body (legacy path used by tests and the
        auto-importer) or a ``multipart/form-data`` file upload containing the
        signed ZIP produced by Central.
        """
        try:
            if file is not None:
                data = await file.read()
                result = package_import.import_signed_zip(conn, data)
            elif request_body is not None:
                result = package_import.import_bundle(conn, request_body)
            else:
                raise HTTPException(422, "Provide either a JSON body or a ZIP upload")
        except PackageImportError as exc:
            return JSONResponse(status_code=422,
                                content={"error": {"code": exc.code, "message": exc.message}})
        return {"imported": True, **result}

    @app.post("/api/login/de", tags=["auth"])
    def login_de(payload: DeLogin, response: Response, conn=Depends(db)):
        user = authenticate_de(conn, payload.pin, payload.initials)
        if user is None:
            raise HTTPException(401, "Invalid PIN or initials")
        token = sessions.issue(user["id"], "DATA_ENTERER")
        response.set_cookie(cfg.session_cookie, token, httponly=True, samesite="lax",
                            max_age=cfg.session_ttl_seconds)
        return {"role": "DATA_ENTERER", "initials": user["initials"], "assignment_id": user["assignment_id"]}

    @app.post("/api/login/admin", tags=["auth"])
    def login_admin(payload: AdminLogin, response: Response, conn=Depends(db)):
        user = authenticate_admin(conn, payload.username, payload.password)
        if user is None:
            raise HTTPException(401, "Invalid username or password")
        token = sessions.issue(user["id"], "EXAM_ADMIN")
        response.set_cookie(cfg.session_cookie, token, httponly=True, samesite="lax",
                            max_age=cfg.session_ttl_seconds)
        return {
            "role": "EXAM_ADMIN",
            "assignment_id": user["assignment_id"],
            "username": user.get("admin_username") or user.get("initials"),
        }

    @app.post("/api/logout", tags=["auth"])
    def logout(response: Response):
        response.delete_cookie(cfg.session_cookie)
        return {"ok": True}

    @app.post("/api/admin/reset-password", tags=["auth"])
    def reset_admin_password(payload: dict, conn=Depends(db)):
        """Reset station admin password using machine credential secret as proof.
        Body: { machine_secret: str, new_password: str }
        """
        from .machine_credential import load as _load_mc
        from .auth import hash_secret as _hash_secret

        machine_secret = (payload.get("machine_secret") or "").strip()
        new_password   = (payload.get("new_password") or "").strip()

        if not machine_secret or not new_password:
            raise HTTPException(422, "machine_secret and new_password are required")
        if len(new_password) < 6:
            raise HTTPException(422, "new_password must be at least 6 characters")

        mc = _load_mc(cfg.station_code or "", cfg.exam_id or "")
        if mc is None:
            raise HTTPException(503, "Machine credential not available")
        if machine_secret != mc.get("secret", ""):
            raise HTTPException(403, "Machine secret is incorrect")

        new_hash = _hash_secret(new_password)
        updated = conn.execute(
            "UPDATE station_users SET password_hash = ? WHERE role = 'EXAM_ADMIN' AND active = 1",
            (new_hash,)
        ).rowcount
        conn.commit()

        if updated == 0:
            raise HTTPException(404, "No active admin user found")

        # Also return the username so the caller knows what to use
        row = conn.execute(
            "SELECT admin_username, initials FROM station_users WHERE role = 'EXAM_ADMIN' AND active = 1 LIMIT 1"
        ).fetchone()
        username = (row["admin_username"] or row["initials"]) if row else "admin"
        return {"ok": True, "username": username}

    @app.get("/api/me", tags=["auth"])
    def me(session=Depends(current)):
        return session

    # ---------------- entry ----------------

    def actor(session=Depends(current), conn=Depends(db)) -> dict:
        row = conn.execute("SELECT id, role, assignment_id FROM station_users WHERE id = ?",
                           (session["uid"],)).fetchone()
        if row is None:
            raise HTTPException(401, "Local user not found")
        return {"uid": row["id"], "role": row["role"], "assignment_id": row["assignment_id"]}

    def _verr(exc: ValidationError):
        return JSONResponse(status_code=422,
                            content={"error": {"code": exc.code.value, "message": exc.message, "details": exc.details}})

    @app.get("/api/scopes", tags=["entry"])
    def scopes(conn=Depends(db), a=Depends(actor)):
        allowed = de_scopes_for(conn, a["assignment_id"]) if a["role"] == "DATA_ENTERER" else {"centres": set(), "subjects": set()}
        rows = conn.execute(
            "SELECT DISTINCT s.centre_number, ss.subject_code FROM students s"
            " JOIN student_subjects ss ON ss.student_id = s.student_id"
        ).fetchall()
        out = []
        for r in rows:
            if a["role"] == "DATA_ENTERER" and not is_scope_allowed(
                allowed, centre_number=r["centre_number"], subject_code=r["subject_code"]
            ):
                continue
            subj = conn.execute("SELECT total_theory1, total_theory2, total_practical FROM subjects WHERE subject_code=?",
                                (r["subject_code"],)).fetchone()
            papers = ["THEORY1"] + (["THEORY2"] if subj and subj["total_theory2"] else []) + (["PRACTICAL"] if subj and subj["total_practical"] else [])
            for p in papers:
                key = locking.scope_key(r["centre_number"], r["subject_code"], p)
                lock = conn.execute("SELECT owner, status FROM work_locks WHERE scope_key=?", (key,)).fetchone()
                fin = conn.execute("SELECT 1 FROM finalized_scopes WHERE scope_key=?", (key,)).fetchone()
                out.append({
                    "centre_number": r["centre_number"], "subject_code": r["subject_code"], "paper_type": p,
                    "lock_status": (lock["status"] if lock else None),
                    "lock_owner": (lock["owner"] if lock else None),
                    "finalized": fin is not None,
                })
        return out

    @app.get("/api/roster", tags=["entry"])
    def roster(subject_code: str, paper_type: PaperType, centre_number: str, conn=Depends(db), a=Depends(actor)):
        if a["role"] == "DATA_ENTERER":
            allowed = de_scopes_for(conn, a["assignment_id"])
            if not is_scope_allowed(allowed, centre_number=centre_number, subject_code=subject_code):
                raise HTTPException(403, "Scope not permitted for this data enterer")
        students = conn.execute(
            "SELECT s.student_id, s.first_name, s.surname FROM students s"
            " JOIN student_subjects ss ON ss.student_id = s.student_id"
            " WHERE s.centre_number = ? AND ss.subject_code = ? ORDER BY s.student_id",
            (centre_number, subject_code),
        ).fetchall()
        result = []
        for st in students:
            att = conn.execute(
                "SELECT is_present FROM attendance WHERE student_id=? AND subject_code=? AND paper_type=?",
                (st["student_id"], subject_code, paper_type.value)).fetchone()
            has_total = conn.execute(
                "SELECT 1 FROM total_marks WHERE student_id=? AND subject_code=? AND paper_type=?",
                (st["student_id"], subject_code, paper_type.value)).fetchone()
            result.append({
                "student_id": st["student_id"], "first_name": st["first_name"], "surname": st["surname"],
                "attendance": (None if att is None else bool(att["is_present"])),
                "has_marks": has_total is not None,
            })
        return result

    @app.post("/api/locks/acquire", tags=["entry"])
    def lock_acquire(payload: LockIn, conn=Depends(db), a=Depends(actor)):
        key = locking.scope_key(payload.centre_number, payload.subject_code, payload.paper_type.value)
        try:
            return locking.acquire(conn, key, a["uid"])
        except locking.LockError as e:
            raise HTTPException(409, detail={"code": e.code, "message": e.message})

    @app.post("/api/locks/release", tags=["entry"])
    def lock_release(payload: LockIn, conn=Depends(db), a=Depends(actor)):
        key = locking.scope_key(payload.centre_number, payload.subject_code, payload.paper_type.value)
        try:
            return {"released": locking.release(conn, key, a["uid"])}
        except locking.LockError as e:
            raise HTTPException(409, detail={"code": e.code, "message": e.message})

    @app.post("/api/locks/force-release", tags=["entry"])
    def lock_force_release(payload: ForceReleaseIn, conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may force-release a lock")
        key = locking.scope_key(payload.centre_number, payload.subject_code, payload.paper_type.value)
        try:
            return {"released": locking.force_release(conn, key, a["uid"], payload.reason)}
        except locking.LockError as e:
            raise HTTPException(409, detail={"code": e.code, "message": e.message})

    @app.put("/api/attendance", tags=["entry"])
    def attendance(payload: AttendanceIn, conn=Depends(db), a=Depends(actor)):
        try:
            eid = entry_svc.transcribe_attendance(
                conn, student_id=payload.student_id, subject_code=payload.subject_code,
                paper_type=payload.paper_type, is_present=payload.is_present,
                source=payload.source, actor_assignment_id=a["assignment_id"])
        except ValidationError as e:
            return _verr(e)
        return {"event_id": eid}

    @app.post("/api/incidents", tags=["entry"])
    def incident(payload: IncidentIn, conn=Depends(db), a=Depends(actor)):
        try:
            eid = entry_svc.raise_incident(
                conn, student_id=payload.student_id, subject_code=payload.subject_code,
                paper_type=payload.paper_type, incident_type=payload.incident_type,
                explanation=payload.explanation, actor_assignment_id=a["assignment_id"])
        except ValidationError as e:
            return _verr(e)
        return {"event_id": eid}

    @app.put("/api/marks/students", tags=["entry"])
    def marks(student_id: str, payload: MarksIn, conn=Depends(db), a=Depends(actor)):
        items = {i.question_number: i.marks for i in payload.items} if payload.items else None
        try:
            result = entry_svc.apply_student_paper_marks(
                conn, student_id=student_id, subject_code=payload.subject_code,
                paper_type=payload.paper_type, mode=payload.mode,
                total_marks_obtained=payload.total_marks_obtained, items=items,
                actor_assignment_id=a["assignment_id"])
        except ValidationError as e:
            return _verr(e)
        return {"result": result}

    @app.put("/api/marks/students/{centre_number}/{student_seq}", tags=["entry"])
    def marks_compat(centre_number: str, student_seq: str, payload: MarksIn,
                     conn=Depends(db), a=Depends(actor)):
        """Compatibility route for older clients that used path-style student IDs.

        Old clients sent: PUT /api/marks/students/S0104/0006
        Reassemble into the canonical student_id format: S0104/0006
        """
        student_id = f"{centre_number}/{student_seq}"
        items = {i.question_number: i.marks for i in payload.items} if payload.items else None
        try:
            result = entry_svc.apply_student_paper_marks(
                conn, student_id=student_id, subject_code=payload.subject_code,
                paper_type=payload.paper_type, mode=payload.mode,
                total_marks_obtained=payload.total_marks_obtained, items=items,
                actor_assignment_id=a["assignment_id"])
        except ValidationError as e:
            return _verr(e)
        return {"result": result}

    @app.get("/api/scopes/validation", tags=["entry"])
    def scope_validation(centre_number: str, subject_code: str, paper_type: PaperType, conn=Depends(db), a=Depends(actor)):
        return finalize_svc.evaluate(conn, centre_number, subject_code, paper_type)

    @app.post("/api/scopes/finalize", tags=["entry"])
    def scope_finalize(payload: ScopeIn, conn=Depends(db), a=Depends(actor)):
        ok, result = finalize_svc.finalize(
            conn, centre_number=payload.centre_number, subject_code=payload.subject_code,
            paper_type=payload.paper_type, finalized_by=a["assignment_id"])
        if not ok:
            raise HTTPException(409, detail={"code": "SCOPE_NOT_COMPLETE", "result": result})
        _fire_sync()  # push the finalized scope to Central when online (best-effort)
        return {"finalized": True, "result": result}

    @app.get("/api/progress", tags=["entry"])
    def progress(conn=Depends(db), a=Depends(actor)):
        import datetime as _dt
        today_prefix = _dt.date.today().isoformat()  # "YYYY-MM-DD"

        # Total scopes (distinct centre+subject+paper combinations)
        all_scopes = conn.execute(
            "SELECT DISTINCT s.centre_number, ss.subject_code FROM students s"
            " JOIN student_subjects ss ON ss.student_id = s.student_id"
        ).fetchall()
        subj_map = {}
        for r in all_scopes:
            subj = conn.execute(
                "SELECT total_theory2, total_practical FROM subjects WHERE subject_code=?",
                (r["subject_code"],)).fetchone()
            papers = 1 + (1 if subj and subj["total_theory2"] else 0) + (1 if subj and subj["total_practical"] else 0)
            subj_map[(r["centre_number"], r["subject_code"])] = papers
        total_scopes = sum(subj_map.values())

        fin_count = conn.execute("SELECT COUNT(*) c FROM finalized_scopes").fetchone()["c"]

        # Today's marks (total_marks entered today)
        marks_today = conn.execute(
            "SELECT COUNT(*) c FROM total_marks WHERE entered_at LIKE ?",
            (today_prefix + "%",)
        ).fetchone()["c"]
        # Also count item-level marks sessions entered today (distinct student/subject/paper combos)
        item_today = conn.execute(
            "SELECT COUNT(DISTINCT im.student_id || '|' || q.subject_code || '|' || q.paper_type) c"
            " FROM item_marks im JOIN questions q ON q.id = im.question_id"
            " WHERE im.entered_at LIKE ?",
            (today_prefix + "%",)
        ).fetchone()["c"]
        marks_today_total = marks_today + item_today

        # Per-school breakdown
        schools = conn.execute("SELECT centre_number, name FROM schools ORDER BY centre_number").fetchall()
        per_school = []
        for school in schools:
            cn = school["centre_number"]
            # Count total scopes for this school
            school_scope_pairs = conn.execute(
                "SELECT DISTINCT ss.subject_code FROM students s"
                " JOIN student_subjects ss ON ss.student_id = s.student_id"
                " WHERE s.centre_number = ?",
                (cn,)
            ).fetchall()
            school_total_scopes = 0
            for sp in school_scope_pairs:
                subj = conn.execute(
                    "SELECT total_theory2, total_practical FROM subjects WHERE subject_code=?",
                    (sp["subject_code"],)).fetchone()
                school_total_scopes += 1 + (1 if subj and subj["total_theory2"] else 0) + (1 if subj and subj["total_practical"] else 0)

            # Finalized scopes for this school
            school_fin = conn.execute(
                "SELECT COUNT(*) c FROM finalized_scopes WHERE scope_key LIKE ?",
                (cn + "|%",)
            ).fetchone()["c"]

            # Marks entered for this school
            school_marks = conn.execute(
                "SELECT COUNT(*) c FROM total_marks tm"
                " JOIN students s ON s.student_id = tm.student_id"
                " WHERE s.centre_number = ?",
                (cn,)
            ).fetchone()["c"]
            # Students in this school
            school_students = conn.execute(
                "SELECT COUNT(*) c FROM students WHERE centre_number = ?",
                (cn,)
            ).fetchone()["c"]
            # Students whose attendance is present (any paper)
            school_present = conn.execute(
                "SELECT COUNT(DISTINCT student_id) c FROM attendance"
                " WHERE student_id IN (SELECT student_id FROM students WHERE centre_number = ?)"
                " AND is_present = 1",
                (cn,)
            ).fetchone()["c"]

            per_school.append({
                "centre_number": cn,
                "name": school["name"],
                "students": school_students,
                "total_scopes": school_total_scopes,
                "finalized_scopes": school_fin,
                "marks_entered": school_marks,
            })

        return {
            "pending_events": outbox.pending_count(conn),
            "rejected_events": outbox.rejected_count(conn),
            "finalized_scopes": fin_count,
            "total_scopes": total_scopes,
            "students": conn.execute("SELECT COUNT(*) c FROM students").fetchone()["c"],
            "total_marks": conn.execute("SELECT COUNT(*) c FROM total_marks").fetchone()["c"],
            "item_marks": conn.execute("SELECT COUNT(*) c FROM item_marks").fetchone()["c"],
            "marks_today": marks_today_total,
            "per_school": per_school,
        }

    # ---------------- sync (push outbox to Central) ----------------

    def _sync_once() -> dict:
        conn = connect(cfg.db_path)
        try:
            from .sync_http import run_http_sync
            return run_http_sync(conn)
        finally:
            conn.close()

    def _fire_sync() -> None:
        """Best-effort background sync (never blocks the caller)."""
        import threading
        threading.Thread(target=_sync_once, daemon=True).start()

    @app.get("/api/sync/config", tags=["sync"])
    def sync_config_get(conn=Depends(db), a=Depends(actor)):
        from .sync_http import get_sync_config
        return get_sync_config(conn)

    @app.post("/api/sync/config", tags=["sync"])
    def sync_config_set(payload: SyncConfigIn, conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may change sync settings")
        from .sync_http import set_sync_config
        return set_sync_config(conn, central_url=payload.central_url)

    @app.post("/api/sync/run", tags=["sync"])
    def sync_run(conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may run a sync")
        import logging
        log = logging.getLogger("station.sync")
        log.setLevel(logging.DEBUG)

        from .sync_http import _meta_get
        from . import machine_credential as mc_mod

        central_url  = _meta_get(conn, "central_url")
        station_code = _meta_get(conn, "station_code")
        exam_id      = _meta_get(conn, "exam_id")
        log.warning("[SYNC-DEBUG] central_url=%r  station_code=%r  exam_id=%r",
                    central_url, station_code, exam_id)

        cred = None
        if station_code and exam_id:
            cred = mc_mod.load(station_code, exam_id)
        log.warning("[SYNC-DEBUG] credential loaded=%r  credential_id=%r",
                    cred is not None, cred.get("credential_id") if cred else None)

        from .sync_http import run_http_sync
        result = run_http_sync(conn)
        log.warning("[SYNC-DEBUG] run_http_sync result=%r", result)
        return result

    # ---------------- admin: user management ----------------

    @app.get("/api/admin/users", tags=["admin"])
    def list_users(conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may view users")
        rows = conn.execute(
            "SELECT id, assignment_id, role, initials, admin_username, active,"
            " first_name, middle_name, surname, phone FROM station_users ORDER BY id"
        ).fetchall()
        out = []
        for row in rows:
            scopes = conn.execute(
                "SELECT centre_number, subject_code FROM user_scopes WHERE assignment_id = ?",
                (row["assignment_id"],)
            ).fetchall()
            # Build full_name if name fields are present
            full_name = None
            if row["first_name"] or row["surname"]:
                parts = [row["first_name"] or "", row["middle_name"] or "", row["surname"] or ""]
                full_name = " ".join(p for p in parts if p).upper()
            out.append({
                "id": row["id"],
                "assignment_id": row["assignment_id"],
                "role": row["role"],
                "initials": row["initials"],
                "admin_username": row["admin_username"],
                "active": bool(row["active"]),
                "first_name": row["first_name"],
                "middle_name": row["middle_name"],
                "surname": row["surname"],
                "phone": row["phone"],
                "full_name": full_name,
                "scopes": [{"centre_number": s["centre_number"], "subject_code": s["subject_code"]} for s in scopes],
            })
        return out

    @app.post("/api/admin/users", tags=["admin"])
    def create_user(payload: CreateUserIn, conn=Depends(db), a=Depends(actor)):
        """Create a local Data Enterer with initials + PIN, optionally assign scopes."""
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may create users")
        initials = (payload.initials or "").strip().upper()
        pin = (payload.pin or "").strip()
        if not initials:
            raise HTTPException(422, "initials is required")
        if not pin or len(pin) < 4:
            raise HTTPException(422, "PIN must be at least 4 characters")

        # Check for duplicate initials among active DEs
        existing = conn.execute(
            "SELECT id FROM station_users WHERE initials = ? AND role = 'DATA_ENTERER' AND active = 1",
            (initials,)
        ).fetchone()
        if existing:
            raise HTTPException(409, f"A Data Enterer with initials '{initials}' already exists")

        from .auth import _ph as _argon2_ph

        pin_hash = _argon2_ph.hash(pin)

        first_name = (payload.first_name or "").strip().upper() or None
        middle_name = (payload.middle_name or "").strip().upper() or None
        surname = (payload.surname or "").strip().upper() or None
        phone = (payload.phone or "").strip() or None

        # Assign a synthetic assignment_id above a high baseline to avoid
        # colliding with package-seeded credentials (which come from Central).
        max_aid = conn.execute("SELECT MAX(assignment_id) m FROM station_users").fetchone()["m"] or 0
        new_aid = max(max_aid + 1, 900_000_001)

        conn.execute(
            "INSERT INTO station_users(assignment_id, role, pin_hash, initials, active,"
            " first_name, middle_name, surname, phone)"
            " VALUES(?,?,?,?,1,?,?,?,?)",
            (new_aid, "DATA_ENTERER", pin_hash, initials,
             first_name, middle_name, surname, phone),
        )
        user_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]

        # Assign scopes
        for cn in (payload.centre_numbers or []):
            conn.execute(
                "INSERT OR IGNORE INTO user_scopes(assignment_id, centre_number, subject_code) VALUES(?,?,NULL)",
                (new_aid, cn)
            )
        for sc in (payload.subject_codes or []):
            conn.execute(
                "INSERT OR IGNORE INTO user_scopes(assignment_id, centre_number, subject_code) VALUES(?,NULL,?)",
                (new_aid, sc)
            )
        conn.commit()
        full_name_parts = [p for p in [first_name, middle_name, surname] if p]
        full_name = " ".join(full_name_parts) if full_name_parts else None
        return {"id": user_id, "assignment_id": new_aid, "initials": initials, "role": "DATA_ENTERER",
                "first_name": first_name, "middle_name": middle_name, "surname": surname,
                "phone": phone, "full_name": full_name}

    @app.delete("/api/admin/users/{user_id}", tags=["admin"])
    def deactivate_user(user_id: int, conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may deactivate users")
        row = conn.execute("SELECT role FROM station_users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "User not found")
        if row["role"] == "EXAM_ADMIN":
            raise HTTPException(403, "Cannot deactivate admin accounts")
        conn.execute("UPDATE station_users SET active = 0 WHERE id = ?", (user_id,))
        conn.commit()
        return {"deactivated": True}

    # ---------------- schools + detailed progress ----------------

    @app.get("/api/schools", tags=["progress"])
    def schools(conn=Depends(db), a=Depends(actor)):
        """Per-school progress: scopes, marks, attendance counts."""
        schools_rows = conn.execute(
            "SELECT centre_number, name FROM schools ORDER BY centre_number"
        ).fetchall()
        out = []
        for school in schools_rows:
            cn = school["centre_number"]
            # Subjects assigned to this school
            subject_rows = conn.execute(
                "SELECT DISTINCT ss.subject_code FROM students s"
                " JOIN student_subjects ss ON ss.student_id = s.student_id"
                " WHERE s.centre_number = ?", (cn,)
            ).fetchall()

            total_scopes = 0
            finalized_scopes = 0
            scope_details = []
            for sr in subject_rows:
                sc = sr["subject_code"]
                subj = conn.execute(
                    "SELECT name, total_theory2, total_practical FROM subjects WHERE subject_code=?",
                    (sc,)).fetchone()
                papers = ["THEORY1"]
                if subj and subj["total_theory2"]: papers.append("THEORY2")
                if subj and subj["total_practical"]: papers.append("PRACTICAL")

                for paper in papers:
                    total_scopes += 1
                    key = f"{cn}|{sc}|{paper}"
                    fin = conn.execute(
                        "SELECT 1 FROM finalized_scopes WHERE scope_key=?", (key,)
                    ).fetchone()
                    lock = conn.execute(
                        "SELECT owner, status FROM work_locks WHERE scope_key=?", (key,)
                    ).fetchone()
                    st_total = conn.execute(
                        "SELECT COUNT(*) c FROM students s"
                        " JOIN student_subjects ss ON ss.student_id=s.student_id"
                        " WHERE s.centre_number=? AND ss.subject_code=?",
                        (cn, sc)
                    ).fetchone()["c"]
                    marks_done = conn.execute(
                        "SELECT COUNT(*) c FROM total_marks tm"
                        " JOIN students s ON s.student_id=tm.student_id"
                        " WHERE s.centre_number=? AND tm.subject_code=? AND tm.paper_type=?",
                        (cn, sc, paper)
                    ).fetchone()["c"]
                    att_present = conn.execute(
                        "SELECT COUNT(*) c FROM attendance a"
                        " JOIN students s ON s.student_id=a.student_id"
                        " WHERE s.centre_number=? AND a.subject_code=? AND a.paper_type=? AND a.is_present=1",
                        (cn, sc, paper)
                    ).fetchone()["c"]
                    att_absent = conn.execute(
                        "SELECT COUNT(*) c FROM attendance a"
                        " JOIN students s ON s.student_id=a.student_id"
                        " WHERE s.centre_number=? AND a.subject_code=? AND a.paper_type=? AND a.is_present=0",
                        (cn, sc, paper)
                    ).fetchone()["c"]
                    if fin: finalized_scopes += 1
                    scope_details.append({
                        "subject_code": sc,
                        "subject_name": subj["name"] if subj else sc,
                        "paper_type": paper,
                        "finalized": fin is not None,
                        "lock_status": lock["status"] if lock else None,
                        "students": st_total,
                        "marks_entered": marks_done,
                        "att_present": att_present,
                        "att_absent": att_absent,
                    })

            students_total = conn.execute(
                "SELECT COUNT(*) c FROM students WHERE centre_number=?", (cn,)
            ).fetchone()["c"]

            out.append({
                "centre_number": cn,
                "name": school["name"],
                "students": students_total,
                "total_scopes": total_scopes,
                "finalized_scopes": finalized_scopes,
                "scopes": scope_details,
            })
        return out

    @app.get("/api/admin/progress/detail", tags=["admin"])
    def progress_detail(conn=Depends(db), a=Depends(actor)):
        """Per-DE progress: how many marks/attendance they've entered, which scopes."""
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Admin only")
        import datetime as _dt
        today_prefix = _dt.date.today().isoformat()

        users = conn.execute(
            "SELECT id, assignment_id, role, initials, admin_username, active"
            " FROM station_users ORDER BY role, initials"
        ).fetchall()

        out = []
        for u in users:
            aid = u["assignment_id"]
            # Total marks entered by this user
            total_marks = conn.execute(
                "SELECT COUNT(*) c FROM total_marks WHERE entered_by=?", (aid,)
            ).fetchone()["c"]
            marks_today = conn.execute(
                "SELECT COUNT(*) c FROM total_marks WHERE entered_by=? AND entered_at LIKE ?",
                (aid, today_prefix + "%")
            ).fetchone()["c"]
            # Total attendance transcriptions
            att_count = conn.execute(
                "SELECT COUNT(*) c FROM attendance WHERE transcribed_by=?", (aid,)
            ).fetchone()["c"]
            # Distinct scopes worked on
            worked_scopes = conn.execute(
                "SELECT DISTINCT tm.subject_code, tm.paper_type,"
                " s.centre_number"
                " FROM total_marks tm JOIN students s ON s.student_id=tm.student_id"
                " WHERE tm.entered_by=?", (aid,)
            ).fetchall()
            # Last activity time
            last_act = conn.execute(
                "SELECT MAX(entered_at) t FROM total_marks WHERE entered_by=?", (aid,)
            ).fetchone()["t"]
            last_att = conn.execute(
                "SELECT MAX(transcribed_at) t FROM attendance WHERE transcribed_by=?", (aid,)
            ).fetchone()["t"]
            last_active = max(filter(None, [last_act, last_att]), default=None)
            # Scope assignments
            scope_assignments = conn.execute(
                "SELECT centre_number, subject_code FROM user_scopes WHERE assignment_id=?", (aid,)
            ).fetchall()

            out.append({
                "id": u["id"],
                "assignment_id": aid,
                "role": u["role"],
                "name": u["initials"] or u["admin_username"] or f"user_{aid}",
                "active": bool(u["active"]),
                "marks_entered": total_marks,
                "marks_today": marks_today,
                "attendance_entered": att_count,
                "scopes_worked": [
                    {"centre_number": r["centre_number"],
                     "subject_code": r["subject_code"],
                     "paper_type": r["paper_type"]}
                    for r in worked_scopes
                ],
                "last_active": last_active,
                "assignments": [
                    {"centre_number": r["centre_number"], "subject_code": r["subject_code"]}
                    for r in scope_assignments
                ],
            })
        return out

    import os as _os
    _autosync_secs = int((_os.environ.get("STATION_AUTOSYNC_SECONDS") or "0") or "0")

    @app.on_event("startup")
    async def _sync_startup():
        if _autosync_secs > 0:
            import asyncio

            async def _loop():
                while True:
                    try:
                        await asyncio.to_thread(_sync_once)
                    except Exception:
                        pass
                    await asyncio.sleep(_autosync_secs)

            app.state._autosync_task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _sync_shutdown():
        task = getattr(app.state, "_autosync_task", None)
        if task:
            task.cancel()

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(_STATIC / "index.html")

    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    return app


app = create_app()
