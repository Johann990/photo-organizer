"""
Tests for the `_CONTAINER_NAMES` blocklist expansion — generic placeholder
words (old/new/misc/backup/...) that must never become an event/subject label.

These are EXACT-match checks (normalised whole-name equality), not substring
matches — a name that merely *contains* one of these words (Old_Believers_Trip)
must NOT be caught.

Run: python -m pytest tests/test_container_names.py
"""

from __future__ import annotations

from pathlib import Path

from photo_organizer.planner import _is_container_or_device, _resolve_event_folder

NEW_CONTAINER_WORDS = [
    "old", "new", "misc", "backup", "bak", "copy", "untitled",
    "新增資料夾", "未命名", "暫存",
]


def test_new_container_words_are_blocked():
    for word in NEW_CONTAINER_WORDS:
        assert _is_container_or_device(word) is True, word


def test_new_container_words_case_insensitive():
    for word in NEW_CONTAINER_WORDS:
        assert _is_container_or_device(word.upper()) is True, word


def test_dcim_old_resolves_to_no_event_folder():
    # Pure path-string case (no real file needed) matching the plan's
    # verification scenario exactly: DCIM blocked by pattern, old blocked by
    # blocklist, drive root reached → None.
    path = Path("D:/DCIM/old/IMG_0001.JPG")
    assert _resolve_event_folder(path) is None


# ── Non-regression: real names still pass through ────────────────────────────

def test_real_event_names_not_blocked():
    for name in ("Kyoto", "蒙古", "小英照"):
        assert _is_container_or_device(name) is False, name


def test_substring_containing_container_word_not_blocked():
    # Contains "old" / "backup" as a substring but is NOT equal to the word —
    # exact-match semantics must not over-match.
    assert _is_container_or_device("Old_Believers_Trip") is False
    assert _is_container_or_device("backup_plan_for_japan") is False


def test_dcim_old_end_to_end_lands_in_others_no_label():
    from photo_organizer.planner import _build_target_path

    row = {
        "path": "D:/DCIM/old/IMG_0001.JPG",
        "filename": "IMG_0001.JPG",
        "extension": "jpg",
        "file_type": "CAMERA_JPEG",
        "datetime_original": "2020:05:01 10:00:00",
        "datetime_digitized": None,
        "mtime": None,
        "camera_model": "UnknownCam",
    }
    target = _build_target_path(row, Path("E:/Library"), set(), {})
    assert target.parent.name == "2020-05-01"
    assert target.parent.parent.name == "2020"
    assert target.parent.parent.parent.name == "Others"
