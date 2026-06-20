"""Tests for O2: planner consults folder_overrides (event label + LOW-date override)."""
from __future__ import annotations

from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.planner import _build_target_path, plan


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
    assert "Videos" in str(result)
    assert "2012-09-08_上海_0000" in str(result)


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
