"""HTTP sync transport + config tests.

Spins a tiny in-process fake Central, points the station at it, enters a mark,
and runs the real HTTP sync path (station.sync_http.run_http_sync) — proving the
station posts the correct body with its X-Station-Key and applies the ack.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from argon2 import PasswordHasher

from lazeims_common.enums import FillingMode, PaperType
from lazeims_common.hashing import sha256_prefixed

from station import entry as E
from station import sync_http as SH
from station.db import connect
from station.migrations import apply_migrations, import_package

T1 = PaperType.THEORY1
_ph = PasswordHasher()

# what the fake Central captured (for assertions)
CAPTURED: dict = {}


def _seed():
    return {
        "schools": [{"centre_number": "SCH-1", "name": "S1"}],
        "subjects": [{"subject_code": "011", "name": "H", "papers": ["THEORY1"],
                      "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0}, "groups": [], "questions": []}],
        "students": [{"student_id": "S-1", "centre_number": "SCH-1", "first_name": "A", "middle_name": None, "surname": "B", "sex": "M"}],
        "registrations": [{"student_id": "S-1", "subject_code": "011"}],
        "credentials": [], "processing_api_key": None,
    }


def _bundle():
    seed = _seed()
    return {"manifest": {"contract_version": "station-package/v1", "package_id": "pkg_s", "package_version": 1,
                         "rules_version": "1.0", "software_min_version": "1.0.0", "station_code": "ST-1",
                         "exam_id": "FTNA-2026", "configuration_hash": sha256_prefixed(seed),
                         "issued_at": "2026-07-27T08:00:00Z",
                         "scope": {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, "signature": "x"},
            "seed": seed}


def _db(tmp_path):
    conn = connect(tmp_path / "s.sqlite3")
    apply_migrations(conn)
    import_package(conn, _bundle())
    E.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1,
                            is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=42)
    E.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1,
                                mode=FillingMode.TOTAL_MARKS, total_marks_obtained=67, actor_assignment_id=42)
    return conn


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode())
        CAPTURED["path"] = self.path
        CAPTURED["credential_id"] = self.headers.get("X-Package-Credential-Id")
        CAPTURED["secret"] = self.headers.get("X-Package-Secret")
        CAPTURED["body"] = body
        resp = {
            "accepted": [{"event_id": e["event_id"], "central_version": 1} for e in body["events"]],
            "duplicates": [], "rejected": [], "server_time": "t",
        }
        payload = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_http_sync_posts_with_key_and_accepts(tmp_path, monkeypatch):
    CAPTURED.clear()
    httpd = _serve()
    try:
        port = httpd.server_address[1]

        # Set up LAZEIMS_HOME so machine_credential.store/load works
        monkeypatch.setenv("LAZEIMS_HOME", str(tmp_path / "lazhome"))
        from station import machine_credential, paths
        # Invalidate any cached home
        paths.lazeims_home.cache_clear() if hasattr(paths.lazeims_home, 'cache_clear') else None

        conn = _db(tmp_path)

        # not configured yet -> no-op
        assert SH.run_http_sync(conn)["configured"] is False

        # Store machine credential and set central_url
        machine_credential.store("ST-1", "FTNA-2026", {
            "credential_id": "cred-123",
            "package_id": "pkg_s",
            "secret": "secret-key-123",
        })
        SH.set_sync_config(conn, central_url=f"http://127.0.0.1:{port}")
        cfg = SH.get_sync_config(conn)
        assert cfg["configured"] is True and cfg["has_credential"] is True

        res = SH.run_http_sync(conn)
        assert res["configured"] is True and res["accepted"] == 2, res

        # the station sent the right endpoint and credential headers
        assert CAPTURED["path"] == "/api/v1/station/sync/events"
        assert CAPTURED["credential_id"] == "cred-123"
        assert CAPTURED["secret"] == "secret-key-123"
        assert CAPTURED["body"]["station_code"] == "ST-1"
        assert CAPTURED["body"]["package_id"] == "pkg_s"
        assert len(CAPTURED["body"]["events"]) == 2

        # outbox drained
        assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='ACCEPTED'").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='PENDING'").fetchone()["c"] == 0
    finally:
        httpd.shutdown()


def test_http_sync_offline_is_resumable(tmp_path, monkeypatch):
    # Set up LAZEIMS_HOME so machine_credential.store/load works
    monkeypatch.setenv("LAZEIMS_HOME", str(tmp_path / "lazhome"))
    from station import machine_credential, paths
    paths.lazeims_home.cache_clear() if hasattr(paths.lazeims_home, 'cache_clear') else None

    conn = _db(tmp_path)
    machine_credential.store("ST-1", "FTNA-2026", {
        "credential_id": "cred-1",
        "package_id": "pkg_s",
        "secret": "k",
    })
    # point at a closed port -> connection refused
    SH.set_sync_config(conn, central_url="http://127.0.0.1:1")
    res = SH.run_http_sync(conn)
    assert res.get("resumable") is True or res.get("error") is not None
    assert conn.execute("SELECT COUNT(*) c FROM outbox_events WHERE status='PENDING'").fetchone()["c"] == 2
