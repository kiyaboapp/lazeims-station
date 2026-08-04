"""Station FastAPI app.

Serves a fully-local static shell (no CDN) and a small local API:
    GET  /                 -> offline UI shell
    GET  /health           -> liveness
    POST /api/import       -> import a scope-only package bundle
    POST /api/login/de     -> Data Enterer login (PIN + initials)
    POST /api/login/admin  -> Station Exam Admin login (password)
    GET  /api/me           -> current local session identity
    GET  /api/status       -> imported package summary

Multi-station design (one computer, many station identities):

  Config is resolved *live* on every request via
  :func:`station.config.resolve_active_cfg`. The active station is recorded in
  ``stations/.active`` and can be switched at runtime without restarting the
  server. Each station has its own SQLite DB, its own users, its own session
  HMAC secret — so a cookie signed by station A cannot be replayed on
  station B, even on the same computer.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import SOFTWARE_VERSION, paths
from . import entry as entry_svc
from . import finalize as finalize_svc
from . import locking, outbox
from . import package_import
from .auth import (
    SessionManager,
    authenticate_admin,
    authenticate_de,
    de_scopes_for,
    is_scope_allowed,
)
from .capabilities import capabilities_for
from .config import (
    StationConfig,
    invalidate_active_cfg,
    list_available_stations as _live_list_stations,
    load_config,
    resolve_active_cfg,
    set_active_station,
    read_active_station,
)
from .db import connect
from .migrations import PackageImportError, apply_migrations, import_package

from lazeims_common.enums import FillingMode, PaperType
from lazeims_common.errors import ValidationError

# Cookie name is stable across stations (only its signed value varies).
SESSION_COOKIE = "lazeims_station_session"

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


class StationSwitchIn(BaseModel):
    station_code: str
    exam_id: str


def create_app(config: StationConfig | None = None) -> FastAPI:
    # ── Runtime mode ─────────────────────────────────────────────────────────
    #
    #   • fixed_cfg  → tests inject a StationConfig. Single-station forever.
    #   • home_root  → production. Active station is resolved live per request
    #                  from ``stations/.active`` and can be switched at runtime.
    #
    fixed_cfg: StationConfig | None = config
    home_root: Path | None = None if fixed_cfg else paths.lazeims_home()

    def _live_cfg() -> StationConfig:
        return fixed_cfg if fixed_cfg is not None else resolve_active_cfg(home_root)

    # Per-station SessionManager cache. Keyed by (station_code, exam_id) so
    # a cookie signed with station A's secret cannot possibly validate as a
    # session for station B — even on the same computer.
    _sm_cache: dict[tuple[str, str], SessionManager] = {}
    _sm_secrets: dict[tuple[str, str], str] = {}
    _sm_lock = threading.Lock()

    def _sessions_for(cfg: StationConfig) -> SessionManager:
        key = (cfg.station_code or "", cfg.exam_id or "")
        with _sm_lock:
            sm = _sm_cache.get(key)
            if sm is None or _sm_secrets.get(key) != cfg.secret_key:
                sm = SessionManager(cfg.secret_key, cfg.session_ttl_seconds)
                _sm_cache[key] = sm
                _sm_secrets[key] = cfg.secret_key
            return sm

    # ── One-time boot ────────────────────────────────────────────────────────
    boot_cfg = _live_cfg()
    if boot_cfg.station_code or fixed_cfg is not None:
        _c = connect(boot_cfg.db_path)
        try:
            apply_migrations(_c)
        finally:
            _c.close()

    # Auto-import any packages pre-staged in imports/pending. In production
    # (multi-station) mode this discovers pending zips under every known
    # station directory so a fresh install with a bundle drops in works
    # automatically. In test mode we only touch the injected cfg.
    from .auto_import import auto_import_pending

    if fixed_cfg is None:
        import_results = auto_import_pending(boot_cfg)
        for r in import_results:
            print(f"[auto-import] {r.get('file')}: {r.get('status')}"
                  + (f" ({r.get('students')} students)" if r.get("status") == "imported" else "")
                  + (f" — {r.get('code')}: {r.get('message')}" if r.get("status") in {"rejected", "error"} else ""))

        # If we started with no active station but the import created a new
        # per-exam DB, invalidate the cache and re-migrate at the correct path.
        if any(r.get("status") == "imported" for r in import_results):
            invalidate_active_cfg(home_root)
            boot_cfg = _live_cfg()
            if boot_cfg.station_code:
                _c = connect(boot_cfg.db_path)
                try:
                    apply_migrations(_c)
                finally:
                    _c.close()
                print(f"[auto-import] active station now: station={boot_cfg.station_code} exam={boot_cfg.exam_id}")

    # Seed a default Central URL from the bundle so sync works out of the box.
    try:
        import json as _json
        if boot_cfg.station_code or fixed_cfg is not None:
            sync_file = boot_cfg.data_dir / "sync.json"
            if sync_file.is_file():
                url = (_json.loads(sync_file.read_text() or "{}") or {}).get("central_url")
                if url:
                    from .sync_http import seed_central_url_if_unset
                    _c = connect(boot_cfg.db_path)
                    try:
                        seed_central_url_if_unset(_c, url)
                    finally:
                        _c.close()
    except Exception as exc:  # never block boot on this
        print(f"[sync] could not seed central_url: {exc}")

    # ── FastAPI app ──────────────────────────────────────────────────────────
    app = FastAPI(title="LAZEIMS Station", version=SOFTWARE_VERSION)
    app.state.home = home_root
    app.state.fixed_cfg = fixed_cfg
    app.state.live_cfg = _live_cfg
    app.state.sessions_for = _sessions_for

    # ── Dependencies (all evaluate cfg LIVE per request) ─────────────────────

    def cfg_dep() -> StationConfig:
        return _live_cfg()

    def db(cfg: StationConfig = Depends(cfg_dep)):
        # In production mode, refuse DB access when no station is chosen.
        if not cfg.station_code and fixed_cfg is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "NO_ACTIVE_STATION",
                        "message": "No station is selected on this device."},
            )
        c = connect(cfg.db_path)
        try:
            yield c
        finally:
            c.close()

    def current(session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
                cfg: StationConfig = Depends(cfg_dep)):
        if not cfg.station_code and fixed_cfg is None:
            raise HTTPException(401, "No station selected")
        sm = _sessions_for(cfg)
        data = sm.resolve(session or "")
        if data is None:
            raise HTTPException(401, "Not authenticated")
        # Belt-and-braces: session must belong to the currently-active station.
        # (Cookies are signed with that station's secret, so a mismatched
        # signature would already have failed above. This guards against edge
        # cases like a cookie that was minted before the station was renamed.)
        if fixed_cfg is None:
            if data.get("sc") != cfg.station_code or data.get("xi") != cfg.exam_id:
                raise HTTPException(401, "Session belongs to a different station")
        return data

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok", "software_version": SOFTWARE_VERSION}

    @app.get("/api/status", tags=["ops"])
    def status(cfg: StationConfig = Depends(cfg_dep)):
        """Public status probe. Works even before a station is selected."""
        if not cfg.station_code and fixed_cfg is None:
            return {
                "station_code": None,
                "exam_id": None,
                "packages": 0,
                "students": 0,
                "software_version": SOFTWARE_VERSION,
            }
        conn = connect(cfg.db_path)
        try:
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
        finally:
            conn.close()

    # ── Station switcher (no auth) ────────────────────────────────────────────

    @app.get("/api/stations/available", tags=["setup"])
    def list_available_stations():
        """Scan disk for every station+exam pair present on this computer.

        No auth required — this is the pre-login chooser. Returns the current
        active pair (if any) so the UI can highlight it.
        """
        if fixed_cfg is not None:
            # Single-station mode (tests): no chooser.
            return {"stations": [], "active": None}
        stations = _live_list_stations(home_root)
        active = read_active_station(home_root)
        return {"stations": stations, "active": active}

    @app.post("/api/stations/switch", tags=["setup"])
    def switch_station(payload: StationSwitchIn, response: Response):
        """Select a different active station on this computer.

        Writes ``stations/.active`` atomically and clears the current session
        cookie so the user is forced to log in against the newly-selected
        station's local users. No process restart, no session survives across
        stations — cookies are per-station HMAC-signed.
        """
        if fixed_cfg is not None:
            raise HTTPException(400, "Station switching is disabled in single-station mode")
        try:
            set_active_station(payload.station_code, payload.exam_id, home=home_root)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

        # Make sure the new station's DB is migrated (may have been imported
        # by a different process or on a first boot).
        new_cfg = _live_cfg()
        _c = connect(new_cfg.db_path)
        try:
            apply_migrations(_c)
        finally:
            _c.close()

        # Drop any lingering session cookie — the user must log in against the
        # newly-selected station's local users.
        response.delete_cookie(SESSION_COOKIE)
        return {
            "switched": True,
            "station_code": new_cfg.station_code,
            "exam_id": new_cfg.exam_id,
        }

    @app.post("/api/stations/refresh-imports", tags=["setup"])
    def refresh_imports():
        """Re-scan ``imports/pending`` for every known station and import.

        Runs the same auto-import pass as boot. Useful after copying a new
        exam package into the station folder while the server is running.
        """
        if fixed_cfg is not None:
            raise HTTPException(400, "Import refresh is not applicable in single-station mode")
        from .auto_import import auto_import_pending
        # Kick off with the currently-resolved cfg (may be None-station).
        results = auto_import_pending(_live_cfg())
        # A fresh identity may have been adopted — force a reload.
        invalidate_active_cfg(home_root)
        return {"results": results, "active": read_active_station(home_root)}

    @app.post("/api/import", tags=["setup"])
    async def do_import(request: Request, conn=Depends(db)):
        """Import a signed exam package.

        Accepts either:
        - a JSON bundle body (Content-Type: application/json), used by tests
          and the internal auto-importer, or
        - a multipart/form-data file upload with a ``file`` field holding the
          signed ZIP produced by Central.

        The dispatch is by Content-Type so a single URL serves both.
        """
        ctype = (request.headers.get("content-type") or "").lower()
        try:
            if "application/json" in ctype:
                request_body = await request.json()
                result = package_import.import_bundle(conn, request_body)
            elif "multipart/form-data" in ctype:
                form = await request.form()
                upload = form.get("file")
                if upload is None or not hasattr(upload, "read"):
                    raise HTTPException(422, "Missing 'file' field in multipart body")
                data = await upload.read()
                result = package_import.import_signed_zip(conn, data)
            else:
                raise HTTPException(
                    422, "Provide application/json body or multipart/form-data file")
        except PackageImportError as exc:
            return JSONResponse(status_code=422,
                                content={"error": {"code": exc.code, "message": exc.message}})
        return {"imported": True, **result}

    @app.post("/api/login/de", tags=["auth"])
    def login_de(payload: DeLogin, response: Response,
                 conn=Depends(db), cfg: StationConfig = Depends(cfg_dep)):
        user = authenticate_de(conn, payload.pin, payload.initials)
        if user is None:
            raise HTTPException(401, "Invalid PIN or initials")
        sm = _sessions_for(cfg)
        token = sm.issue(user["id"], "DATA_ENTERER",
                         station_code=cfg.station_code, exam_id=cfg.exam_id)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                            max_age=cfg.session_ttl_seconds)
        return {
            "role": "DATA_ENTERER",
            "initials": user["initials"],
            "assignment_id": user["assignment_id"],
            "station_code": cfg.station_code,
            "exam_id": cfg.exam_id,
            "capabilities": capabilities_for("DATA_ENTERER"),
        }

    @app.post("/api/login/admin", tags=["auth"])
    def login_admin(payload: AdminLogin, response: Response,
                    conn=Depends(db), cfg: StationConfig = Depends(cfg_dep)):
        user = authenticate_admin(conn, payload.username, payload.password)
        if user is None:
            raise HTTPException(401, "Invalid username or password")
        sm = _sessions_for(cfg)
        token = sm.issue(user["id"], "EXAM_ADMIN",
                         station_code=cfg.station_code, exam_id=cfg.exam_id)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                            max_age=cfg.session_ttl_seconds)
        return {
            "role": "EXAM_ADMIN",
            "assignment_id": user["assignment_id"],
            "username": user.get("admin_username") or user.get("initials"),
            "station_code": cfg.station_code,
            "exam_id": cfg.exam_id,
            "capabilities": capabilities_for("EXAM_ADMIN"),
        }

    @app.post("/api/logout", tags=["auth"])
    def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.post("/api/admin/reset-password", tags=["auth"])
    def reset_admin_password(payload: dict,
                             conn=Depends(db), cfg: StationConfig = Depends(cfg_dep)):
        """Reset station admin password using the machine credential as proof.
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

        row = conn.execute(
            "SELECT admin_username, initials FROM station_users WHERE role = 'EXAM_ADMIN' AND active = 1 LIMIT 1"
        ).fetchone()
        username = (row["admin_username"] or row["initials"]) if row else "admin"
        return {"ok": True, "username": username}

    @app.get("/api/me", tags=["auth"])
    def me(session=Depends(current), cfg: StationConfig = Depends(cfg_dep)):
        role = session.get("role") or ""
        return {
            **session,
            "station_code": cfg.station_code,
            "exam_id": cfg.exam_id,
            "capabilities": capabilities_for(role),
        }

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
            subj = conn.execute("SELECT name, total_theory1, total_theory2, total_practical FROM subjects WHERE subject_code=?",
                                (r["subject_code"],)).fetchone()
            school = conn.execute("SELECT name FROM schools WHERE centre_number = ?", (r["centre_number"],)).fetchone()
            papers = ["THEORY1"] + (["THEORY2"] if subj and subj["total_theory2"] else []) + (["PRACTICAL"] if subj and subj["total_practical"] else [])
            for p in papers:
                key = locking.scope_key(r["centre_number"], r["subject_code"], p)
                lock = conn.execute("SELECT owner, status FROM work_locks WHERE scope_key=?", (key,)).fetchone()
                fin = conn.execute("SELECT 1 FROM finalized_scopes WHERE scope_key=?", (key,)).fetchone()
                out.append({
                    "centre_number": r["centre_number"],
                    "school_name": school["name"] if school else r["centre_number"],
                    "subject_code": r["subject_code"],
                    "subject_name": subj["name"] if subj else r["subject_code"],
                    "paper_type": p,
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
            "SELECT s.student_id, s.first_name, s.middle_name, s.surname FROM students s"
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
                "student_id": st["student_id"],
                "full_name": (
                    (st["first_name"] or "").strip().upper()
                    + (" " + (st["middle_name"] or "").strip().upper() if st["middle_name"] else "")
                    + " " + (st["surname"] or "").strip().upper()
                ).strip(),
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

    @app.get("/api/scopes/report", tags=["entry"])
    def scope_report(centre_number: str, subject_code: str, paper_type: PaperType, conn=Depends(db), a=Depends(actor)):
        """Generate marks report data for printing."""
        from .entry import exam_id as get_exam_id, _attendance_rows
        from lazeims_common.validation.attendance import effective_attendance, AttendanceRow

        eid = get_exam_id(conn)
        exam_row = conn.execute("SELECT value FROM station_meta WHERE key='exam_name'").fetchone()
        exam_name = exam_row["value"] if exam_row else eid

        school_row = conn.execute("SELECT name FROM schools WHERE centre_number=?", (centre_number,)).fetchone()
        school_name = school_row["name"] if school_row else centre_number

        sub_row = conn.execute(
            "SELECT name, total_theory1, total_theory2, total_practical FROM subjects WHERE subject_code=?",
            (subject_code,)
        ).fetchone()
        subject_name = sub_row["name"] if sub_row else subject_code

        # Total possible marks for this paper — scoring config lives directly
        # on the subjects row (see migrations.py); there is no separate
        # exam_subjects table on the station.
        total_possible = 100
        if sub_row:
            if paper_type.value == "THEORY1":
                total_possible = sub_row["total_theory1"] or 100
            elif paper_type.value == "THEORY2":
                total_possible = sub_row["total_theory2"] or 100
            elif paper_type.value == "PRACTICAL":
                total_possible = sub_row["total_practical"] or 100

        # Questions
        q_rows = conn.execute(
            "SELECT question_number, max_marks FROM questions WHERE subject_code=? AND paper_type=? ORDER BY question_number",
            (subject_code, paper_type.value)
        ).fetchall()
        questions = [{"number": str(q["question_number"]), "max_marks": float(q["max_marks"])} for q in q_rows]

        # Students — full_name is derived here (first/middle/surname are the
        # only stored columns; see AGENTS.md: never expose them separately).
        students_rows = conn.execute(
            "SELECT s.student_id, s.first_name, s.middle_name, s.surname"
            " FROM students s JOIN student_subjects ss ON ss.student_id = s.student_id"
            " WHERE ss.subject_code=? AND s.centre_number=? ORDER BY s.student_id",
            (subject_code, centre_number)
        ).fetchall()

        out = []
        for st in students_rows:
            sid = st["student_id"]
            full_name = (
                (st["first_name"] or "").strip().upper()
                + (" " + (st["middle_name"] or "").strip().upper() if st["middle_name"] else "")
                + " " + (st["surname"] or "").strip().upper()
            ).strip()
            # Attendance
            att_rows = _attendance_rows(conn, sid, subject_code)
            is_present = effective_attendance(att_rows, paper_type)

            # Total marks
            tm = conn.execute(
                "SELECT total_marks_obtained FROM total_marks WHERE student_id=? AND subject_code=? AND paper_type=?",
                (sid, subject_code, paper_type.value)
            ).fetchone()

            # Item marks
            item_marks = None
            if q_rows:
                im_rows = conn.execute(
                    "SELECT q.question_number, im.marks_obtained FROM item_marks im"
                    " JOIN questions q ON q.id = im.question_id"
                    " WHERE im.student_id=? AND q.subject_code=? AND q.paper_type=?",
                    (sid, subject_code, paper_type.value)
                ).fetchall()
                item_marks = {str(r["question_number"]): float(r["marks_obtained"]) if r["marks_obtained"] is not None else None for r in im_rows}

            out.append({
                "student_id": sid,
                "full_name": full_name,
                "attendance": is_present,
                "total_marks": float(tm["total_marks_obtained"]) if tm else None,
                "item_marks": item_marks,
            })

        return {
            "exam_name": exam_name,
            "school_name": school_name,
            "centre_number": centre_number,
            "subject_code": subject_code,
            "subject_name": subject_name,
            "paper_type": paper_type.value,
            "total_possible": total_possible,
            "enterer_initials": None,
            "finalized_at": None,
            "questions": questions if questions else None,
            "students": out,
        }

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
    def progress(user_id: int | None = None, conn=Depends(db), a=Depends(actor)):
        import datetime as _dt
        today_prefix = _dt.date.today().isoformat()  # "YYYY-MM-DD"

        # If user_id is provided, scope filtering to that user's assignments
        scope_filter = None
        if user_id is not None:
            user_row = conn.execute(
                "SELECT assignment_id FROM station_users WHERE id=?", (user_id,)
            ).fetchone()
            if user_row:
                scope_filter = de_scopes_for(conn, user_row["assignment_id"])

        # Total scopes (distinct centre+subject+paper combinations)
        all_scopes = conn.execute(
            "SELECT DISTINCT s.centre_number, ss.subject_code FROM students s"
            " JOIN student_subjects ss ON ss.student_id = s.student_id"
        ).fetchall()
        subj_map = {}
        for r in all_scopes:
            # Apply scope filter if present
            if scope_filter is not None and not is_scope_allowed(
                scope_filter, centre_number=r["centre_number"], subject_code=r["subject_code"]
            ):
                continue
            subj = conn.execute(
                "SELECT total_theory2, total_practical FROM subjects WHERE subject_code=?",
                (r["subject_code"],)).fetchone()
            papers = 1 + (1 if subj and subj["total_theory2"] else 0) + (1 if subj and subj["total_practical"] else 0)
            subj_map[(r["centre_number"], r["subject_code"])] = papers
        total_scopes = sum(subj_map.values())

        # Determine which centres are in scope for filtering
        scoped_centres = None
        if scope_filter is not None:
            scoped_centres = set(cn for cn, _ in subj_map.keys())

        fin_count = 0
        if scope_filter is None:
            fin_count = conn.execute("SELECT COUNT(*) c FROM finalized_scopes").fetchone()["c"]
        else:
            # Count finalized scopes only for the in-scope centre+subject+paper combos
            for (cn, sc), papers_count in subj_map.items():
                subj = conn.execute(
                    "SELECT total_theory2, total_practical FROM subjects WHERE subject_code=?",
                    (sc,)).fetchone()
                paper_list = ["THEORY1"]
                if subj and subj["total_theory2"]:
                    paper_list.append("THEORY2")
                if subj and subj["total_practical"]:
                    paper_list.append("PRACTICAL")
                for p in paper_list:
                    key = f"{cn}|{sc}|{p}"
                    fin = conn.execute(
                        "SELECT 1 FROM finalized_scopes WHERE scope_key=?", (key,)
                    ).fetchone()
                    if fin:
                        fin_count += 1

        # Today's marks (total_marks entered today)
        if scope_filter is None:
            marks_today = conn.execute(
                "SELECT COUNT(*) c FROM total_marks WHERE entered_at LIKE ?",
                (today_prefix + "%",)
            ).fetchone()["c"]
            item_today = conn.execute(
                "SELECT COUNT(DISTINCT im.student_id || '|' || q.subject_code || '|' || q.paper_type) c"
                " FROM item_marks im JOIN questions q ON q.id = im.question_id"
                " WHERE im.entered_at LIKE ?",
                (today_prefix + "%",)
            ).fetchone()["c"]
        else:
            # Filter marks to scoped students
            marks_today = conn.execute(
                "SELECT COUNT(*) c FROM total_marks tm"
                " JOIN students s ON s.student_id=tm.student_id"
                " WHERE tm.entered_at LIKE ?"
                " AND EXISTS (SELECT 1 FROM student_subjects ss WHERE ss.student_id=s.student_id"
                "   AND (? = '' OR s.centre_number IN (SELECT centre_number FROM user_scopes WHERE assignment_id=?))"
                "   AND (? = '' OR ss.subject_code IN (SELECT subject_code FROM user_scopes WHERE assignment_id=?)))",
                (today_prefix + "%",
                 "" if not scope_filter["centres"] else "x",
                 (user_row["assignment_id"] if user_row else 0),
                 "" if not scope_filter["subjects"] else "x",
                 (user_row["assignment_id"] if user_row else 0))
            ).fetchone()["c"]
            item_today = 0  # simplified for scoped view
        marks_today_total = marks_today + (item_today if scope_filter is None else 0)

        # Per-school breakdown
        schools = conn.execute("SELECT centre_number, name FROM schools ORDER BY centre_number").fetchall()
        per_school = []
        for school in schools:
            cn = school["centre_number"]
            # Skip schools not in scope
            if scoped_centres is not None and cn not in scoped_centres:
                continue
            # Count total scopes for this school
            school_scope_pairs = conn.execute(
                "SELECT DISTINCT ss.subject_code FROM students s"
                " JOIN student_subjects ss ON ss.student_id = s.student_id"
                " WHERE s.centre_number = ?",
                (cn,)
            ).fetchall()
            school_total_scopes = 0
            for sp in school_scope_pairs:
                if scope_filter is not None and not is_scope_allowed(
                    scope_filter, centre_number=cn, subject_code=sp["subject_code"]
                ):
                    continue
                subj = conn.execute(
                    "SELECT total_theory2, total_practical FROM subjects WHERE subject_code=?",
                    (sp["subject_code"],)).fetchone()
                school_total_scopes += 1 + (1 if subj and subj["total_theory2"] else 0) + (1 if subj and subj["total_practical"] else 0)

            if school_total_scopes == 0 and scope_filter is not None:
                continue

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

            per_school.append({
                "centre_number": cn,
                "name": school["name"],
                "students": school_students,
                "total_scopes": school_total_scopes,
                "finalized_scopes": school_fin,
                "marks_entered": school_marks,
            })

        # Total counts - apply scope filter
        if scope_filter is None:
            total_students = conn.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
            total_marks_count = conn.execute("SELECT COUNT(*) c FROM total_marks").fetchone()["c"]
            total_item_marks = conn.execute("SELECT COUNT(*) c FROM item_marks").fetchone()["c"]
        else:
            total_students = sum(s["students"] for s in per_school)
            total_marks_count = sum(s["marks_entered"] for s in per_school)
            total_item_marks = 0  # simplified for scoped view

        return {
            "pending_events": outbox.pending_count(conn),
            "rejected_events": outbox.rejected_count(conn),
            "finalized_scopes": fin_count,
            "total_scopes": total_scopes,
            "students": total_students,
            "total_marks": total_marks_count,
            "item_marks": total_item_marks,
            "marks_today": marks_today_total,
            "per_school": per_school,
        }

    # ---------------- audit ----------------

    @app.get("/api/audit/marks", tags=["audit"])
    def audit_marks(
        student_id: str | None = None,
        subject_code: str | None = None,
        paper_type: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 100,
        conn=Depends(db), a=Depends(actor),
    ):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Admin only")
        if limit > 500:
            limit = 500

        query = "SELECT * FROM marks_audit WHERE 1=1"
        params = []
        if student_id:
            query += " AND student_id = ?"
            params.append(student_id)
        if subject_code:
            query += " AND subject_code = ?"
            params.append(subject_code)
        if paper_type:
            query += " AND paper_type = ?"
            params.append(paper_type)
        if from_date:
            query += " AND station_occurred_at >= ?"
            params.append(from_date)
        if to_date:
            query += " AND station_occurred_at <= ?"
            params.append(to_date + "T23:59:59")
        query += " ORDER BY station_occurred_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        # Resolve actor initials
        result = []
        for r in rows:
            actor_name = None
            if r["actor_assignment_id"]:
                u = conn.execute(
                    "SELECT initials FROM station_users WHERE assignment_id = ?",
                    (r["actor_assignment_id"],)
                ).fetchone()
                actor_name = u["initials"] if u else None
            result.append({
                "id": r["id"],
                "student_id": r["student_id"],
                "subject_code": r["subject_code"],
                "paper_type": r["paper_type"],
                "operation": r["operation"],
                "mode": r["mode"],
                "before_total": r["before_total"],
                "before_items": r["before_items"],
                "after_total": r["after_total"],
                "after_items": r["after_items"],
                "actor_initials": actor_name,
                "station_occurred_at": r["station_occurred_at"],
                "event_id": r["event_id"],
            })
        return result

    # ---------------- sync (push outbox to Central) ----------------

    def _sync_once() -> dict:
        # Sync is best-effort and runs in background; resolve cfg fresh so a
        # mid-session switch never sends a station's outbox to a different
        # station's DB.
        _cfg = _live_cfg()
        if not _cfg.station_code:
            return {"synced": False, "reason": "no active station"}
        conn = connect(_cfg.db_path)
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

    @app.get("/api/sync/rejected", tags=["sync"])
    def sync_rejected(conn=Depends(db), a=Depends(actor)):
        """List rejected outbox events with Central's rejection reason.

        The bare ``rejected_events`` count in /api/progress tells the admin
        SOMETHING failed but not why. This endpoint answers that: each entry
        carries the natural key (student/subject/paper) and the rejection
        code Central returned (e.g. ATTENDANCE_REQUIRED_FIRST, PHASE_NOT_OPEN,
        EVENT_ID_PAYLOAD_CONFLICT) so the admin knows what to fix before
        retrying.
        """
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Admin only")
        return outbox.list_rejected(conn)

    @app.post("/api/sync/retry-rejected", tags=["sync"])
    def sync_retry_rejected(conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may retry rejected events")
        from .db import transaction
        with transaction(conn):
            count = outbox.retry_rejected(conn)
        return {"queued": count}

    @app.post("/api/sync/pull-snapshot", tags=["sync"])
    def sync_pull_snapshot(conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may pull snapshots")
        from .sync_http import _meta_get
        from . import machine_credential as mc_mod
        import json as _json
        import urllib.request
        import urllib.error

        central_url = _meta_get(conn, "central_url")
        station_code = _meta_get(conn, "station_code")
        exam_id_val = _meta_get(conn, "exam_id")
        if not central_url or not station_code or not exam_id_val:
            return {"configured": False, "reason": "Central URL or station not configured"}

        cred = mc_mod.load(station_code, exam_id_val)
        if not cred:
            return {"configured": False, "reason": "No machine credential found"}

        url = central_url.rstrip("/") + f"/api/v1/station/sync/pull/snapshot?station_code={station_code}"
        req = urllib.request.Request(
            url, method="GET",
            headers={
                "X-Package-Credential-Id": cred["credential_id"],
                "X-Package-Secret": cred["secret"],
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return _json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise HTTPException(502, f"Central returned {exc.code}: {payload}")
        except urllib.error.URLError as exc:
            raise HTTPException(502, f"Cannot reach Central: {exc.reason}")

    @app.get("/api/sync/local-digests", tags=["sync"])
    def sync_local_digests(conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Admin only")
        from .sync import compute_reconciliation
        return compute_reconciliation(conn)

    @app.get("/api/sync/export-outbox", tags=["sync"])
    def sync_export_outbox(conn=Depends(db), a=Depends(actor)):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may export outbox")
        from .sync import export_pending_envelope
        from .sync_http import _meta_get
        from . import machine_credential as mc_mod

        station_code = _meta_get(conn, "station_code")
        exam_id_val = _meta_get(conn, "exam_id")
        if not station_code or not exam_id_val:
            return Response(status_code=204)

        cred = mc_mod.load(station_code, exam_id_val)
        if not cred:
            raise HTTPException(503, "No machine credential")

        token = export_pending_envelope(conn, key=cred["secret"])
        if token is None:
            return Response(status_code=204)

        import io, zipfile, json as _json
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("envelope.json", _json.dumps({
                "station_code": station_code,
                "token": token,
            }))
        buf.seek(0)
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="outbox_{station_code}.zip"'},
        )

    @app.post("/api/sync/import-ack", tags=["sync"])
    async def sync_import_ack(
        file: UploadFile = File(...),
        conn=Depends(db), a=Depends(actor),
    ):
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may import ack")
        from .sync import apply_ack_envelope
        from .sync_http import _meta_get
        from . import machine_credential as mc_mod
        import zipfile, io, json as _json

        station_code = _meta_get(conn, "station_code")
        exam_id_val = _meta_get(conn, "exam_id")
        if not station_code or not exam_id_val:
            raise HTTPException(422, "Station not configured")

        cred = mc_mod.load(station_code, exam_id_val)
        if not cred:
            raise HTTPException(503, "No machine credential")

        data = await file.read()
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                envelope = _json.loads(zf.read("envelope.json"))
        except Exception as exc:
            raise HTTPException(422, f"Invalid ack ZIP: {exc}")

        ack_token = envelope.get("ack_token") or envelope.get("token")
        if not ack_token:
            raise HTTPException(422, "No ack_token found in envelope")

        result = apply_ack_envelope(conn, key=cred["secret"], token=ack_token)
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

        # Validate that subject_codes exist for the given centre_numbers
        if payload.centre_numbers and payload.subject_codes:
            placeholders = ",".join("?" for _ in payload.centre_numbers)
            available_subjects = conn.execute(
                f"SELECT DISTINCT ss.subject_code FROM students s"
                f" JOIN student_subjects ss ON ss.student_id=s.student_id"
                f" WHERE s.centre_number IN ({placeholders})",
                payload.centre_numbers
            ).fetchall()
            available_set = {r["subject_code"] for r in available_subjects}
            invalid = [sc for sc in payload.subject_codes if sc not in available_set]
            if invalid:
                raise HTTPException(
                    422,
                    f"Subject codes {invalid} have no students at the given schools ({payload.centre_numbers})"
                )

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

    # ---------------- analytics ----------------

    @app.get("/api/analytics/dashboard", tags=["analytics"])
    def analytics_dashboard(conn=Depends(db), a=Depends(actor)):
        """Aggregated station analytics: marks per day, per-subject progress,
        completion timeline, and top enterers."""
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may view analytics")
        import datetime as _dt

        # marks_per_day: last 14 days
        today = _dt.date.today()
        marks_per_day = []
        for i in range(13, -1, -1):
            d = (today - _dt.timedelta(days=i)).isoformat()
            total_count = conn.execute(
                "SELECT COUNT(*) c FROM total_marks WHERE entered_at LIKE ?",
                (d + "%",)
            ).fetchone()["c"]
            item_count = conn.execute(
                "SELECT COUNT(DISTINCT im.student_id || '|' || q.subject_code || '|' || q.paper_type) c"
                " FROM item_marks im JOIN questions q ON q.id = im.question_id"
                " WHERE im.entered_at LIKE ?",
                (d + "%",)
            ).fetchone()["c"]
            marks_per_day.append({"date": d, "count": total_count + item_count})

        # per_subject_progress: per subject, total students and marks entered
        subjects_rows = conn.execute(
            "SELECT subject_code, name FROM subjects ORDER BY subject_code"
        ).fetchall()
        per_subject_progress = []
        for subj in subjects_rows:
            sc = subj["subject_code"]
            total_students = conn.execute(
                "SELECT COUNT(*) c FROM student_subjects WHERE subject_code=?", (sc,)
            ).fetchone()["c"]
            marks_entered = conn.execute(
                "SELECT COUNT(*) c FROM total_marks WHERE subject_code=?", (sc,)
            ).fetchone()["c"]
            per_subject_progress.append({
                "subject_code": sc,
                "subject_name": subj["name"],
                "total_students": total_students,
                "marks_entered": marks_entered,
            })

        # completion_timeline: cumulative finalized scopes per day
        fin_rows = conn.execute(
            "SELECT finalized_at FROM finalized_scopes WHERE finalized_at IS NOT NULL ORDER BY finalized_at"
        ).fetchall()
        timeline_map: dict[str, int] = {}
        for r in fin_rows:
            day = (r["finalized_at"] or "")[:10]
            if day:
                timeline_map[day] = timeline_map.get(day, 0) + 1
        cumulative = 0
        completion_timeline = []
        for day in sorted(timeline_map.keys()):
            cumulative += timeline_map[day]
            completion_timeline.append({"date": day, "cumulative_finalized": cumulative})

        # top_enterers: per DE, marks count and name
        de_rows = conn.execute(
            "SELECT assignment_id, initials, first_name, surname FROM station_users"
            " WHERE role='DATA_ENTERER' AND active=1"
        ).fetchall()
        top_enterers = []
        for de in de_rows:
            aid = de["assignment_id"]
            count = conn.execute(
                "SELECT COUNT(*) c FROM total_marks WHERE entered_by=?", (aid,)
            ).fetchone()["c"]
            name = de["initials"]
            if de["first_name"] or de["surname"]:
                parts = [de["first_name"] or "", de["surname"] or ""]
                name = " ".join(p for p in parts if p).strip() or de["initials"]
            top_enterers.append({"assignment_id": aid, "name": name, "marks_count": count})
        top_enterers.sort(key=lambda x: x["marks_count"], reverse=True)

        return {
            "marks_per_day": marks_per_day,
            "per_subject_progress": per_subject_progress,
            "completion_timeline": completion_timeline,
            "top_enterers": top_enterers,
        }

    @app.get("/api/analytics/subject-distribution", tags=["analytics"])
    def analytics_subject_distribution(conn=Depends(db), a=Depends(actor)):
        """Per-subject + paper marks distribution: min, max, mean, median."""
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may view analytics")
        import statistics as _stats

        rows = conn.execute(
            "SELECT tm.subject_code, tm.paper_type, tm.total_marks_obtained"
            " FROM total_marks tm ORDER BY tm.subject_code, tm.paper_type"
        ).fetchall()

        # Group by subject_code + paper_type
        groups: dict[tuple[str, str], list[float]] = {}
        for r in rows:
            key = (r["subject_code"], r["paper_type"])
            if key not in groups:
                groups[key] = []
            groups[key].append(float(r["total_marks_obtained"]))

        # Subject names lookup
        subj_names: dict[str, str] = {}
        for row in conn.execute("SELECT subject_code, name FROM subjects").fetchall():
            subj_names[row["subject_code"]] = row["name"]

        result = []
        for (sc, pt), marks in sorted(groups.items()):
            entry: dict = {
                "subject_code": sc,
                "subject_name": subj_names.get(sc, sc),
                "paper_type": pt,
                "count": len(marks),
                "min": min(marks),
                "max": max(marks),
                "mean": round(_stats.mean(marks), 2),
                "median": round(_stats.median(marks), 2),
            }
            if len(marks) >= 4:
                q = _stats.quantiles(marks, n=4)
                entry["q1"] = round(q[0], 2)
                entry["q3"] = round(q[2], 2)
            result.append(entry)

        return result

    @app.get("/api/analytics/attendance-rates", tags=["analytics"])
    def analytics_attendance_rates(conn=Depends(db), a=Depends(actor)):
        """Per-school and per-subject attendance rates."""
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may view analytics")

        # Per-school attendance
        schools_rows = conn.execute(
            "SELECT centre_number, name FROM schools ORDER BY centre_number"
        ).fetchall()
        per_school = []
        for school in schools_rows:
            cn = school["centre_number"]
            total = conn.execute(
                "SELECT COUNT(DISTINCT a.student_id || '|' || a.subject_code || '|' || a.paper_type) c"
                " FROM attendance a JOIN students s ON s.student_id=a.student_id"
                " WHERE s.centre_number=?", (cn,)
            ).fetchone()["c"]
            present = conn.execute(
                "SELECT COUNT(DISTINCT a.student_id || '|' || a.subject_code || '|' || a.paper_type) c"
                " FROM attendance a JOIN students s ON s.student_id=a.student_id"
                " WHERE s.centre_number=? AND a.is_present=1", (cn,)
            ).fetchone()["c"]
            rate = round((present / total * 100), 1) if total > 0 else 0.0
            per_school.append({
                "centre_number": cn,
                "school_name": school["name"],
                "total": total,
                "present": present,
                "rate": rate,
            })

        # Per-subject attendance
        subjects_rows = conn.execute(
            "SELECT subject_code, name FROM subjects ORDER BY subject_code"
        ).fetchall()
        per_subject = []
        for subj in subjects_rows:
            sc = subj["subject_code"]
            total = conn.execute(
                "SELECT COUNT(DISTINCT a.student_id || '|' || a.paper_type) c"
                " FROM attendance a WHERE a.subject_code=?", (sc,)
            ).fetchone()["c"]
            present = conn.execute(
                "SELECT COUNT(DISTINCT a.student_id || '|' || a.paper_type) c"
                " FROM attendance a WHERE a.subject_code=? AND a.is_present=1", (sc,)
            ).fetchone()["c"]
            rate = round((present / total * 100), 1) if total > 0 else 0.0
            per_subject.append({
                "subject_code": sc,
                "subject_name": subj["name"],
                "total": total,
                "present": present,
                "rate": rate,
            })

        return {"per_school": per_school, "per_subject": per_subject}

    # ---------------- available-scopes for user creation ----------------

    @app.get("/api/admin/users/available-scopes", tags=["admin"])
    def available_scopes(centre_numbers: str = "", conn=Depends(db), a=Depends(actor)):
        """Return subjects that have students registered at the given schools.

        Query param: centre_numbers (comma-separated list of centre numbers).
        Returns only subjects relevant to those schools on this station.
        """
        if a["role"] != "EXAM_ADMIN":
            raise HTTPException(403, "Only the station admin may query available scopes")
        centres = [c.strip() for c in centre_numbers.split(",") if c.strip()]
        if not centres:
            # Return all subjects on this station
            rows = conn.execute(
                "SELECT DISTINCT ss.subject_code FROM student_subjects ss"
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in centres)
            rows = conn.execute(
                f"SELECT DISTINCT ss.subject_code FROM students s"
                f" JOIN student_subjects ss ON ss.student_id=s.student_id"
                f" WHERE s.centre_number IN ({placeholders})",
                centres
            ).fetchall()

        # Resolve subject names
        result = []
        for r in rows:
            sc = r["subject_code"]
            subj = conn.execute(
                "SELECT name FROM subjects WHERE subject_code=?", (sc,)
            ).fetchone()
            result.append({
                "subject_code": sc,
                "subject_name": subj["name"] if subj else sc,
            })
        result.sort(key=lambda x: x["subject_code"])
        return result

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
