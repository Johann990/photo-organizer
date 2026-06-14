"""
Tests for redundant-copy detection (`planner.redundant_copy_ids`) and its
integration into `plan()`.

A redundant copy is a re-encode or downscale of a shot whose better version
survives (different sha256, so EXACT dedup never catches it).  Two safe signals:

  Rule A — re-encodes & renamed exports: files sharing EXIF datetime_original AND
           an identical, non-junk pHash are the same shot content → keep the best,
           stage the rest (even same-size re-encodes, even if renamed).
  Rule B — downscales whose pHash drifted: within a (stem, datetime_original)
           group, a STRICTLY-smaller same-aspect file is a pure downscale.

A copy is staged ONLY when a superior sibling is kept, so the unique / best copy
of any content is never deleted.  RAW is out of scope (never staged).

Run: python -m pytest tests/test_redundant_copies.py
"""

from __future__ import annotations

from photo_organizer.db import Database
from photo_organizer.planner import plan, redundant_copy_ids


def _add(db, fid, filename, *, w, h, dt, sha=None, phash=None,
         ftype="CAMERA_JPEG", path=None, size=None):
    db.conn.execute(
        "INSERT INTO files (file_id, path, filename, extension, file_type, "
        "status, width, height, size_bytes, sha256, phash, datetime_original, "
        "date_source, date_confidence) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, path or f"/a/{filename}", filename, "jpg", ftype, "scanned",
         w, h, size if size is not None else w * h, sha, phash, dt,
         "exif_original", "HIGH"),
    )


# ---------------------------------------------------------------------------
# Rule B — downscale (stem + datetime, strictly smaller, same aspect)
# ---------------------------------------------------------------------------

def test_smaller_sibling_is_loser(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")
        _add(db, 2, "IMG_1.jpg", w=1920, h=1280, dt="2020:01:01 10:00:00",
             path="/b/IMG_1.jpg")
        db.commit()
        assert redundant_copy_ids(db) == {2}  # keeper=1 (larger), loser=2


def test_unique_image_never_loser(tmp_path):
    """A lone small image (no larger sibling) is kept — it IS the original."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=1920, h=1280, dt="2020:01:01 10:00:00")
        db.commit()
        assert redundant_copy_ids(db) == set()


def test_different_datetime_not_matched(tmp_path):
    """Same filename but different capture time = different shots (recycled name)."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "DSC_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")
        _add(db, 2, "DSC_1.jpg", w=1920, h=1280, dt="2019:05:05 08:00:00",
             path="/b/DSC_1.jpg")
        db.commit()
        assert redundant_copy_ids(db) == set()


def test_different_aspect_not_matched(tmp_path):
    """A crop changes the aspect ratio → not a pure downscale → kept."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")   # 1.5
        _add(db, 2, "IMG_1.jpg", w=1000, h=1000, dt="2020:01:01 10:00:00",   # 1.0
             path="/b/IMG_1.jpg")
        db.commit()
        assert redundant_copy_ids(db) == set()


def test_missing_datetime_not_matched(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt=None)
        _add(db, 2, "IMG_1.jpg", w=1920, h=1280, dt=None, path="/b/IMG_1.jpg")
        db.commit()
        assert redundant_copy_ids(db) == set()


def test_three_resolutions_keep_largest(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=6000, h=4000, dt="2020:01:01 10:00:00")
        _add(db, 2, "IMG_1.jpg", w=3000, h=2000, dt="2020:01:01 10:00:00",
             path="/b/IMG_1.jpg")
        _add(db, 3, "IMG_1.jpg", w=800, h=533, dt="2020:01:01 10:00:00",
             path="/c/IMG_1.jpg")  # 1.50 aspect (≈)
        db.commit()
        assert redundant_copy_ids(db) == {2, 3}


# ---------------------------------------------------------------------------
# Rule A — re-encodes & renamed exports (datetime + identical non-junk pHash)
# ---------------------------------------------------------------------------

def test_same_size_reencodes_collapse(tmp_path):
    """Three same-size copies, identical pHash, different sha → keep one."""
    with Database(tmp_path / "p.db") as db:
        ph = "d2dee0369b306d98"
        _add(db, 1, "IMG_1.jpg", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="a", phash=ph, size=8_500_000)
        _add(db, 2, "IMG_1.jpg", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="b", phash=ph, size=8_300_000, path="/b/IMG_1.jpg")
        _add(db, 3, "IMG_1.jpg", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="c", phash=ph, size=8_100_000, path="/c/IMG_1.jpg")
        db.commit()
        # keeper = largest file (1); the other two re-encodes are redundant.
        assert redundant_copy_ids(db) == {2, 3}


def test_drifted_thumbnail_staged_via_stem_aspect(tmp_path):
    """Regression: a tiny thumbnail whose pHash drifted must still be staged.

    The thumbnail (144x95) has a different pHash from the original (too small for
    a stable hash) but shares its filename, capture time and aspect ratio.  An
    earlier 'protected Rule-A keeper' guard wrongly rescued such thumbnails when
    two of them shared a pHash; stem+aspect linking must stage it regardless.
    """
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "DSC_0169.jpg", w=3008, h=2000, dt="2006:08:19 15:42:25",
             sha="a", phash="8013d71c730f9dd3")  # original
        _add(db, 2, "DSC_0169.jpg", w=144, h=95, dt="2006:08:19 15:42:25",
             sha="b", phash="8013d71c734f95d3", path="/t/DSC_0169.jpg")  # thumb, drifted
        _add(db, 3, "DSC_0169.jpg", w=144, h=95, dt="2006:08:19 15:42:25",
             sha="c", phash="8013d71c734f95d3", path="/u/DSC_0169.jpg")  # thumb twin
        db.commit()
        # 144/95=1.5158 vs 3008/2000=1.504 → within aspect tol → all one shot.
        assert redundant_copy_ids(db) == {2, 3}


def test_crop_same_name_and_time_is_kept(tmp_path):
    """A different-aspect derivative (crop) of the same filename/second is kept."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="a", phash="d2dee0369b306d98")          # 3:2 original
        _add(db, 2, "IMG_1.jpg", w=2048, h=1536, dt="2010:02:15 20:11:08",
             sha="b", phash="aaaaaaaaaaaaaaaa", path="/b/IMG_1.jpg")  # 4:3 crop
        db.commit()
        assert redundant_copy_ids(db) == set()


def test_derivative_folder_export_staged(tmp_path):
    """A cropped/stripped-date export in a share/resize folder is staged when a
    same-stem master exists in the same event — even when pHash/date can't place
    it (downscaled crops of different shots collide; share copies lose EXIF date).
    """
    with Database(tmp_path / "p.db") as db:
        ev = "/photos/2012/Event"
        # Masters: full JPEG + RAW under the event (not in a derivative folder).
        _add(db, 1, "IMG_9717.JPG", w=5616, h=3744, dt="2012:05:20 15:47:03",
             sha="a", phash="cb959607c069f3e1", path=f"{ev}/Jpeg/IMG_9717.JPG")
        db.conn.execute(
            "INSERT INTO files (file_id, path, filename, extension, file_type, "
            "status, width, height) VALUES (?,?,?,?,?,?,?,?)",
            (2, f"{ev}/IMG_9717.CR2", "IMG_9717.CR2", "cr2", "RAW", "scanned",
             5616, 3744),
        )
        # Derivatives: cropped (4:3, different aspect) and date-stripped share copy.
        _add(db, 3, "IMG_9717.jpg", w=2048, h=1536, dt="2012:05:20 15:47:03",
             sha="b", phash="da9d9406ca69b3e1", path=f"{ev}/resize+crop/IMG_9717.jpg")
        _add(db, 4, "IMG_9717.jpg", w=2048, h=1536, dt=None,
             sha="c", phash="da9d9406ca7933e1", path=f"{ev}/share/IMG_9717.jpg")
        db.commit()
        # Both derivatives staged; the full JPEG master (1) and RAW (2) are kept.
        assert redundant_copy_ids(db) == {3, 4}


def test_derivative_folder_without_master_kept(tmp_path):
    """An export with NO same-stem master must be kept (could be the only copy)."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "only.jpg", w=2048, h=1536, dt=None,
             phash="abc1230000000000", path="/photos/Event/share/only.jpg")
        db.commit()
        assert redundant_copy_ids(db) == set()


def test_renamed_resize_caught_by_phash(tmp_path):
    """A downscaled export with a DIFFERENT filename but identical pHash."""
    with Database(tmp_path / "p.db") as db:
        ph = "d2dee0369b306d98"
        _add(db, 1, "IMG_1.jpg", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="a", phash=ph)
        _add(db, 2, "image00017.jpg", w=1800, h=1200, dt="2010:02:15 20:11:08",
             sha="b", phash=ph, path="/resize/image00017.jpg")
        db.commit()
        assert redundant_copy_ids(db) == {2}  # renamed resize, caught via pHash


def test_two_shots_same_second_kept_apart(tmp_path):
    """Two different shots taken the same second have different pHash → both kept
    (one keeper each)."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.jpg", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="a", phash="d2dee0369b306d98")
        _add(db, 2, "IMG_2.jpg", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="b", phash="d2dee03e99306d98", path="/b/IMG_2.jpg")
        db.commit()
        assert redundant_copy_ids(db) == set()


def test_junk_phash_not_collapsed(tmp_path):
    """A pHash shared by >= 8 files is non-discriminative → never collapse on it.

    Eight unrelated files share a junk hash AND a capture second; Rule A must not
    merge them.  (Distinct filenames, so Rule B doesn't apply either.)
    """
    with Database(tmp_path / "p.db") as db:
        junk = "0011223344556677"
        for fid in range(1, 9):
            _add(db, fid, f"J{fid}.jpg", w=4000, h=3000,
                 dt="2010:02:15 20:11:08", sha=f"s{fid}", phash=junk,
                 path=f"/a/J{fid}.jpg")
        db.commit()
        assert redundant_copy_ids(db) == set()


def test_raw_never_staged(tmp_path):
    """A RAW master sharing stem/datetime with JPEGs is out of scope — kept."""
    with Database(tmp_path / "p.db") as db:
        _add(db, 1, "IMG_1.JPG", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="a", phash="d2dee0369b306d98")
        _add(db, 2, "IMG_1.JPG", w=5616, h=3744, dt="2010:02:15 20:11:08",
             sha="b", phash="d2dee0369b306d98", path="/b/IMG_1.JPG")
        db.conn.execute(
            "INSERT INTO files (file_id, path, filename, extension, file_type, "
            "status, width, height, datetime_original) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (3, "/a/IMG_1.CR2", "IMG_1.CR2", "cr2", "RAW", "scanned",
             5616, 3744, "2010:02:15 20:11:08"),
        )
        db.commit()
        assert redundant_copy_ids(db) == {2}  # one JPEG staged; RAW (3) untouched


# ---------------------------------------------------------------------------
# plan() integration — redundant copy staged and NOT rescued by the 1d net
# ---------------------------------------------------------------------------

def test_plan_stages_unique_sha_redundant_copy(tmp_path):
    """A redundant copy with its OWN sha (no byte-twin) must still be staged.

    The 1d safety net keeps one copy of each sha256 group; a unique-sha redundant
    copy would be wrongly "rescued" unless exempted as content-safe.  The better
    copy (different sha) is what preserves the content.
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
        assert ops[2] == "STAGE_DELETE", "redundant copy must be staged"
        assert ops[1] == "MOVE", "better copy must be kept and moved"
