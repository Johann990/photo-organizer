"""
Tests for the execute pre-flight gate (photo_organizer.executor._preflight and
its integration into execute()).

The gate must refuse to start when the target is on a different volume than the
source, instead of letting every file fail mid-run with a cross-device rename
error. We stub validator._same_drive to simulate a volume mismatch.
"""

from __future__ import annotations

from photo_organizer import executor
from photo_organizer.db import Database


def _seed_confirmed(db, tmp_path):
    """Insert one confirmed MOVE op and record a target_root in the review phase."""
    src = tmp_path / "src" / "a.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x")
    target_root = tmp_path / "Organised"
    dst = target_root / "Masters" / "2023" / "a.jpg"

    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status) "
        "VALUES (?,?,?,?,'confirmed')",
        (str(src), "a.jpg", "jpg", "CAMERA_JPEG"),
    )
    fid = cur.lastrowid
    db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, target_path, status) "
        "VALUES (?, 'MOVE', ?, ?, 'confirmed')",
        (fid, str(src), str(dst)),
    )
    db.set_phase_status("review", "complete", {"target_root": str(target_root)})
    db.commit()
    return fid


def test_preflight_refuses_on_different_volume(tmp_path, monkeypatch):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _seed_confirmed(db, tmp_path)

        # Simulate a cross-volume target.
        monkeypatch.setattr(executor, "_same_drive", lambda a, b: False)

        executor.execute(db)

        # Refused: the op was NOT executed; the file stays confirmed.
        op_status = db.conn.execute(
            "SELECT status FROM operations WHERE file_id=?", (fid,)
        ).fetchone()["status"]
        assert op_status == "confirmed"
        file_status = db.conn.execute(
            "SELECT status FROM files WHERE file_id=?", (fid,)
        ).fetchone()["status"]
        assert file_status == "confirmed"

        # The refusal is logged.
        logged = db.conn.execute(
            "SELECT message FROM run_log WHERE phase='execute' AND level='ERROR'"
        ).fetchall()
        assert any("Pre-flight" in r["message"] for r in logged)


def test_preflight_passes_on_same_volume(tmp_path, monkeypatch):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _seed_confirmed(db, tmp_path)

        # Same volume → gate passes and the move actually runs.
        monkeypatch.setattr(executor, "_same_drive", lambda a, b: True)

        executor.execute(db)

        op_status = db.conn.execute(
            "SELECT status FROM operations WHERE file_id=?", (fid,)
        ).fetchone()["status"]
        assert op_status == "done"


def test_skip_preflight_bypasses_gate(tmp_path, monkeypatch):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _seed_confirmed(db, tmp_path)

        # Even with a (stubbed) cross-volume target, --skip-preflight proceeds.
        monkeypatch.setattr(executor, "_same_drive", lambda a, b: False)

        executor.execute(db, skip_preflight=True)

        op_status = db.conn.execute(
            "SELECT status FROM operations WHERE file_id=?", (fid,)
        ).fetchone()["status"]
        assert op_status == "done"
