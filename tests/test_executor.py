"""
Regression test for executor.execute()'s crash-recovery path.

A prior execute run can be interrupted right after the on-disk rename
succeeds but before the DB is updated: the op is left 'in_progress',
files.path still points at the (now gone) source, and the target already
exists. execute() is supposed to detect this on the next run and seal the
op as 'done' instead of re-attempting the move — but the ops query must
actually select operations.status for that check to work at all.
"""

from __future__ import annotations

from photo_organizer import executor
from photo_organizer.db import Database


def _seed_interrupted_move(db, tmp_path):
    """An op whose rename already completed on disk, but the DB still says
    'in_progress' (as if a prior execute() crashed between the rename and
    the follow-up status update)."""
    src = tmp_path / "src" / "a.jpg"
    target_root = tmp_path / "Organised"
    dst = target_root / "Masters" / "2023" / "a.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("x")  # the move already happened; source is gone

    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status) "
        "VALUES (?,?,?,?,'confirmed')",
        (str(src), "a.jpg", "jpg", "CAMERA_JPEG"),
    )
    fid = cur.lastrowid
    db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, target_path, status) "
        "VALUES (?, 'MOVE', ?, ?, 'in_progress')",
        (fid, str(src), str(dst)),
    )
    db.set_phase_status("review", "complete", {"target_root": str(target_root)})
    db.commit()
    return fid


def test_crash_recovery_seals_completed_rename_without_raising(tmp_path, monkeypatch):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _seed_interrupted_move(db, tmp_path)
        monkeypatch.setattr(executor, "_same_drive", lambda a, b: True)

        executor.execute(db)  # must not raise (regression: missing o.status column)

        op_status = db.conn.execute(
            "SELECT status FROM operations WHERE file_id=?", (fid,)
        ).fetchone()["status"]
        assert op_status == "done"
        file_row = db.conn.execute(
            "SELECT status, path FROM files WHERE file_id=?", (fid,)
        ).fetchone()
        assert file_row["status"] == "done"
        assert file_row["path"] == str(tmp_path / "Organised" / "Masters" / "2023" / "a.jpg")
