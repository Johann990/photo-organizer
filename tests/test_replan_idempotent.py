"""
Tests for the target_root boundary in `_resolve_event_folder`, and the
companion `video`/`videos` container-name fix.

Real incident this guards against: `relocate` re-pointed `files.path` for
already-organized files back to their in-target-tree location without
marking them 'done'; a subsequent `plan` then climbed from that in-tree path
looking for an event/subject label and — finding only date folders below it —
incorrectly latched onto the planner's OWN structural folder name
(Masters/Others/NoDate) as if it were a real label, nesting the base inside
itself (`Masters/Masters/2020/...`, `Others/Others/2020/...`). A SEPARATE but
similarly-shaped bug hit videos specifically: a video's own per-event
`Videos/` subfolder (created by the planner for every event, and common in
source libraries too) was never in the container blocklist, so climbing from
a video stopped at "Videos" immediately instead of continuing up to the real
event/subject above it (`Others/Videos/2020/Videos/...` instead of
`Masters/Joip8/2020/Videos/...`).

Run: python -m pytest tests/test_replan_idempotent.py
"""
from __future__ import annotations

from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.planner import (
    _build_target_path,
    _compute_event_groups,
    _event_base_map,
    _is_container_or_device,
    _resolve_event_folder,
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
    return db.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()


# ---------------------------------------------------------------------------
# 1. _resolve_event_folder: target_root boundary (pure path, no DB)
# ---------------------------------------------------------------------------

def test_resolve_stops_at_masters_under_target_root():
    target = Path("D:/Media")
    p = target / "Masters" / "2020" / "2020-10-25" / "foo.jpg"
    assert _resolve_event_folder(p, target_root=target) is None


def test_resolve_stops_at_others_under_target_root():
    target = Path("D:/Media")
    p = target / "Others" / "2020" / "2020-09-20" / "foo.jpg"
    assert _resolve_event_folder(p, target_root=target) is None


def test_resolve_stops_at_nodate_under_target_root():
    target = Path("D:/Media")
    p = target / "NoDate" / "foo.jpg"
    assert _resolve_event_folder(p, target_root=target) is None


def test_resolve_real_event_below_target_root_still_resolves():
    # The boundary only fires when NOTHING usable survives below it — a real
    # event/subject folder beneath Masters/Others is still found normally.
    target = Path("D:/Media")
    p = target / "Masters" / "2020" / "2020-09-08 Kyoto" / "foo.jpg"
    assert _resolve_event_folder(p, target_root=target) == (
        target / "Masters" / "2020" / "2020-09-08 Kyoto"
    )


def test_resolve_without_target_root_reproduces_prior_unbounded_climb():
    # Documents WHY the param is needed: the default (None) preserves the
    # old unbounded behaviour, so a caller that forgets to pass target_root
    # would still mislabel Masters as an event — exactly the reported bug.
    target = Path("D:/Media")
    p = target / "Masters" / "2020" / "2020-10-25" / "foo.jpg"
    assert _resolve_event_folder(p) == target / "Masters"


# ---------------------------------------------------------------------------
# 2. _is_container_or_device: video / videos (the second, independent bug)
# ---------------------------------------------------------------------------

def test_video_container_words_blocked():
    for word in ("video", "videos", "Videos", "VIDEOS"):
        assert _is_container_or_device(word) is True, word


def test_resolve_skips_videos_subfolder_to_find_real_event():
    # A video's own per-event Videos/ subfolder must never be mistaken for
    # the event itself — climb past it to the real event/subject above.
    p = Path("D:/Media/Masters/Joip8/2020/Videos/foo.mp4")
    assert _resolve_event_folder(p) == Path("D:/Media/Masters/Joip8")


# ---------------------------------------------------------------------------
# 3. End-to-end idempotency: replanning an already-organized no-event file
#    must reproduce the SAME destination, not nest Masters/Masters.
# ---------------------------------------------------------------------------

def test_build_target_path_idempotent_for_already_organized_nodate_file():
    target = Path("D:/Media")
    row = {
        "path": str(target / "Masters" / "2020" / "2020-10-25" / "foo.jpg"),
        "filename": "foo.jpg",
        "extension": "jpg",
        "file_type": "CAMERA_JPEG",
        "datetime_original": "2020:10:25 16:37:12",
        "datetime_digitized": None,
        "mtime": None,
        "camera_model": "iPhone SE",
    }
    result = _build_target_path(row, target, {"iphone se"}, {})
    assert result == target / "Masters" / "2020" / "2020-10-25" / "foo.jpg"
    assert "Masters\\Masters" not in str(result)


def test_replan_no_event_subject_span_does_not_double_nest(tmp_path):
    """Full incident repro: 3 already-organized no-event photos under
    Masters/2020/{date}/, spanning >30 days (the real case spanned
    2020-01-04..2020-10-25). Without the target_root boundary,
    `_compute_event_groups`/`_event_base_map` would resolve all three to the
    "Masters" folder itself, classify it as a >30-day subject, and replan
    every file into Masters/Masters/2020/ — the exact reported bug."""
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    dates = ["2020-01-04", "2020-09-20", "2020-10-25"]
    with Database(db_path) as db:
        for i, d in enumerate(dates):
            path = str(target / "Masters" / "2020" / d / f"foo{i}.jpg")
            _add_file(
                db, path,
                datetime_original=d.replace("-", ":") + " 12:00:00",
                date_confidence="HIGH", camera_model="iPhone SE",
            )

        event_groups = _compute_event_groups(db, set(), target_root=target)
        event_base = _event_base_map(db, {"iphone se"}, target_root=target)

        # Nothing resolves to a label -> no spurious subject group/base entry.
        assert event_groups == {}
        assert event_base == {}

        for i, d in enumerate(dates):
            row = _row(db, str(target / "Masters" / "2020" / d / f"foo{i}.jpg"))
            result = _build_target_path(
                row, target, {"iphone se"}, {},
                event_groups=event_groups, event_base=event_base,
            )
            expected = target / "Masters" / "2020" / d / f"foo{i}.jpg"
            assert result == expected
            assert "Masters\\Masters" not in str(result)


def test_replan_video_in_subject_folder_colocates_not_videos_videos(tmp_path):
    """Full incident repro (the video variant): a known-camera photo and a
    video both already live under Masters/Joip8/2020/ (a >30-day subject),
    the video one level deeper in its own Videos/ subfolder. Replanning must
    keep the video co-located under Joip8 with base=Masters (inherited from
    the photo) — not resolve the video's own Videos/ folder as the subject
    and fall back to base=Others (the exact reported
    Others/Videos/2020/Videos/... shape)."""
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    photo_path = str(target / "Masters" / "Joip8" / "2020" / "IMG_0001.JPG")
    video_path = str(target / "Masters" / "Joip8" / "2020" / "Videos" / "2020-10-10_0000.MP4")
    with Database(db_path) as db:
        _add_file(
            db, photo_path,
            datetime_original="2020:01:15 10:00:00", date_confidence="HIGH",
            camera_model="iPhone SE",
        )
        _add_file(
            db, video_path, file_type="VIDEO",
            datetime_original="2020:10:10 09:00:00", date_confidence="HIGH",
        )

        event_groups = _compute_event_groups(db, set(), target_root=target)
        event_base = _event_base_map(db, {"iphone se"}, target_root=target)

        joip8_key = str(target / "Masters" / "Joip8")
        assert event_groups.get(joip8_key, {}).get("kind") == "subject"
        assert event_base.get(joip8_key) == "Masters"

        video_row = _row(db, video_path)
        video_result = _build_target_path(
            video_row, target, {"iphone se"}, {},
            event_groups=event_groups, event_base=event_base,
        )
        assert video_result == target / "Masters" / "Joip8" / "2020" / "Videos" / "2020-10-10_0000.MP4"
        assert "Others" not in str(video_result)
        assert "Videos\\2020\\Videos" not in str(video_result)
