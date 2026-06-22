"""
Tests for the `reconcile` conservation proof (photo_organizer.reconcile).

Builds a temp DB with fabricated files + operations covering each terminal
state, asserts the balance-sheet counts and that a clean set balances
(UNACCOUNTED == 0). Then injects a "done MOVE but file missing on disk" case
and asserts --verify-disk flags it and the command would exit non-zero.
"""

from __future__ import annotations

from photo_organizer.db import Database
from photo_organizer.reconcile import (
    CONFLICT,
    DELETED,
    ERROR,
    MOVED,
    SKIPPED,
    STAGED,
    UNACCOUNTED,
    UNTOUCHED,
    _classify,
    reconcile,
)


def _add_file(db, path, *, file_type="CAMERA_JPEG", status="scanned") -> int:
    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status) "
        "VALUES (?,?,?,?,?)",
        (str(path), str(path).rsplit("/", 1)[-1], "jpg", file_type, status),
    )
    return cur.lastrowid


def _add_op(db, file_id, op_type, status, *, source="/src", target="/dst"):
    db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, target_path, status) "
        "VALUES (?,?,?,?,?)",
        (file_id, op_type, source, target, status),
    )


def _build_clean_db(tmp_path):
    """One file in every terminal state; returns (db_path, dict of file_ids)."""
    db_path = tmp_path / "photos.db"
    ids = {}
    with Database(db_path) as db:
        # moved
        moved = tmp_path / "Masters" / "a.jpg"
        moved.parent.mkdir(parents=True, exist_ok=True)
        moved.write_text("x")
        ids["moved"] = _add_file(db, moved, status="done")
        _add_op(db, ids["moved"], "MOVE", "done")

        # staged
        staged = tmp_path / "_staging" / "to_delete" / "1_b.jpg"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("x")
        ids["staged"] = _add_file(db, staged, status="done")
        _add_op(db, ids["staged"], "STAGE_DELETE", "done")

        # conflict-renamed (moved, name carries _conflict_N)
        conflict = tmp_path / "Masters" / "c_conflict_1.jpg"
        conflict.write_text("x")
        ids["conflict"] = _add_file(db, conflict, status="done")
        _add_op(db, ids["conflict"], "MOVE", "done")

        # skipped/pending (confirmed op, not yet executed)
        ids["skipped"] = _add_file(db, tmp_path / "d.jpg", status="confirmed")
        _add_op(db, ids["skipped"], "MOVE", "confirmed")

        # error
        ids["error"] = _add_file(db, tmp_path / "e.jpg", status="error")

        # untouched-by-design (UNKNOWN, no op)
        ids["untouched"] = _add_file(
            db, tmp_path / "f.bin", file_type="UNKNOWN", status="scanned"
        )
        db.commit()
    return db_path, ids


def test_classify_each_state():
    moved_row = {"path": "/x/a.jpg", "file_type": "CAMERA_JPEG", "status": "done"}
    assert _classify(moved_row, [{"op_type": "MOVE", "status": "done"}]) == MOVED

    conflict_row = {"path": "/x/a_conflict_2.jpg", "file_type": "CAMERA_JPEG", "status": "done"}
    assert _classify(conflict_row, [{"op_type": "MOVE", "status": "done"}]) == CONFLICT

    staged_row = {"path": "/s/b.jpg", "file_type": "CAMERA_JPEG", "status": "done"}
    assert _classify(staged_row, [{"op_type": "STAGE_DELETE", "status": "done"}]) == STAGED

    pending_row = {"path": "/x/d.jpg", "file_type": "RAW", "status": "confirmed"}
    assert _classify(pending_row, [{"op_type": "MOVE", "status": "confirmed"}]) == SKIPPED

    err_row = {"path": "/x/e.jpg", "file_type": "RAW", "status": "error"}
    assert _classify(err_row, []) == ERROR

    unknown_row = {"path": "/x/f.bin", "file_type": "UNKNOWN", "status": "scanned"}
    assert _classify(unknown_row, []) == UNTOUCHED

    # A kept file with no operation should NOT silently balance.
    orphan_row = {"path": "/x/g.jpg", "file_type": "CAMERA_JPEG", "status": "scanned"}
    assert _classify(orphan_row, []) == UNACCOUNTED


def test_clean_set_balances(tmp_path):
    db_path, _ = _build_clean_db(tmp_path)
    with Database(db_path) as db:
        ok = reconcile(db, verify_disk=False)
        assert ok is True
        total = db.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert total == 6
        # No reconcile anomalies logged for a clean set.
        anomalies = db.conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE phase='reconcile'"
        ).fetchone()[0]
        assert anomalies == 0


def test_unaccounted_fails(tmp_path):
    db_path, _ = _build_clean_db(tmp_path)
    with Database(db_path) as db:
        # A kept (non-UNKNOWN) file with no operation == unaccounted.
        _add_file(db, tmp_path / "orphan.jpg", status="scanned")
        db.commit()
        ok = reconcile(db, verify_disk=False)
        assert ok is False
        logged = db.conn.execute(
            "SELECT message FROM run_log WHERE phase='reconcile'"
        ).fetchall()
        assert any("Unaccounted" in r["message"] for r in logged)


def test_verify_disk_flags_lost_file(tmp_path):
    db_path, ids = _build_clean_db(tmp_path)
    with Database(db_path) as db:
        # DB-only reconcile still balances...
        assert reconcile(db, verify_disk=False) is True

        # ...but delete the moved file on disk: a 'done' row now points nowhere.
        moved_path = db.conn.execute(
            "SELECT path FROM files WHERE file_id=?", (ids["moved"],)
        ).fetchone()["path"]
        import os
        os.remove(moved_path)

        ok = reconcile(db, verify_disk=True)
        assert ok is False
        lost = db.conn.execute(
            "SELECT path FROM run_log WHERE phase='reconcile' AND message LIKE 'LOST FILE%'"
        ).fetchall()
        assert len(lost) == 1
        assert lost[0]["path"] == moved_path


def test_verify_disk_passes_when_all_present(tmp_path):
    db_path, _ = _build_clean_db(tmp_path)
    with Database(db_path) as db:
        # All moved/staged files were written to disk in the builder.
        assert reconcile(db, verify_disk=True) is True


# ── DELETED — a sync.acknowledge_deleted() acknowledgment (Pillar 2) ─────────
# A 'done' file the user removed directly in Explorer, then ran
# `sync delete` to record it. There is nothing to verify on disk (the path
# is known gone), and it must NOT show up as UNACCOUNTED — that would be a
# regression that re-introduces the exact "the DB looks broken when it
# isn't" noise Pillar 1 set out to eliminate.

def test_classify_done_delete_op_is_deleted_not_unaccounted():
    row = {"path": "/x/gone.jpg", "file_type": "CAMERA_JPEG", "status": "done"}
    assert _classify(row, [
        {"op_type": "MOVE", "status": "done"},
        {"op_type": "DELETE", "status": "done"},
    ]) == DELETED


def test_acknowledged_deleted_file_balances(tmp_path):
    db_path, _ = _build_clean_db(tmp_path)
    with Database(db_path) as db:
        gone = _add_file(db, tmp_path / "gone.jpg", status="done")
        _add_op(db, gone, "MOVE", "done")
        _add_op(db, gone, "DELETE", "done", target=None)
        db.commit()

        ok = reconcile(db, verify_disk=False)
        assert ok is True
        anomalies = db.conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE phase='reconcile'"
        ).fetchone()[0]
        assert anomalies == 0


def test_verify_disk_does_not_check_deleted_rows(tmp_path):
    # gone.jpg's path never existed on disk at all — --verify-disk must not
    # try to stat it (that's the whole point of acknowledging the deletion).
    db_path, _ = _build_clean_db(tmp_path)
    with Database(db_path) as db:
        gone = _add_file(db, tmp_path / "never_existed.jpg", status="done")
        _add_op(db, gone, "MOVE", "done")
        _add_op(db, gone, "DELETE", "done", target=None)
        db.commit()

        assert reconcile(db, verify_disk=True) is True
