from __future__ import annotations

from pathlib import Path

from station.backup import backup_database, list_snapshots, restore_database, verify_snapshot
from station.db import connect, get_user_version, set_user_version
from station.migrations import apply_migrations


def _populate(conn):
    # minimal rows in marks + outbox to prove they survive
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

    # wipe live db, restore from snapshot
    db.unlink()
    restore_database(snap, db)
    conn2 = connect(db)
    assert conn2.execute("SELECT total_marks_obtained FROM total_marks WHERE student_id='S-1'").fetchone()[0] == 67
    assert conn2.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1


def test_upgrade_v1_to_v2_preserves_marks_and_outbox(tmp_path):
    """Simulate an existing v1 station, then upgrade to current schema: marks and
    outbox must be preserved and a pre-upgrade backup taken."""
    db = tmp_path / "s.sqlite3"
    conn = connect(db)
    # Build only v1 schema and stamp user_version=1 (simulate older install).
    from station.migrations import _SCHEMA_V1
    conn.executescript(_SCHEMA_V1)
    set_user_version(conn, 1)
    _populate(conn)
    assert get_user_version(conn) == 1

    # Upgrade with a backup dir -> should back up then apply v2 additively.
    backups = tmp_path / "backups"
    final_version = apply_migrations(conn, backup_before_upgrade=str(backups))
    from station import SCHEMA_VERSION
    assert final_version == SCHEMA_VERSION == 2

    # data preserved
    assert conn.execute("SELECT total_marks_obtained FROM total_marks WHERE student_id='S-1'").fetchone()[0] == 67
    assert conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1
    # new additive column exists
    cols = [r[1] for r in conn.execute("PRAGMA table_info(outbox_events)").fetchall()]
    assert "priority" in cols
    # pre-upgrade backup was taken (data existed)
    assert len(list(backups.glob("*.sqlite3"))) == 1
    conn.close()


def test_fresh_install_reaches_current_schema(tmp_path):
    conn = connect(tmp_path / "s.sqlite3")
    v = apply_migrations(conn)
    from station import SCHEMA_VERSION
    assert v == SCHEMA_VERSION
    cols = [r[1] for r in conn.execute("PRAGMA table_info(outbox_events)").fetchall()]
    assert "priority" in cols  # v2 column present on fresh installs too
