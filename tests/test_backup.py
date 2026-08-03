from __future__ import annotations

from pathlib import Path

import pytest

from station.backup import backup_database, list_snapshots, restore_database, verify_snapshot
from station.db import connect, get_user_version, set_user_version
from station.migrations import PackageImportError, apply_migrations


def _populate(conn):
    conn.execute("INSERT INTO students(student_id, centre_number, first_name, surname, sex) VALUES('S-1','SCH-1','A','B','M')")
    conn.execute("INSERT INTO total_marks(student_id, subject_code, paper_type, total_marks_obtained) VALUES('S-1','011','THEORY1',67)")
    conn.execute("INSERT INTO outbox_events(event_id, entity_type, operation, natural_key_json, local_version, occurred_at, status) "
                 "VALUES('evt_1','STUDENT_PAPER_MARKS_REPLACED','UPSERT','{}',1,'2026-07-27T08:00:00Z','PENDING')")
    conn.commit()


def test_backup_roundtrip_preserves_data(tmp_path):
    db = tmp_path / "s.sqlite3"
    conn = connect(db)
    apply_migrations(conn)
    _populate(conn)
    conn.close()

    snap = backup_database(db, tmp_path / "backups")
    assert verify_snapshot(snap)
    assert len(list_snapshots(db, tmp_path / "backups")) == 1

    db.unlink()
    restore_database(snap, db)
    conn2 = connect(db)
    assert conn2.execute("SELECT total_marks_obtained FROM total_marks WHERE student_id='S-1'").fetchone()[0] == 67
    assert conn2.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1


def test_fresh_install_reaches_current_schema(tmp_path):
    conn = connect(tmp_path / "s.sqlite3")
    v = apply_migrations(conn)
    from station import SCHEMA_VERSION
    assert v == SCHEMA_VERSION
    cols = [r[1] for r in conn.execute("PRAGMA table_info(outbox_events)").fetchall()]
    # The baseline schema includes 'priority' directly, not as a later upgrade.
    assert "priority" in cols


def test_reject_higher_user_version(tmp_path):
    """A database written by a newer Station build must be rejected."""
    db_path = tmp_path / "s.sqlite3"
    conn = connect(db_path)
    apply_migrations(conn)
    from station import SCHEMA_VERSION
    set_user_version(conn, SCHEMA_VERSION + 1)
    with pytest.raises(PackageImportError) as exc:
        apply_migrations(conn)
    assert exc.value.code == "UPGRADE_REQUIRED"
