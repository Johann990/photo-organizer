"""
Tests for date forensics in planner.py:

  - _parse_filename_dt: extract capture timestamps embedded in filenames
  - _resolve_date:      pick a date + source + confidence from competing signals
  - audit_dates:        populate date_source/date_confidence columns and log
                        LOW-confidence (suspicious) dates to run_log

All deterministic: _resolve_date / audit_dates accept an injected reference
"today" so future-date detection never depends on the wall clock.

Run: python -m pytest tests/test_date_forensics.py
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from photo_organizer.db import Database
from photo_organizer.planner import (
    _effective_date,
    _parse_filename_dt,
    _resolve_date,
    audit_dates,
)


# ---------------------------------------------------------------------------
# Feature 1 — filename-embedded timestamp parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename, expected",
    [
        # IMG_ with and without time
        ("IMG_20230615_143022.jpg", datetime(2023, 6, 15, 14, 30, 22)),
        ("IMG_20230615.jpg", datetime(2023, 6, 15, 0, 0, 0)),
        # Google Pixel PXL_YYYYMMDD_HHMMSSsss (trailing milliseconds dropped)
        ("PXL_20211103_080910123.jpg", datetime(2021, 11, 3, 8, 9, 10)),
        # VID_
        ("VID_20200101_000000.mp4", datetime(2020, 1, 1, 0, 0, 0)),
        # Android screenshots
        ("Screenshot_20230615-143022.png", datetime(2023, 6, 15, 14, 30, 22)),
        ("Screenshot_2023-06-15-14-30-22.png", datetime(2023, 6, 15, 14, 30, 22)),
        ("Screenshot_2023-06-15-14-30-22-123_com.app.png",
         datetime(2023, 6, 15, 14, 30, 22)),
        ("Screenshot_2023-06-15.png", datetime(2023, 6, 15, 0, 0, 0)),
        # WhatsApp
        ("IMG-20230615-WA0001.jpg", datetime(2023, 6, 15, 0, 0, 0)),
        # Signal
        ("Signal-2023-06-15-143022.jpg", datetime(2023, 6, 15, 14, 30, 22)),
        ("Signal-2023-06-15.jpg", datetime(2023, 6, 15, 0, 0, 0)),
        # bare YYYYMMDD_###### / YYYYMMDD-######  (trailing is a sequence, not time)
        ("20230615_123456.jpg", datetime(2023, 6, 15, 0, 0, 0)),
        ("20230615-001.jpg", datetime(2023, 6, 15, 0, 0, 0)),
        # case-insensitive
        ("img_20230615_143022.JPG", datetime(2023, 6, 15, 14, 30, 22)),
    ],
)
def test_parse_filename_dt_positive(filename, expected):
    assert _parse_filename_dt(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        None,
        "",
        "DSC_0042.jpg",                 # camera serial, no date
        "vacation.jpg",
        "random_5551234.jpg",
        "IMG_20231345_120000.jpg",      # month 13 invalid
        "IMG_20230631.jpg",             # June 31 does not exist
        "20231301_001.jpg",             # month 13 invalid (bare)
        "IMG_2023061.jpg",              # too few digits
        "P1010001.jpg",                 # Panasonic serial, not a date
    ],
)
def test_parse_filename_dt_negative(filename):
    assert _parse_filename_dt(filename) is None


# ---------------------------------------------------------------------------
# Feature 2 — _resolve_date(row, today) -> (datetime|None, source, confidence)
# ---------------------------------------------------------------------------

TODAY = date(2024, 1, 1)


def _local_mtime(s: str) -> datetime:
    """Expected value of _parse_mtime(s): UTC ISO string converted to the
    local calendar day/time, tz dropped — mirrors the production conversion
    so these tests don't hardcode a value that only matches on a UTC+0 box."""
    return datetime.fromisoformat(s).astimezone().replace(tzinfo=None)


def _row(**kw):
    base = {
        "datetime_original": None,
        "datetime_digitized": None,
        "mtime": None,
        "filename": "DSC_0001.jpg",
        "camera_make": None,
        "camera_model": None,
    }
    base.update(kw)
    return base


def test_resolve_camera_with_sane_exif_is_high():
    row = _row(
        camera_model="ILCE-7RM2",
        datetime_original="2023:06:15 14:30:22",
        filename="DSC01234.ARW",
    )
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert dt == datetime(2023, 6, 15, 14, 30, 22)
    assert source == "exif_original"
    assert conf == "HIGH"


def test_resolve_exif_and_filename_agree_is_high():
    row = _row(
        datetime_original="2023:06:15 10:00:00",
        filename="IMG_20230615_100500.jpg",   # same day, ~5 min apart
    )
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert source == "exif_original"
    assert conf == "HIGH"
    assert dt == datetime(2023, 6, 15, 10, 0, 0)


def test_resolve_filename_contradicts_exif_picks_filename_low():
    row = _row(
        datetime_original="2023:06:15 12:00:00",   # faked / injected EXIF
        filename="IMG_20150402_120000.jpg",        # real capture date
    )
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert dt == datetime(2015, 4, 2, 12, 0, 0)
    assert source == "filename"
    assert conf == "LOW"


def test_resolve_exif_only_no_camera_is_medium():
    row = _row(
        datetime_original="2023:06:15 10:00:00",
        filename="DSC_0001.jpg",       # no parseable date
    )
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert source == "exif_original"
    assert conf == "MEDIUM"


def test_resolve_digitized_fallback_is_medium():
    row = _row(
        datetime_original=None,
        datetime_digitized="2022:05:05 09:00:00",
    )
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert dt == datetime(2022, 5, 5, 9, 0, 0)
    assert source == "exif_digitized"
    assert conf == "MEDIUM"


def test_resolve_mtime_last_resort_is_low():
    row = _row(mtime="2021-08-20T10:30:00+00:00")
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert dt == _local_mtime("2021-08-20T10:30:00+00:00")
    assert source == "mtime"
    assert conf == "LOW"


def test_resolve_mtime_converts_utc_to_local_calendar_day():
    # Regression for an off-by-one-day bug: the stored mtime is UTC-aware
    # (scanner.py), but the old _parse_mtime relabelled it naive WITHOUT
    # converting first, so any file modified between local midnight and the
    # local UTC offset (e.g. 00:00-08:00 in UTC+8) resolved to the day BEFORE
    # what Explorer / the OS show for that same file.
    mtime_str = "2020-01-06T16:22:25+00:00"  # 2020-01-07 00:22 local in UTC+8
    row = _row(mtime=mtime_str)
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert dt == _local_mtime(mtime_str)
    assert source == "mtime"
    assert conf == "LOW"


def test_resolve_nothing_is_none():
    row = _row()
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert dt is None
    assert source == "none"


# ---- sanity bounds --------------------------------------------------------

def test_resolve_future_exif_is_not_trusted():
    # Even WITH a camera, a future DateTimeOriginal must not be HIGH.
    row = _row(
        camera_model="ILCE-7RM2",
        datetime_original="2025:06:15 10:00:00",   # future vs TODAY
        mtime="2023-12-31T10:00:00+00:00",
    )
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert source == "mtime"
    assert conf == "LOW"
    assert dt == _local_mtime("2023-12-31T10:00:00+00:00")


def test_resolve_absurdly_old_exif_discarded():
    row = _row(
        datetime_original="1985:01:01 00:00:00",   # year < 1990
        mtime="2020-01-02T00:00:00+00:00",
    )
    dt, source, conf = _resolve_date(row, today=TODAY)
    assert source == "mtime"
    assert dt == _local_mtime("2020-01-02T00:00:00+00:00")


@pytest.mark.parametrize("sentinel", ["1980:01:01 00:00:00", "2000:01:01 00:00:00"])
def test_resolve_fake_sentinel_discarded(sentinel):
    row = _row(camera_model="Canon", datetime_original=sentinel)
    dt, source, conf = _resolve_date(row, today=TODAY)
    # No corroborating date at all → falls all the way through to none.
    assert source == "none"
    assert dt is None


# ---- backward-compatible shim --------------------------------------------

def test_effective_date_shim_matches_resolver():
    row = _row(datetime_original="2023:06:15 10:00:00", camera_model="Canon")
    dt, used_mtime = _effective_date(row)
    assert dt == datetime(2023, 6, 15, 10, 0, 0)
    assert used_mtime is False

    row2 = _row(mtime="2021-08-20T10:30:00+00:00")
    dt2, used_mtime2 = _effective_date(row2)
    assert dt2 == _local_mtime("2021-08-20T10:30:00+00:00")
    assert used_mtime2 is True


# ---------------------------------------------------------------------------
# Feature 3 — audit_dates: persist columns + log suspicious dates
# ---------------------------------------------------------------------------

def _insert(db, file_id, **cols):
    base = {
        "path": f"/src/img{file_id}.jpg",
        "filename": f"img{file_id}.jpg",
        "extension": "jpg",
        "file_type": "CAMERA_JPEG",
        "datetime_original": None,
        "datetime_digitized": None,
        "camera_make": None,
        "camera_model": None,
        "mtime": None,
    }
    base.update(cols)
    keys = ", ".join(base)
    ph = ", ".join("?" * len(base))
    db._conn.execute(
        f"INSERT INTO files (file_id, {keys}) VALUES (?, {ph})",
        (file_id, *base.values()),
    )
    db._conn.commit()


def test_audit_dates_columns_exist():
    """Migration adds the two forensics columns."""
    db = Database(":memory:").connect()
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(files)").fetchall()}
        assert "date_confidence" in cols
        assert "date_source" in cols
    finally:
        db.close()


def test_audit_dates_low_confidence_logs_review_entry(tmp_path):
    db = Database(tmp_path / "photos.db").connect()
    try:
        # A file whose EXIF is contradicted by its filename → LOW, picks filename.
        _insert(
            db, 1,
            path="/src/IMG_20150402_120000.jpg",
            filename="IMG_20150402_120000.jpg",
            datetime_original="2023:06:15 12:00:00",
        )
        # A clean camera file → HIGH, must NOT be flagged.
        _insert(
            db, 2,
            datetime_original="2022:01:02 08:00:00",
            camera_model="ILCE-7RM2",
            filename="DSC09999.jpg",
        )

        summary = audit_dates(db, today=TODAY)

        # Columns populated on both rows.
        r1 = db._conn.execute(
            "SELECT date_source, date_confidence FROM files WHERE file_id=1"
        ).fetchone()
        assert r1["date_source"] == "filename"
        assert r1["date_confidence"] == "LOW"

        r2 = db._conn.execute(
            "SELECT date_source, date_confidence FROM files WHERE file_id=2"
        ).fetchone()
        assert r2["date_confidence"] == "HIGH"

        # A suspicious-date review entry was logged for file 1 only.
        rows = db._conn.execute(
            "SELECT file_id, message FROM run_log "
            "WHERE phase='review' AND message LIKE 'Suspicious-date%' "
            "AND message NOT LIKE 'Suspicious-date summary%'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["file_id"] == 1
        msg = rows[0]["message"]
        assert "2023-06-15" in msg     # the EXIF candidate
        assert "2015-04-02" in msg     # the filename candidate
        assert summary["low"] == 1
    finally:
        db.close()


def test_audit_dates_is_idempotent(tmp_path):
    """Re-running must not duplicate the suspicious-date log entries."""
    db = Database(tmp_path / "photos.db").connect()
    try:
        _insert(
            db, 1,
            path="/src/IMG_20150402_120000.jpg",
            filename="IMG_20150402_120000.jpg",
            datetime_original="2023:06:15 12:00:00",
        )
        audit_dates(db, today=TODAY)
        audit_dates(db, today=TODAY)

        n = db._conn.execute(
            "SELECT COUNT(*) FROM run_log "
            "WHERE phase='review' AND message LIKE 'Suspicious-date%' "
            "AND message NOT LIKE 'Suspicious-date summary%'"
        ).fetchone()[0]
        assert n == 1
    finally:
        db.close()
