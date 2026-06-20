"""
Tests for Subject collection coverage of VIDEO files and the mtime fallback
in `_compute_event_groups` (planner.py).

Two gaps fixed here:
  1. VIDEO was excluded from `_compute_event_groups` and only trusted EXIF
     dates — a real case (`2010-2012 愷 1~2 歲 videos`, no EXIF/QuickTime
     metadata at all) was scattered into Videos/2010, /2011, /2012 instead of
     being kept together as a Subject collection.
  2. `_build_target_path`'s VIDEO branch never consulted `event_groups`.

Side effect under test: switching the date source from `_parse_exif_dt` to
`_effective_date` (EXIF, else mtime) means mtime-only PHOTOS now also enter
grouping — previously they were skipped entirely. Locked down by the
regression/boundary/single-day tests below.

mtime values use ISO-8601 (`_parse_mtime` requirement), not EXIF format.

Run: python -m pytest tests/test_subject_collection.py
"""

from __future__ import annotations

from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.planner import _build_target_path, _compute_event_groups

KNOWN_CAMERA = "TestCam"


def _add_file(
    db, path, *, file_type="CAMERA_JPEG", status="hashed",
    sha256=None, phash=None, camera_model=None,
    datetime_original=None, mtime=None, filename=None,
    width=4000, height=3000, size_bytes=1000,
) -> int:
    p = Path(path)
    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status, "
        "sha256, phash, camera_model, datetime_original, mtime, width, "
        "height, size_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(p), filename or p.name, p.suffix.lstrip(".").lower(), file_type,
         status, sha256, phash, camera_model, datetime_original, mtime,
         width, height, size_bytes),
    )
    return cur.lastrowid


def _row(db, file_id):
    return db.conn.execute(
        "SELECT * FROM files WHERE file_id=?", (file_id,)
    ).fetchone()


def test_video_subject_collection_2010_2012(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Library"
    folder = tmp_path / "src" / "2010-2012 愷 1~2 歲 videos"

    with Database(db_path) as db:
        v1 = _add_file(db, folder / "MOV001.mp4", file_type="VIDEO",
                       mtime="2010-03-01T10:00:00+00:00")
        v2 = _add_file(db, folder / "MOV002.mp4", file_type="VIDEO",
                       mtime="2011-06-15T10:00:00+00:00")
        v3 = _add_file(db, folder / "MOV003.mp4", file_type="VIDEO",
                       mtime="2012-09-20T10:00:00+00:00")
        db.commit()

        groups = _compute_event_groups(db, set())
        key = str(folder)
        assert key in groups
        assert groups[key]["kind"] == "subject"
        assert groups[key]["label"] == "愷_1_2_歲_videos"

        t1 = _build_target_path(_row(db, v1), target, set(), {}, groups)
        t2 = _build_target_path(_row(db, v2), target, set(), {}, groups)
        t3 = _build_target_path(_row(db, v3), target, set(), {}, groups)

        # V3: videos co-locate inside the event/subject folder's own Videos/
        # subfolder (base defaults to "Others" — no event_base map passed here,
        # and no known camera).
        assert t1.parent == target / "Others" / "愷_1_2_歲_videos" / "2010" / "Videos"
        assert t2.parent == target / "Others" / "愷_1_2_歲_videos" / "2011" / "Videos"
        assert t3.parent == target / "Others" / "愷_1_2_歲_videos" / "2012" / "Videos"


def test_mtime_only_photo_now_enters_grouping_regression(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    folder = tmp_path / "src" / "GreeceTrip"

    with Database(db_path) as db:
        _add_file(db, folder / "a.jpg", mtime="2015-01-01T10:00:00+00:00")
        _add_file(db, folder / "b.jpg", mtime="2015-04-01T10:00:00+00:00")
        db.commit()

        groups = _compute_event_groups(db, set())
        key = str(folder)
        assert key in groups
        assert groups[key]["kind"] == "subject"


def test_mtime_only_boundary_2_to_30_days_is_event(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    folder = tmp_path / "src" / "Wedding"

    with Database(db_path) as db:
        _add_file(db, folder / "a.jpg", mtime="2015-06-01T10:00:00+00:00")
        _add_file(db, folder / "b.jpg", mtime="2015-06-10T10:00:00+00:00")
        db.commit()

        groups = _compute_event_groups(db, set())
        key = str(folder)
        assert key in groups
        assert groups[key]["kind"] == "event"
        assert groups[key]["start"].isoformat() == "2015-06-01"
        assert groups[key]["span"] == 10


def test_mtime_only_single_day_omitted_from_groups(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    folder = tmp_path / "src" / "SingleDay"

    with Database(db_path) as db:
        _add_file(db, folder / "a.jpg", mtime="2015-06-01T08:00:00+00:00")
        _add_file(db, folder / "b.jpg", mtime="2015-06-01T20:00:00+00:00")
        db.commit()

        groups = _compute_event_groups(db, set())
        assert str(folder) not in groups


def test_video_event_kind_unaffected_existing_path(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Library"
    folder = tmp_path / "src" / "Wedding"

    with Database(db_path) as db:
        v1 = _add_file(db, folder / "MOV001.mp4", file_type="VIDEO",
                       mtime="2015-06-01T10:00:00+00:00")
        _add_file(db, folder / "MOV002.mp4", file_type="VIDEO",
                  mtime="2015-06-10T10:00:00+00:00")
        db.commit()

        groups = _compute_event_groups(db, set())
        key = str(folder)
        assert groups[key]["kind"] == "event"

        # V3: no event_groups passed → falls to the single-day folder shape,
        # but still co-locates under the resolved event folder's Videos/.
        t1 = _build_target_path(_row(db, v1), target, set(), {})
        assert t1.parent == target / "Others" / "2015" / "2015-06-01_Wedding" / "Videos"
        assert t1.name.startswith("2015-06-01_")

        # Unresolved/no-group video (no datetime at all) → Videos/NoDate/.
        none_id = _add_file(db, tmp_path / "src" / "Misc" / "x.mp4",
                            file_type="VIDEO")
        t_none = _build_target_path(_row(db, none_id), target, set(), {})
        assert t_none.parent == target / "Videos" / "NoDate"


def test_video_subject_nodate_falls_to_standalone_nodate(tmp_path):
    # V3: a video with NO date of its own can't co-locate (no date → no event
    # subdir to place it in), even when siblings establish the folder as a
    # subject collection — it falls to the standalone Videos/NoDate/ tree.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Library"
    folder = tmp_path / "src" / "2010-2012 愷 1~2 歲 videos"

    with Database(db_path) as db:
        v1 = _add_file(db, folder / "MOV001.mp4", file_type="VIDEO",
                       mtime="2010-03-01T10:00:00+00:00")
        v2 = _add_file(db, folder / "MOV002.mp4", file_type="VIDEO",
                       mtime="2012-09-20T10:00:00+00:00")
        # This one has NO date of its own (no EXIF, no mtime) but its siblings
        # establish the folder as a subject collection.
        v_nodate = _add_file(db, folder / "MOV003.mp4", file_type="VIDEO")
        db.commit()

        groups = _compute_event_groups(db, set())
        key = str(folder)
        assert groups[key]["kind"] == "subject"

        t_nodate = _build_target_path(_row(db, v_nodate), target, set(), {}, groups)
        assert t_nodate.parent == target / "Videos" / "NoDate"
        assert v1 and v2  # siblings exist, just asserting setup sanity


def test_stage_ids_excluded_from_grouping_still_works(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    folder = tmp_path / "src" / "Excluded"

    with Database(db_path) as db:
        staged = _add_file(db, folder / "a.jpg", mtime="2015-01-01T10:00:00+00:00")
        _add_file(db, folder / "b.jpg", mtime="2015-04-01T10:00:00+00:00")
        db.commit()

        groups = _compute_event_groups(db, {staged})
        key = str(folder)
        # Only one non-staged file remains → span collapses to a single day,
        # so the folder doesn't even reach grouping.
        assert key not in groups
