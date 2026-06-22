"""
Tests for undo()'s optional filters (--year/--camera/--software/--type/--op-type)
and the phase-reset behavior they imply: a filtered/partial undo must leave the
'execute' phase alone (so a later unfiltered undo or execute doesn't redo work
that's still in place), while a full unfiltered undo that reverts everything
with no errors/skips must reset 'execute' back to 'pending'.

Run: python -m pytest tests/test_undo_filters.py
"""

from __future__ import annotations

from photo_organizer.db import Database
from photo_organizer.executor import undo


def _add_done_op(db, tmp_path, fid, *, op_type="MOVE", year=None, camera=None,
                  file_type="CAMERA_JPEG"):
    """One 'done' operation with a real file at its current location, so undo's
    os.rename has something to move back."""
    cur_path = tmp_path / f"cur_{fid}.jpg"
    orig_path = tmp_path / f"orig_{fid}.jpg"
    cur_path.write_bytes(b"x")

    dt = f"{year}:01:01 10:00:00" if year else None
    db.conn.execute(
        "INSERT INTO files (file_id, path, filename, extension, file_type, "
        "status, datetime_original, camera_model) VALUES (?,?,?,?,?,?,?,?)",
        (fid, str(cur_path), cur_path.name, "jpg", file_type, "done", dt, camera),
    )
    db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, target_path, status) "
        "VALUES (?,?,?,?,?)",
        (fid, op_type, str(orig_path), str(cur_path), "done"),
    )
    db.commit()
    return cur_path, orig_path


def _op_status(db, fid):
    return db.conn.execute(
        "SELECT status FROM operations WHERE file_id=?", (fid,)
    ).fetchone()["status"]


def test_year_filter_reverts_only_matching_year(tmp_path):
    with Database(tmp_path / "p.db") as db:
        cur_a, orig_a = _add_done_op(db, tmp_path, 1, year="2020")
        cur_b, orig_b = _add_done_op(db, tmp_path, 2, year="2021")

        undo(db, force=True, year="2020")

        assert orig_a.exists() and not cur_a.exists()
        assert _op_status(db, 1) == "confirmed"

        assert cur_b.exists() and not orig_b.exists()
        assert _op_status(db, 2) == "done"


def test_op_type_filter_reverts_only_staged_delete(tmp_path):
    with Database(tmp_path / "p.db") as db:
        cur_a, orig_a = _add_done_op(db, tmp_path, 1, op_type="STAGE_DELETE")
        cur_b, orig_b = _add_done_op(db, tmp_path, 2, op_type="MOVE")

        undo(db, force=True, op_type="STAGE_DELETE")

        assert orig_a.exists() and not cur_a.exists()
        assert _op_status(db, 1) == "confirmed"

        assert cur_b.exists() and not orig_b.exists()
        assert _op_status(db, 2) == "done"


def test_camera_and_software_filters_narrow_selection(tmp_path):
    with Database(tmp_path / "p.db") as db:
        cur_a, orig_a = _add_done_op(db, tmp_path, 1, camera="ILCE-7RM2")
        cur_b, orig_b = _add_done_op(db, tmp_path, 2, camera="iPhone 13")

        undo(db, force=True, camera="ILCE")

        assert orig_a.exists() and not cur_a.exists()
        assert cur_b.exists() and not orig_b.exists()
        assert _op_status(db, 2) == "done"


def test_filtered_undo_does_not_reset_phase_when_done_remain(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add_done_op(db, tmp_path, 1, year="2020")
        _add_done_op(db, tmp_path, 2, year="2021")
        db.set_phase_status("execute", "complete")
        db.commit()

        undo(db, force=True, year="2020")

        row = db.conn.execute(
            "SELECT status FROM phases WHERE phase_name='execute'"
        ).fetchone()
        assert row["status"] == "complete"


def test_full_undo_resets_phase_to_pending(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add_done_op(db, tmp_path, 1, year="2020")
        db.set_phase_status("execute", "complete")
        db.commit()

        undo(db, force=True)

        row = db.conn.execute(
            "SELECT status FROM phases WHERE phase_name='execute'"
        ).fetchone()
        assert row["status"] == "pending"
