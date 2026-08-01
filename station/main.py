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

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
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
    sync_key: str | None = None


def create_app(config: StationConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    # Prepare DB + schema at startup (safe to call repeatedly).
    conn = connect(cfg.db_path)
    apply_migrations(conn)
    conn.close()

    # Auto-import any package(s) shipped inside station_data/import/ (this is
    # what makes a freshly-downloaded Complete Station Bundle run ready).
    from .auto_import import auto_import_pending

    for r in auto_import_pending(cfg):
        print(f"[auto-import] {r.get('file')}: {r.get('status')}"
              + (f" ({r.get('students')} students)" if r.get("status") == "imported" else "")
              + (f" — {r.get('code')}: {r.get('message')}" if r.get("status") in {"rejected", "error"} else ""))

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
    def do_import(bundle: dict, conn=Depends(db)):
        try:
            result = import_package(conn, bundle)
        except PackageImportError as exc:
            return JSONResponse(status_code=422, content={"error": {"code": exc.code, "message": exc.message}})
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
        user = authenticate_admin(conn, payload.password)
        if user is None:
            raise HTTPException(401, "Invalid password")
        token = sessions.issue(user["id"], "EXAM_ADMIN")
        response.set_cookie(cfg.session_cookie, token, httponly=True, samesite="lax",
                            max_age=cfg.session_ttl_seconds)
        return {"role": "EXAM_ADMIN", "assignment_id": user["assignment_id"]}

    @app.post("/api/logout", tags=["auth"])
    def logout(response: Response):
        response.delete_cookie(cfg.session_cookie)
        return {"ok": True}

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
        rows = conn.execute(
            "SELECT DISTINCT s.centre_number, ss.subject_code FROM students s"
            " JOIN student_subjects ss ON ss.student_id = s.student_id"
        ).fetchall()
        out = []
        for r in rows:
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

    @app.put("/api/marks/students/{student_id}", tags=["entry"])
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
        return {
            "pending_events": outbox.pending_count(conn),
            "rejected_events": outbox.rejected_count(conn),
            "finalized_scopes": conn.execute("SELECT COUNT(*) c FROM finalized_scopes").fetchone()["c"],
            "students": conn.execute("SELECT COUNT(*) c FROM students").fetchone()["c"],
            "total_marks": conn.execute("SELECT COUNT(*) c FROM total_marks").fetchone()["c"],
            "item_marks": conn.execute("SELECT COUNT(*) c FROM item_marks").fetchone()["c"],
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
        return set_sync_config(conn, central_url=payload.central_url, sync_key=payload.sync_key)

    @app.post("/api/sync/run", tags=["sync"])
    def sync_run(conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may run a sync")
        from .sync_http import run_http_sync
        return run_http_sync(conn)

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
