"""Tests for V3: video event co-location.

Videos used to land in a flat Videos/{YYYY}/ tree, separated from their
event's photos. V3 co-locates a DATED video inside the SAME event folder its
photos use, in a Videos/ subfolder of that folder. Base (Masters/Others)
follows the event's photos (`_event_base_map`: any known-camera photo in the
event -> Masters, else Others), not the video's own camera — except when the
video's folder doesn't resolve to an event at all (camera-dump folder), where
there are no event photos to inherit a base from, so it falls back to the
video's own camera.

A video with NO date can't be placed in any event subdir, so it keeps the
standalone Videos/NoDate/ tree.
"""
from __future__ import annotations

from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.planner import (
    _build_target_path,
    _event_base_map,
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


def _row(db, path):
    return db.conn.execute(
        "SELECT * FROM files WHERE path=?", (path,)
    ).fetchone()


def _row_by_id(db, file_id):
    return db.conn.execute(
        "SELECT * FROM files WHERE file_id=?", (file_id,)
    ).fetchone()


def _set_override(db, source_folder, *, event_name=None, date_override=None):
    db.set_folder_override(
        source_folder, event_name=event_name, date_override=date_override,
        updated_at="2026-06-20T00:00:00+00:00",
    )
    db.commit()


# ---------------------------------------------------------------------------
# 1. Single-day event co-location
# ---------------------------------------------------------------------------

def test_video_single_day_event_colocated(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder = r"D:\Trips\Kyoto"
    with Database(db_path) as db:
        video_path = folder + r"\MVI_0001.mp4"
        _add_file(
            db, video_path, file_type="VIDEO",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        row = _row(db, video_path)

    resolved_key = str(Path(folder))
    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        event_base={resolved_key: "Masters"},
    )
    expected = (
        target / "Masters" / "2012" / "2012-09-08 Kyoto" / "Videos"
        / "2012-09-08_0000.MP4"
    )
    assert result == expected


# ---------------------------------------------------------------------------
# 2. Multi-day event co-location
# ---------------------------------------------------------------------------

def test_video_multiday_event_colocated(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder = r"D:\Trips\Kyoto"
    with Database(db_path) as db:
        video_path = folder + r"\MVI_0002.mp4"
        _add_file(
            db, video_path, file_type="VIDEO",
            datetime_original="2012:09:09 10:00:00", date_confidence="HIGH",
        )
        row = _row(db, video_path)

    from datetime import date as _date
    resolved_key = str(Path(folder))
    group = {resolved_key: {"kind": "event", "start": _date(2012, 9, 8), "span": 3}}
    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups=group,
        event_base={resolved_key: "Masters"},
    )
    expected = (
        target / "Masters" / "2012" / "2012-09-08(3d) Kyoto" / "Videos"
        / "2012-09-09_0000.MP4"
    )
    assert result == expected


# ---------------------------------------------------------------------------
# 3. Subject collection co-location
# ---------------------------------------------------------------------------

def test_video_subject_colocated(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder = r"D:\Kids\愷"
    with Database(db_path) as db:
        video_path = folder + r"\MOV001.mp4"
        _add_file(
            db, video_path, file_type="VIDEO",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        row = _row(db, video_path)

    resolved_key = str(Path(folder))
    group = {resolved_key: {"kind": "subject", "label": "愷"}}
    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups=group,
        event_base={resolved_key: "Masters"},
    )
    expected = (
        target / "Masters" / "愷" / "2012" / "Videos" / "2012-09-08_0000.MP4"
    )
    assert result == expected


# ---------------------------------------------------------------------------
# 4. Base follows the event_base map (Masters vs Others)
# ---------------------------------------------------------------------------

def test_video_base_follows_event_masters(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder_masters = r"D:\Trips\Kyoto"
    folder_others = r"D:\Trips\Osaka"
    with Database(db_path) as db:
        v1_path = folder_masters + r"\MVI_0001.mp4"
        v2_path = folder_others + r"\MVI_0002.mp4"
        _add_file(
            db, v1_path, file_type="VIDEO",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        _add_file(
            db, v2_path, file_type="VIDEO",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        row_masters = _row(db, v1_path)
        row_others = _row(db, v2_path)

    event_base = {
        str(Path(folder_masters)): "Masters",
        str(Path(folder_others)): "Others",
    }
    result_masters = _build_target_path(
        row_masters, target, known_cameras=set(), counters={}, event_groups={},
        event_base=event_base,
    )
    result_others = _build_target_path(
        row_others, target, known_cameras=set(), counters={}, event_groups={},
        event_base=event_base,
    )
    assert "Masters" in str(result_masters)
    assert "Others" not in str(result_masters)
    assert "Others" in str(result_others)
    assert "Masters" not in str(result_others)


# ---------------------------------------------------------------------------
# 5. _event_base_map: any known-camera photo in the event wins Masters
# ---------------------------------------------------------------------------

def test_event_base_map_any_known_camera_wins(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        known_folder = r"D:\Trips\Kyoto"
        unknown_folder = r"D:\Trips\Osaka"

        # Kyoto: one known-camera photo + one unknown-camera photo -> Masters.
        _add_file(
            db, known_folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        _add_file(
            db, known_folder + r"\IMG_0002.jpg",
            datetime_original="2012:09:08 11:00:00", date_confidence="HIGH",
            camera_model="Unknown Phone X",
        )

        # Osaka: only unknown-camera photos -> Others.
        _add_file(
            db, unknown_folder + r"\IMG_0003.jpg",
            datetime_original="2012:09:09 10:00:00", date_confidence="HIGH",
            camera_model="Unknown Phone X",
        )

        known_cameras = {"canon eos 5d"}
        base_map = _event_base_map(db, known_cameras)

    assert base_map[str(Path(known_folder))] == "Masters"
    assert base_map[str(Path(unknown_folder))] == "Others"


# ---------------------------------------------------------------------------
# 6. No resolved event (camera-dump folder), dated -> base by video's own camera
# ---------------------------------------------------------------------------

def test_video_no_event_dated_colocates_by_own_camera(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder = r"D:\DCIM\100EOS5D"  # camera-dump folder name -> resolves to None
    with Database(db_path) as db:
        video_path = folder + r"\MVI_0001.mp4"
        _add_file(
            db, video_path, file_type="VIDEO",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Unknown Cam",
        )
        row = _row(db, video_path)

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        event_base={},
    )
    expected = (
        target / "Others" / "2012" / "2012-09-08" / "Videos"
        / "2012-09-08_0000.MP4"
    )
    assert result == expected


# ---------------------------------------------------------------------------
# 7. No date -> standalone Videos/NoDate/
# ---------------------------------------------------------------------------

def test_video_nodate_standalone(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder = r"D:\Trips\Kyoto"
    with Database(db_path) as db:
        video_path = folder + r"\MVI_0001.mp4"
        _add_file(db, video_path, file_type="VIDEO", datetime_original=None)
        row = _row(db, video_path)

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        event_base={str(Path(folder)): "Masters"},
    )
    expected = target / "Videos" / "NoDate" / "video_0000.MP4"
    assert result == expected


# ---------------------------------------------------------------------------
# 8. Sequence numbers don't collide
# ---------------------------------------------------------------------------

def test_video_seq_no_collision(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder = r"D:\Trips\Kyoto"
    with Database(db_path) as db:
        v1_path = folder + r"\MVI_0001.mp4"
        v2_path = folder + r"\MVI_0002.mp4"
        _add_file(
            db, v1_path, file_type="VIDEO",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        _add_file(
            db, v2_path, file_type="VIDEO",
            datetime_original="2012:09:08 11:00:00", date_confidence="HIGH",
        )
        row1 = _row(db, v1_path)
        row2 = _row(db, v2_path)

    counters: dict = {}
    event_base = {str(Path(folder)): "Masters"}
    result1 = _build_target_path(
        row1, target, known_cameras=set(), counters=counters, event_groups={},
        event_base=event_base,
    )
    result2 = _build_target_path(
        row2, target, known_cameras=set(), counters=counters, event_groups={},
        event_base=event_base,
    )
    assert result1.name == "2012-09-08_0000.MP4"
    assert result2.name == "2012-09-08_0001.MP4"
    assert result1.parent == result2.parent


# ---------------------------------------------------------------------------
# 9. Folder-level event override applies to co-located videos too
# ---------------------------------------------------------------------------

def test_video_event_override_colocated(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder = r"D:\DCIM\100EOS5D"
    with Database(db_path) as db:
        video_path = folder + r"\MVI_0001.mp4"
        _add_file(
            db, video_path, file_type="VIDEO",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        _set_override(db, folder, event_name="上海")
        row = _row(db, video_path)
        overrides = db.get_folder_overrides()

    result = _build_target_path(
        row, target, known_cameras=set(), counters={}, event_groups={},
        overrides=overrides, event_base={},
    )
    assert str(Path("2012-09-08 上海") / "Videos") in str(result)


# ---------------------------------------------------------------------------
# 10. Photo paths unchanged after the _event_subdir refactor
# ---------------------------------------------------------------------------

def test_photo_paths_unchanged_after_refactor(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"

    with Database(db_path) as db:
        # Single-day event.
        single_folder = r"D:\Trips\Kyoto"
        single_path = single_folder + r"\IMG_0001.jpg"
        _add_file(
            db, single_path,
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        single_row = _row(db, single_path)

        # Subject collection.
        subject_folder = r"D:\Kids\愷"
        subject_path = subject_folder + r"\IMG_0002.jpg"
        _add_file(
            db, subject_path,
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        subject_row = _row(db, subject_path)

    known_cameras = {"canon eos 5d"}

    single_result = _build_target_path(
        single_row, target, known_cameras=known_cameras, counters={},
        event_groups={},
    )
    assert single_result == (
        target / "Masters" / "2012" / "2012-09-08 Kyoto" / "IMG_0001.jpg"
    )

    subject_group = {str(Path(subject_folder)): {"kind": "subject", "label": "愷"}}
    subject_result = _build_target_path(
        subject_row, target, known_cameras=known_cameras, counters={},
        event_groups=subject_group,
    )
    assert subject_result == (
        target / "Masters" / "愷" / "2012" / "IMG_0002.jpg"
    )


# ---------------------------------------------------------------------------
# 11. Full plan() end-to-end: video co-locates with its event's photos
# ---------------------------------------------------------------------------

def test_plan_video_colocation_end_to_end(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    folder = r"D:\Trips\Kyoto"
    with Database(db_path) as db:
        db.add_known_camera("Canon", "Canon EOS 5D")
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        _add_file(
            db, folder + r"\MVI_0001.mp4", file_type="VIDEO",
            datetime_original="2012:09:08 10:30:00", date_confidence="HIGH",
        )

        plan(db, target, assume_yes=True)

        op = db.conn.execute(
            "SELECT target_path FROM operations WHERE op_type='MOVE' "
            "AND target_path LIKE '%.MP4'"
        ).fetchone()
        assert op is not None
        target_path = op["target_path"]

    assert "Masters" in target_path
    assert "2012-09-08 Kyoto" in target_path
    assert "Videos" in target_path
