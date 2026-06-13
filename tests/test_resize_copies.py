"""
Tests for resized / downscaled copy detection (`planner.resize_loser_ids`) and
its integration into `plan()`.

A resize copy is a smaller version of a shot whose larger original survives
(different sha256, so EXACT dedup never catches it).  Rule:
  - "shot signature" = (normalized stem, EXIF datetime_original)
  - keeper = largest-area file (keep_score)
  - loser  = a STRICTLY-smaller sibling sharing the keeper's aspect ratio
A unique / largest copy is NEVER a loser ("a resized photo needs a bigger one to
exist before it can be deleted").

Run: python -m pytest tests/test_resize_copies.py
"""

from __future__ import annotations

from photo_organizer.db import Database
from photo_organizer.planner import plan, resize_loser_ids


def _add(db, fid, filename, *, w, h, dt, sha=None, ftype="CAMERA_JPEG",
         path=None, size=None):
    db.conn.execute(
        "INSERT INTO files (file_id, path, filename, extension, file_type, "
        "status, width, height, size_bytes, sha256, datetime_original, "
        "date_source, date_confidence) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, path or f"/a/{filename}", filename, "jpg", ftype, "scanned",
         w, h, size if size is not None else w * h, sha, dt,
         "exif_original", "HIGH"),
    )


# ---------------------------------------------------------------------------
# resize_loser_ids — the detection rule
# ---------------------------------------------------------------------------

def test_smaller_sibling_is_loser(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")
        _add(db, 2, "IMG_1.jpg", w=1920, h=1280, dt="2020:01:01 10:00:00",
             path="/b/IMG_1.jpg")
        db.commit()
        assert resize_loser_ids(db) == {2}  # keeper=1 (larger), loser=2


def test_unique_image_never_loser(tmp_path):
    """A lone small image (no larger sibling) is kept — it IS the original."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=1920, h=1280, dt="2020:01:01 10:00:00")
        db.commit()
        assert resize_loser_ids(db) == set()


def test_different_datetime_not_matched(tmp_path):
    """Same filename but different capture time = different shots (e.g. two cards)."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "DSC_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")
        _add(db, 2, "DSC_1.jpg", w=1920, h=1280, dt="2019:05:05 08:00:00",
             path="/b/DSC_1.jpg")
        db.commit()
        assert resize_loser_ids(db) == set()


def test_different_aspect_not_matched(tmp_path):
    """A crop changes the aspect ratio → not a pure downscale → kept."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")   # 1.5
        _add(db, 2, "IMG_1.jpg", w=1000, h=1000, dt="2020:01:01 10:00:00",   # 1.0
             path="/b/IMG_1.jpg")
        db.commit()
        assert resize_loser_ids(db) == set()


def test_equal_size_tie_not_loser(tmp_path):
    """Same dimensions = a tie left to exact dedup, not a resize loser."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")
        _add(db, 2, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00",
             path="/b/IMG_1.jpg")
        db.commit()
        assert resize_loser_ids(db) == set()


def test_missing_datetime_not_matched(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt=None)
        _add(db, 2, "IMG_1.jpg", w=1920, h=1280, dt=None, path="/b/IMG_1.jpg")
        db.commit()
        assert resize_loser_ids(db) == set()


def test_three_resolutions_keep_largest(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")
        _add(db, 2, "IMG_1.jpg", w=3000, h=2000, dt="2020:01:01 10:00:00",
             path="/b/IMG_1.jpg")
        _add(db, 3, "IMG_1.jpg", w=800, h=533, dt="2020:01:01 10:00:00",
             path="/c/IMG_1.jpg")  # 1.50 aspect (≈)
        db.commit()
        # 800x533 = 1.5009..., within tolerance of 1.5; both smaller are losers.
        assert resize_loser_ids(db) == {2, 3}


# ---------------------------------------------------------------------------
# plan() integration — resize loser is staged and NOT rescued by the 1d net
# ---------------------------------------------------------------------------

def test_plan_stages_unique_sha_resize_copy(tmp_path):
    """A resize copy with its OWN sha (no byte-twin) must still be staged.

    The 1d safety net keeps one copy of each sha256 group; a unique-sha resize
    copy would be wrongly "rescued" unless exempted as content-safe.  The larger
    original (different sha) is what preserves the content.
    """
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00",
             sha="big_original_sha")
        _add(db, 2, "IMG_1.jpg", w=1920, h=1280, dt="2020:01:01 10:00:00",
             sha="small_copy_sha", path="/b/IMG_1.jpg")
        db.commit()

        plan(db, tmp_path / "out", assume_yes=True)

        ops = {
            r["file_id"]: r["op_type"]
            for r in db.conn.execute(
                "SELECT file_id, op_type FROM operations"
            ).fetchall()
        }
        assert ops[2] == "STAGE_DELETE", "resize copy must be staged"
        assert ops[1] == "MOVE", "larger original must be kept and moved"
