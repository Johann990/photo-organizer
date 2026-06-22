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

from datetime import date

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


# ── Leading date glued directly to a CJK name (no separator) ─────────────────
# Real D:\Media case: "20200112三貂嶺 小蜂" produced "2020-01-12 20200112三貂嶺_小蜂"
# because the 8-digit date is followed by a CJK word char, not '_' or end.

def test_leading_date_glued_to_cjk_stripped():
    assert _sanitize_event("20200112三貂嶺 小蜂") == "三貂嶺_小蜂"


def test_leading_year_glued_to_cjk_stripped():
    assert _sanitize_event("2020貢寮浮潛") == "貢寮浮潛"


# ── Leftover (Nd) day-count stamp from a previous organize pass ──────────────
# Real D:\Media cases: "...(4d) 4d_台南移地訓練", "...(2d) 2d_貢寮浮潛" — the source
# folder already carried the planner's own multi-day "(Nd)" stamp; it must be
# stripped along with the leading date, not re-emitted into the event label.

def test_leading_date_and_daycount_dashed_stripped():
    assert _sanitize_event("2020-07-11(2d) 貢寮浮潛") == "貢寮浮潛"


def test_leading_date_and_daycount_compact_stripped():
    assert _sanitize_event("20200130(4d) 台南移地訓練") == "台南移地訓練"


# ── Doubly-dated source folder (date prefix + date-glued name) ───────────────
# Real D:\Media case: a previously-organized source folder "2020-01-12
# 20200112三貂嶺_小蜂" carries TWO date stamps; stripping just the first leaves
# "20200112三貂嶺". Stripping must repeat until no leading date remains.

def test_two_leading_dates_both_stripped():
    assert _sanitize_event("2020-01-12 20200112三貂嶺_小蜂") == "三貂嶺_小蜂"


def test_leading_date_keeps_legit_cjk_number_name():
    # A digit-led label that is NOT a date (e.g. "11月" = November) is kept.
    assert _sanitize_event("2020-11-15 11月團集會") == "11月團集會"


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


# ── Date-RANGE tail-day residue (the "C fix") ────────────────────────────────
# Real D:\Media cases: "20080614~23 NICE 出差", "2019-10-19&20 屈尺當家_蜂為主",
# "20020413_19 ..." — a source folder named as a date RANGE leaks its tail day
# into the label once the leading 8-digit/dashed date is stripped, because the
# generic separator-normalisation step collapses the distinguishing '~'/'&'
# character before the leading-date stripper ever sees it ("23_NICE_出差").
#
# '~' and '&' are unambiguous range separators — nobody types them into an
# event name — so they strip unconditionally, no dates required.
# '_' and '-' on a compact date collide with this project's own NN_ numeric
# labels ("101_煙火" — Taipei 101) and (for '_') its "(Nd)" day-count residue,
# so they only strip when the caller supplies `date_range` (the folder's
# actual EXIF date span) AND the tail, read as a day-of-month, exactly
# matches the real max date.

def test_date_range_tilde_separator_stripped_unconditionally():
    assert _sanitize_event("20080614~23 NICE 出差") == "NICE_出差"


def test_date_range_ampersand_separator_on_dashed_date_stripped_unconditionally():
    assert _sanitize_event("2019-10-19&20 屈尺當家_蜂為主") == "屈尺當家_蜂為主"


def test_date_range_underscore_separator_stripped_when_dates_corroborate():
    date_range = (date(2002, 4, 13), date(2002, 4, 19))
    assert _sanitize_event("20020413_19 清水溪", date_range) == "清水溪"


def test_date_range_underscore_separator_kept_without_date_range():
    # No date context supplied — conservative, unchanged from today's behavior.
    assert _sanitize_event("20020413_19 清水溪") == "19_清水溪"


def test_date_range_underscore_separator_kept_when_dates_dont_corroborate():
    # Tail day 19 is NOT actually the folder's max date — don't trust it.
    date_range = (date(2002, 4, 13), date(2002, 4, 15))
    assert _sanitize_event("20020413_19 清水溪", date_range) == "19_清水溪"


def test_date_range_month_rollover_corroborated():
    # 6/28 ~ 7/3 rolls into the next month; '~' is unconditional anyway, but
    # this exercises the rollover arithmetic via the ambiguous '_' separator.
    date_range = (date(2008, 6, 28), date(2008, 7, 3))
    assert _sanitize_event("20080628_3 NICE 出差", date_range) == "NICE_出差"


def test_date_range_does_not_break_space_separated_taipei101():
    # "20060719 101 煙火" — a SPACE precedes "101", not a glued separator, so
    # this never matches the range-tail pattern at all (must stay untouched).
    assert _sanitize_event("20060719 101 煙火") == "101_煙火"


def test_date_range_dash_separator_kept_without_corroboration():
    # '-' on a compact date is ambiguous too ("19980101-5次旅行" is a real
    # label, not a range) — same conservative default as '_'.
    assert _sanitize_event("20080614-23 NICE 出差") == "23_NICE_出差"
