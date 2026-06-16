"""
Tests for `planner._sanitize_event` — deriving the event/location folder
segment from a source folder name.

The date is always added separately as the folder PREFIX
({YYYY-MM-DD}_{event}), so a leading date stamp inside the source folder name
is redundant and must be stripped, and separator runs (including literal
underscores) must collapse to a single '_'.

Surfaced by a real test folder `20100815 倩家 _ 愷 倩 Leo 小蝦`, which produced
`2010-08-15_20100815_倩家___愷_倩_Leo_小蝦` (date twice, triple underscores).

Run: python -m pytest tests/test_sanitize_event.py
"""

from __future__ import annotations

from photo_organizer.planner import _sanitize_event


# ── Separator collapse (literal '_' is a word char, so ' _ ' must collapse) ──

def test_space_underscore_space_collapses_to_single():
    assert _sanitize_event("倩家 _ 愷") == "倩家_愷"


def test_multiple_separators_collapse():
    assert _sanitize_event("a  --  b") == "a_b"


# ── Leading date stamp stripped (date is already the folder prefix) ──────────

def test_leading_yyyymmdd_stripped():
    assert _sanitize_event("20100815 倩家 _ 愷 倩 Leo 小蝦") == "倩家_愷_倩_Leo_小蝦"


def test_leading_dashed_date_stripped():
    assert _sanitize_event("2023-06-15 Wedding") == "Wedding"


def test_leading_year_range_stripped():
    assert _sanitize_event("2010-2012 愷 1~2 歲 videos") == "愷_1_2_歲_videos"


def test_leading_standalone_year_stripped():
    # User decision A: a standalone leading year is also stripped.
    assert _sanitize_event("2012 Summer Trip") == "Summer_Trip"


def test_leading_datetime_stamp_stripped():
    assert _sanitize_event("20230615_143022 party") == "party"


# ── Pure-date folder names collapse to empty (event segment dropped) ─────────

def test_pure_yyyymmdd_becomes_empty():
    assert _sanitize_event("20100815") == ""


def test_pure_year_becomes_empty():
    assert _sanitize_event("2012") == ""


# ── Real event names are preserved (no false stripping) ──────────────────────

def test_plain_event_name_unchanged():
    assert _sanitize_event("Kyoto") == "Kyoto"


def test_event_name_with_underscore_unchanged():
    assert _sanitize_event("Japan_Trip") == "Japan_Trip"


def test_camera_dump_serial_not_mistaken_for_date():
    # 3-digit dump prefixes (100CANON) are not 4-digit years — must NOT strip.
    assert _sanitize_event("100CANON") == "100CANON"


def test_number_inside_name_not_stripped():
    assert _sanitize_event("Grandma 90th") == "Grandma_90th"


# ── Existing guarantees preserved ────────────────────────────────────────────

def test_empty_input_returns_empty():
    assert _sanitize_event("") == ""
    assert _sanitize_event(None) == ""


def test_drive_root_returns_empty():
    assert _sanitize_event("E:\\") == ""


def test_result_truncated_to_40_chars():
    assert len(_sanitize_event("x" * 100)) == 40
