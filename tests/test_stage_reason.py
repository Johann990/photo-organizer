"""
Tests that every STAGE_DELETE operation plan() creates carries a stage_reason
and (where a specific duplicate is known) a dupe_of_file_id, and lands under
the matching _staging/to_delete/<reason>/ subfolder instead of one flat pile.

Covers the five staging categories from planner.plan():
  1a resized_jpeg, 1b exact_dupe, 1c near_dupe, 1c-bis redundant_copy,
  1d folder_merge_loser (covered separately in test_planner_folder_merge.py).

Run: python -m pytest tests/test_stage_reason.py
"""

from __future__ import annotations

from photo_organizer.db import Database
from photo_organizer.planner import plan


def _add_file(db, fid, path, *, sha=None, status="scanned", file_type="CAMERA_JPEG",
              w=None, h=None, dt=None, size=1000):
    filename = path.split("/")[-1]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    db.conn.execute(
        "INSERT INTO files (file_id, path, filename, extension, sha256, status, "
        "file_type, size_bytes, width, height, datetime_original, "
        "date_source, date_confidence) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, path, filename, ext, sha, status, file_type, size, w, h, dt,
         "exif_original" if dt else None, "HIGH" if dt else None),
    )


def _ops(db):
    return {
        r["file_id"]: r
        for r in db.conn.execute(
            "SELECT file_id, op_type, target_path, stage_reason, dupe_of_file_id "
            "FROM operations"
        ).fetchall()
    }


def test_resized_jpeg_reason_and_subfolder(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add_file(db, 1, "/a/resized/img001.jpg", file_type="RESIZED_JPEG")
        db.commit()

        plan(db, tmp_path / "out", assume_yes=True)
        ops = _ops(db)

    assert ops[1]["op_type"] == "STAGE_DELETE"
    assert ops[1]["stage_reason"] == "resized_jpeg"
    assert ops[1]["dupe_of_file_id"] is None
    assert "resized_jpeg" in ops[1]["target_path"]


def test_exact_dupe_reason_records_keeper(tmp_path):
    with Database(tmp_path / "p.db") as db:
        # Larger file wins keep_score (size_bytes tiebreak) -> file 1 kept, 2 staged.
        # _exact_dup_groups reads the `duplicates` table (populated by `dedup`),
        # not raw sha256 matches in `files` — a shared sha256 alone isn't enough.
        _add_file(db, 1, "/a/img001.jpg", sha="same_sha", size=5000)
        _add_file(db, 2, "/b/img001.jpg", sha="same_sha", size=1000)
        db.conn.execute(
            "INSERT INTO duplicates (file_id_a, file_id_b, dup_type) VALUES (1, 2, 'EXACT')"
        )
        db.commit()

        plan(db, tmp_path / "out", assume_yes=True)
        ops = _ops(db)

    assert ops[1]["op_type"] == "MOVE"
    assert ops[2]["op_type"] == "STAGE_DELETE"
    assert ops[2]["stage_reason"] == "exact_dupe"
    assert ops[2]["dupe_of_file_id"] == 1
    assert "exact_dupe" in ops[2]["target_path"]


def test_near_dupe_reason_records_keeper(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add_file(db, 1, "/a/img001.jpg")
        _add_file(db, 2, "/b/img002.jpg")
        db.commit()
        db.conn.execute(
            "INSERT INTO duplicates (file_id_a, file_id_b, dup_type, status, keep_file_id) "
            "VALUES (1, 2, 'NEAR', 'reviewed', 1)"
        )
        db.commit()

        plan(db, tmp_path / "out", assume_yes=True)
        ops = _ops(db)

    assert ops[1]["op_type"] == "MOVE"
    assert ops[2]["op_type"] == "STAGE_DELETE"
    assert ops[2]["stage_reason"] == "near_dupe"
    assert ops[2]["dupe_of_file_id"] == 1
    assert "near_dupe" in ops[2]["target_path"]


def test_rescued_keeper_has_no_stage_reason(tmp_path):
    """The 1e safety net discards a wrongly-staged keeper from every loser set
    when independent NEAR decisions would otherwise stage EVERY copy of one
    sha256 group. The rescue must also clear that file's reason_map entry,
    since it's no longer staged (no orphaned stage_reason on a MOVE op)."""
    with Database(tmp_path / "p.db") as db:
        # file 1 and 2 share a sha (the group the safety net protects).
        _add_file(db, 1, "/a/img001.jpg", sha="shared_sha", size=1000)
        _add_file(db, 2, "/b/img001.jpg", sha="shared_sha", size=1000)
        # Two unrelated keepers elsewhere, each winning an INDEPENDENT NEAR
        # review against one half of the shared-sha group — together staging
        # both 1 and 2, leaving no survivor unless the safety net intervenes.
        _add_file(db, 3, "/c/other1.jpg", sha="sha3")
        _add_file(db, 4, "/d/other2.jpg", sha="sha4")
        db.commit()
        db.conn.execute(
            "INSERT INTO duplicates (file_id_a, file_id_b, dup_type, status, keep_file_id) "
            "VALUES (1, 3, 'NEAR', 'reviewed', 3)"
        )
        db.conn.execute(
            "INSERT INTO duplicates (file_id_a, file_id_b, dup_type, status, keep_file_id) "
            "VALUES (2, 4, 'NEAR', 'reviewed', 4)"
        )
        db.commit()

        plan(db, tmp_path / "out", assume_yes=True)
        ops = _ops(db)

    # Of the shared-sha group {1, 2}, exactly one must survive as MOVE.
    group_ops = {fid: ops[fid]["op_type"] for fid in (1, 2)}
    kept = [fid for fid, op_type in group_ops.items() if op_type == "MOVE"]
    staged = [fid for fid, op_type in group_ops.items() if op_type == "STAGE_DELETE"]
    assert len(kept) == 1 and len(staged) == 1
    keeper = kept[0]
    assert ops[keeper]["stage_reason"] is None
    assert ops[keeper]["dupe_of_file_id"] is None
