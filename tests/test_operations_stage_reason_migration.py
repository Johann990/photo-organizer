"""
Migration test for operations.stage_reason / dupe_of_file_id (schema v8).

Mirrors the established pattern in test_folder_overrides_db.py
(test_migration_adds_confirmed_subject_to_v6_db): build a pre-migration v7 DB
by hand, open it with Database(), and confirm the columns appear with a usable
default — idempotent, no disk read, safe on a fresh DB too.
"""

from __future__ import annotations

import sqlite3

from photo_organizer.db import Database


def test_migration_adds_stage_reason_to_v7_db(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # file_type CHECK must already list 'HEIC' so _migrate_filetype_check's
    # unrelated files-table rebuild (a separate, pre-existing migration) is a
    # no-op here -- this test targets the stage_reason/dupe_of_file_id columns only.
    conn.execute(
        "CREATE TABLE files (file_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "path TEXT, filename TEXT, extension TEXT, status TEXT, "
        "sha256 TEXT, phash TEXT, datetime_original TEXT, "
        "file_type TEXT CHECK(file_type IN "
        "('RAW','CAMERA_JPEG','DEV_JPEG','RESIZED_JPEG','VIDEO','HEIC','UNKNOWN')), "
        "camera_make TEXT, camera_model TEXT)"
    )
    conn.execute(
        "CREATE TABLE operations (op_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "file_id INTEGER NOT NULL, op_type TEXT NOT NULL, source_path TEXT NOT NULL, "
        "target_path TEXT, status TEXT NOT NULL DEFAULT 'planned', error_msg TEXT, "
        "planned_at TEXT, executed_at TEXT)"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '7')")
    conn.execute(
        "INSERT INTO files (path, filename, extension, status) VALUES (?,?,?,?)",
        ("D:\\Old\\a.jpg", "a.jpg", "jpg", "confirmed"),
    )
    conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, status) "
        "VALUES (1, 'MOVE', 'D:\\Old\\a.jpg', 'confirmed')"
    )
    conn.commit()
    conn.close()

    with Database(db_path) as db:
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(operations)")]
        assert "stage_reason" in cols
        assert "dupe_of_file_id" in cols
        row = db.conn.execute(
            "SELECT stage_reason, dupe_of_file_id FROM operations WHERE file_id=1"
        ).fetchone()
        assert row["stage_reason"] is None
        assert row["dupe_of_file_id"] is None


def test_migration_idempotent_on_fresh_db(tmp_path):
    db_path = tmp_path / "fresh.db"
    with Database(db_path) as db:
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(operations)")]
        assert "stage_reason" in cols
        assert "dupe_of_file_id" in cols
