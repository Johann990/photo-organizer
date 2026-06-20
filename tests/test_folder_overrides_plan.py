"""Tests for O2: planner consults folder_overrides (event label + LOW-date override)."""
from __future__ import annotations

from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.planner import (
    _build_target_path,
    _resolve_date,
    _sibling_date_hints,
    plan,
)


def _add_file(
    db,
    path,
    *,
    sha=None,
    status="scanned",
    file_type="CAMERA_JPEG",
    datetime_original=None,
    date_confidence=None,
    camera_model=None,
    mtime=None,
):
    filename = path.split("\\")[-1]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    db.conn.execute(
        "INSERT INTO files "
        "(path, filename, extension, sha256, status, file_type, size_bytes, "
        " datetime_original, date_confidence, camera_model, mtime) "
        "VALUES (?, ?, ?, ?, ?, ?, 1000, ?, ?, ?, ?)",
        (
            path, filename, ext, sha, status, file_type,
            datetime_original, date_confidence, camera_model, mtime,
        ),
    )
    db.commit()
    return db.conn.execute(
        "SELECT file_id FROM files WHERE path=?", (path,)
    ).fetchone()[0]


def _set_override(db, source_folder, *, event_name=None, date_override=None):
    db.set_folder_override(
        source_folder, event_name=event_name, date_override=date_override,
        updated_at="2026-06-20T00:00:00+00:00",
    )
    db.commit()


def test_event_override_photo(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        _set_override(db, folder, event_name="上海")
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\IMG_0001.jpg",)
        ).fetchone()
        overrides = db.get_folder_overrides()

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides,
    )
    assert "_上海" in str(result)
    assert str(Path("2012") / "2012-09-08_上海") in str(result)


def test_event_override_video(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        _add_file(
            db, folder + r"\MVI_0001.mp4",
            file_type="VIDEO",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        _set_override(db, folder, event_name="上海")
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\MVI_0001.mp4",)
        ).fetchone()
        overrides = db.get_folder_overrides()

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides,
    )
    # V3: video co-locates inside the event folder's own Videos/ subfolder
    # (base defaults to "Others" — no event_base map / known camera here).
    assert str(Path("2012-09-08_上海") / "Videos") in str(result)
    assert "2012-09-08_0000" in str(result)


def test_date_override_applies_to_low(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Phone\Dump"
        _add_file(
            db, folder + r"\photo001.jpg",
            datetime_original="2023:01:01 00:00:00", date_confidence="LOW",
        )
        _set_override(db, folder, date_override="2015-04-02")
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\photo001.jpg",)
        ).fetchone()
        overrides = db.get_folder_overrides()

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides,
    )
    assert "2015" in str(result)
    assert "2023" not in str(result)


def test_date_override_skips_high(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Phone\Dump"
        _add_file(
            db, folder + r"\photo001.jpg",
            datetime_original="2023:06:15 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        _set_override(db, folder, date_override="2015-04-02")
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\photo001.jpg",)
        ).fetchone()
        overrides = db.get_folder_overrides()

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides,
    )
    # SAFETY RULE: HIGH confidence dates are never overridden.
    assert "2023" in str(result)
    assert "2015" not in str(result)


def test_date_override_skips_medium(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Phone\Dump"
        _add_file(
            db, folder + r"\photo001.jpg",
            datetime_original="2023:06:15 10:00:00", date_confidence="MEDIUM",
        )
        _set_override(db, folder, date_override="2015-04-02")
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\photo001.jpg",)
        ).fetchone()
        overrides = db.get_folder_overrides()

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides,
    )
    # SAFETY RULE: MEDIUM confidence dates are never overridden.
    assert "2023" in str(result)
    assert "2015" not in str(result)


def test_no_overrides_unchanged(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Trips\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\IMG_0001.jpg",)
        ).fetchone()
        empty_overrides = db.get_folder_overrides()  # no rows inserted

    assert empty_overrides == {}

    result_with_empty = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=empty_overrides,
    )
    result_without = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
    )
    assert result_with_empty == result_without


def test_plan_applies_override_end_to_end(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        # No camera_model / no filename-date match / EXIF far from filename-less
        # signal → date forensics ladder falls through to mtime, LOW confidence.
        _add_file(
            db, folder + r"\photo001.jpg",
            datetime_original=None,
            mtime="2023-01-01T00:00:00+00:00",
        )
        _set_override(db, folder, event_name="上海", date_override="2015-04-02")

        plan(db, target, assume_yes=True)

        op = db.conn.execute(
            "SELECT target_path FROM operations WHERE op_type='MOVE'"
        ).fetchone()
        assert op is not None
        target_path = op["target_path"]

    assert "上海" in target_path
    assert "2015" in target_path
    assert "2023" not in target_path


def test_malformed_date_override_falls_back(tmp_path):
    # A garbage date_override must not crash; the file keeps its real date.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Phone\Dump"
        _add_file(
            db, folder + r"\photo001.jpg",
            datetime_original="2023:01:01 00:00:00", date_confidence="LOW",
        )
        _set_override(db, folder, date_override="2015-13-99")  # invalid
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\photo001.jpg",)
        ).fetchone()
        overrides = db.get_folder_overrides()

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides,
    )
    # malformed override ignored → original LOW date (2023) kept, no crash
    assert "2023" in str(result)
    assert "2015" not in str(result)


def test_event_name_sanitizing_to_empty_falls_back(tmp_path):
    # An event_name that sanitizes to "" must fall back to the resolved label,
    # never produce an empty/broken folder segment.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Trips\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        _set_override(db, folder, event_name="2012")  # leading-year → stripped to ""
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\IMG_0001.jpg",)
        ).fetchone()
        overrides = db.get_folder_overrides()
        baseline = _build_target_path(
            row, target, known_cameras=set(), counters={}, event_groups={},
        )

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides,
    )
    # override sanitized to empty → identical to no override (resolved label kept)
    assert result == baseline


def test_override_on_folder_with_no_files_is_noop(tmp_path):
    # An override row whose source_folder matches no file's parent is harmless.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Trips\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        _set_override(db, r"D:\Some\Other\Folder", event_name="上海",
                      date_override="2015-04-02")
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\IMG_0001.jpg",)
        ).fetchone()
        overrides = db.get_folder_overrides()
        baseline = _build_target_path(
            row, target, known_cameras=set(), counters={}, event_groups={},
        )

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides,
    )
    assert result == baseline


def test_event_override_subject_collection(tmp_path):
    # A >30-day named (subject) folder must also honor the event_name override
    # (regression for the subject branch shadowing _ov_event).
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Kids\愷"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        _set_override(db, folder, event_name="愷成長")
        row = db.conn.execute(
            "SELECT * FROM files WHERE path=?", (folder + r"\IMG_0001.jpg",)
        ).fetchone()
        overrides = db.get_folder_overrides()

    # Force this file's resolved folder into a subject group.
    resolved = str(Path(folder))
    event_groups = {resolved: {"kind": "subject", "label": "愷"}}
    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups=event_groups,
        overrides=overrides,
    )
    # override label "愷成長" wins over the auto subject label "愷"
    assert str(Path("愷成長") / "2012") in str(result)


# ---------------------------------------------------------------------------
# V2: trust video filename dates + borrow sibling-photo date
# ---------------------------------------------------------------------------

def test_video_filename_date_is_medium(tmp_path):
    # A VIDEO with no EXIF but a sane filename date is the camera's own
    # capture timestamp (videos carry no EXIF to contradict it) → MEDIUM,
    # not LOW. mtime is in a different year and must NOT win.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\Camera\Clips"
        fid = _add_file(
            db, folder + r"\VID_20120908_143000.mp4",
            file_type="VIDEO",
            datetime_original=None,
            mtime="2023-01-01T00:00:00+00:00",
        )
        row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (fid,)
        ).fetchone()

    dt, source, confidence = _resolve_date(row)
    assert source == "filename"
    assert confidence == "MEDIUM"
    assert dt.date().isoformat() == "2012-09-08"


def test_photo_filename_date_still_low(tmp_path):
    # Same situation but for a photo — filename-only dates stay LOW; photo
    # behavior must be unchanged by the video-specific rung 3b branch.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\Camera\Clips"
        fid = _add_file(
            db, folder + r"\IMG_20120908_143000.jpg",
            file_type="CAMERA_JPEG",
            datetime_original=None,
            mtime="2023-01-01T00:00:00+00:00",
        )
        row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (fid,)
        ).fetchone()

    dt, source, confidence = _resolve_date(row)
    assert source == "filename"
    assert confidence == "LOW"
    assert dt.date().isoformat() == "2012-09-08"


def test_video_borrows_sibling_photo_date(tmp_path):
    # A folder with a confidently-dated (HIGH) photo and a date-less video
    # (no filename date either) → the video borrows the folder's photo date.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Trips\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        video_id = _add_file(
            db, folder + r"\MOV001.mov",
            file_type="VIDEO",
            datetime_original=None, date_confidence="LOW",
            mtime="2023-01-01T00:00:00+00:00",
        )
        video_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (video_id,)
        ).fetchone()
        hints = _sibling_date_hints(db)

    result = _build_target_path(
        video_row, target, known_cameras=set(), counters={}, event_groups={},
        sibling_hints=hints,
    )
    assert "2012" in str(result)
    assert "2023" not in str(result)


def test_video_filename_beats_sibling(tmp_path):
    # A video with its OWN filename date (→ MEDIUM via rung 3b) must keep that
    # date and NOT borrow from siblings, even when confident sibling photos
    # exist with a different date. Own evidence beats borrowed inference.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Trips\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2015:04:02 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        video_id = _add_file(
            db, folder + r"\VID_20120908_143000.mp4",
            file_type="VIDEO",
            datetime_original=None, date_confidence="MEDIUM",  # as audit sets it
            mtime="2023-01-01T00:00:00+00:00",
        )
        video_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (video_id,)
        ).fetchone()
        hints = _sibling_date_hints(db)

    result = _build_target_path(
        video_row, target, known_cameras=set(), counters={}, event_groups={},
        sibling_hints=hints,
    )
    assert "2012" in str(result)       # own filename date wins
    assert "2015" not in str(result)   # not the sibling photo date


def test_video_no_sibling_keeps_own_date(tmp_path):
    # A video-only folder (no photos) has no sibling hint → the video keeps
    # its own (mtime) date.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Camera\VideosOnly"
        video_id = _add_file(
            db, folder + r"\MOV002.mov",
            file_type="VIDEO",
            datetime_original=None, date_confidence="LOW",
            mtime="2023-01-01T00:00:00+00:00",
        )
        video_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (video_id,)
        ).fetchone()
        hints = _sibling_date_hints(db)

    assert str(Path(folder)) not in hints

    result = _build_target_path(
        video_row, target, known_cameras=set(), counters={}, event_groups={},
        sibling_hints=hints,
    )
    assert "2023" in str(result)


def test_override_beats_sibling(tmp_path):
    # A folder has both confident photos (2012) AND a user date_override
    # (2015-04-02). The explicit override wins over the sibling-photo borrow.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Trips\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        video_id = _add_file(
            db, folder + r"\MOV001.mov",
            file_type="VIDEO",
            datetime_original=None, date_confidence="LOW",
            mtime="2023-01-01T00:00:00+00:00",
        )
        _set_override(db, folder, date_override="2015-04-02")
        video_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (video_id,)
        ).fetchone()
        overrides = db.get_folder_overrides()
        hints = _sibling_date_hints(db)

    result = _build_target_path(
        video_row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides, sibling_hints=hints,
    )
    assert "2015" in str(result)
    assert "2012" not in str(result)
    assert "2023" not in str(result)


def test_sibling_does_not_touch_photos(tmp_path):
    # Sibling-photo borrowing applies ONLY to videos. A LOW-confidence photo
    # in a folder with other confident photos must keep its own resolved date.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Trips\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        low_photo_id = _add_file(
            db, folder + r"\photo002.jpg",
            file_type="CAMERA_JPEG",
            datetime_original="2023:06:15 10:00:00", date_confidence="LOW",
            mtime="2023-06-15T10:00:00+00:00",
        )
        low_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (low_photo_id,)
        ).fetchone()
        hints = _sibling_date_hints(db)

    baseline = _build_target_path(
        low_row, target, known_cameras=set(), counters={}, event_groups={},
    )
    result = _build_target_path(
        low_row, target, known_cameras=set(), counters={}, event_groups={},
        sibling_hints=hints,
    )
    # sibling hints exist for this folder (the 2012 HIGH photo) but must not
    # be applied to a photo — only videos borrow.
    assert str(Path(folder)) in hints
    assert result == baseline
    assert "2023" in str(result)


def test_plan_video_date_end_to_end(tmp_path):
    # Full plan(): a folder with confident 2012 photos plus an mtime-2023
    # video → the video's MOVE op target lands in 2012, not 2023.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    with Database(db_path) as db:
        folder = r"D:\Trips\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        _add_file(
            db, folder + r"\MOV001.mov",
            file_type="VIDEO",
            datetime_original=None,
            mtime="2023-01-01T00:00:00+00:00",
        )

        plan(db, target, assume_yes=True)

        op = db.conn.execute(
            "SELECT target_path FROM operations WHERE op_type='MOVE' "
            "AND target_path LIKE '%Videos%'"
        ).fetchone()
        assert op is not None
        target_path = op["target_path"]

    assert "Videos" in target_path
    assert "2012" in target_path
    assert "2023" not in target_path
