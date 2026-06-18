"""Tests for folderreview.py — folder twin-pair review web UI."""
from __future__ import annotations
from datetime import datetime, timezone
from photo_organizer.db import Database


def _add_overlap(db, folder_a, folder_b, shared=10, a_only=0, b_only=2, keeper="a"):
    db.insert_folder_overlap(
        folder_a=folder_a, folder_b=folder_b,
        shared_count=shared, a_only_count=a_only, b_only_count=b_only,
        coverage_a=round(shared / (shared + a_only), 4),
        coverage_b=round(shared / (shared + b_only), 4),
        keeper=keeper,
    )
    db.commit()
    return db.conn.execute(
        "SELECT overlap_id FROM folder_overlaps ORDER BY overlap_id DESC LIMIT 1"
    ).fetchone()[0]


def test_record_folder_overlap_decision(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        oid = _add_overlap(db, "D:\\A", "D:\\B")
        now = datetime.now(timezone.utc).isoformat()
        db.record_folder_overlap_decision(oid, "b", now)
        db.commit()
        row = db.conn.execute(
            "SELECT status, keeper, reviewed_at FROM folder_overlaps WHERE overlap_id=?",
            (oid,),
        ).fetchone()
        assert row["status"] == "reviewed"
        assert row["keeper"] == "b"
        assert row["reviewed_at"] == now


def test_reopen_folder_overlap(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        oid = _add_overlap(db, "D:\\A", "D:\\B")
        db.record_folder_overlap_decision(oid, "a", datetime.now(timezone.utc).isoformat())
        db.commit()
        db.reopen_folder_overlap(oid)
        db.commit()
        row = db.conn.execute(
            "SELECT status, reviewed_at FROM folder_overlaps WHERE overlap_id=?",
            (oid,),
        ).fetchone()
        assert row["status"] == "pending"
        assert row["reviewed_at"] is None
