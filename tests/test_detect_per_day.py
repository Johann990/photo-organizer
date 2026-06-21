"""Tests for detect_per_day_events (already-day-organized multi-day events)."""
from __future__ import annotations

from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.planner import detect_per_day_events


def _add(db, path, dto, conf="HIGH"):
    fn = path.split("\\")[-1]
    db.conn.execute(
        "INSERT INTO files (path, filename, extension, status, file_type, "
        "size_bytes, datetime_original, date_confidence, camera_model) "
        "VALUES (?,?,?,?,?,1000,?,?,?)",
        (path, fn, "jpg", "scanned", "CAMERA_JPEG", dto, conf, "Canon EOS 5D"),
    )
    db.commit()


SR = [Path(r"D:\Raw")]


def test_detects_day_organized_multiday(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        _add(db, root + r"\0808\a.jpg", "2005:08:08 10:00:00")
        _add(db, root + r"\0808\b.jpg", "2005:08:08 11:00:00")
        _add(db, root + r"\0809\c.jpg", "2005:08:09 10:00:00")
        _add(db, root + r"\0810\d.jpg", "2005:08:10 10:00:00")
        cands = detect_per_day_events(db, SR)
    folders = {c["event_folder"] for c in cands}
    assert root in folders
    c = next(c for c in cands if c["event_folder"] == root)
    assert c["span"] == 3 and len(c["subfolders"]) == 3


def test_single_subfolder_not_detected(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        _add(db, root + r"\0808\a.jpg", "2005:08:08 10:00:00")
        cands = detect_per_day_events(db, SR)
    assert all(c["event_folder"] != root for c in cands)


def test_mixed_day_subfolder_below_threshold_not_detected(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Dump"
        _add(db, root + r"\sub1\a.jpg", "2005:08:08 10:00:00")
        _add(db, root + r"\sub1\b.jpg", "2005:09:20 10:00:00")  # 50/50 across months
        _add(db, root + r"\sub2\c.jpg", "2005:08:09 10:00:00")
        cands = detect_per_day_events(db, SR)
    assert all(c["event_folder"] != root for c in cands)


def test_non_consecutive_days_not_detected(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Mixed"
        _add(db, root + r"\jan\a.jpg", "2005:01:01 10:00:00")
        _add(db, root + r"\dec\b.jpg", "2005:12:25 10:00:00")  # huge gap
        cands = detect_per_day_events(db, SR)
    assert all(c["event_folder"] != root for c in cands)


def test_dominant_day_within_threshold_detected(tmp_path):
    # A subfolder that is 90%+ one day (9 of 10) still counts as single-day.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        for i in range(9):
            _add(db, root + f"\\0808\\a{i}.jpg", "2005:08:08 10:00:00")
        _add(db, root + r"\0808\outlier.jpg", "2005:08:09 10:00:00")  # 1 of 10 = 10%
        _add(db, root + r"\0809\c.jpg", "2005:08:09 10:00:00")
        cands = detect_per_day_events(db, SR)
    assert any(c["event_folder"] == root for c in cands)


def test_below_dominance_threshold_not_detected(tmp_path):
    # 8/10 = 80% one day → below 90% dominance → subfolder doesn't qualify.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        for i in range(8):
            _add(db, root + f"\0808\a{i}.jpg", "2005:08:08 10:00:00")
        _add(db, root + r"\0808\x1.jpg", "2005:08:09 10:00:00")  # 2 of 10 = 80%
        _add(db, root + r"\0808\x2.jpg", "2005:08:09 11:00:00")
        _add(db, root + r"\0809\c.jpg", "2005:08:09 10:00:00")
        cands = detect_per_day_events(db, SR)
    # only 0809 qualifies (one subfolder) → not enough for a candidate
    assert all(c["event_folder"] != root for c in cands)


def test_two_independent_events_both_detected(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        a = r"D:\Raw\20050814 蒙古"
        _add(db, a + r"\0808\a.jpg", "2005:08:08 10:00:00")
        _add(db, a + r"\0809\b.jpg", "2005:08:09 10:00:00")
        _add(db, a + r"\0810\c.jpg", "2005:08:10 10:00:00")
        b = r"D:\Raw\20120901 上海"
        _add(db, b + r"\0901\d.jpg", "2012:09:01 10:00:00")
        _add(db, b + r"\0902\e.jpg", "2012:09:02 10:00:00")
        cands = detect_per_day_events(db, SR)
    folders = {c["event_folder"] for c in cands}
    assert a in folders and b in folders
    # sorted by -len(subfolders): 蒙古 (3) before 上海 (2)
    assert cands[0]["event_folder"] == a


def test_no_event_root_not_detected(tmp_path):
    # Subfolders directly under a scan root with no event-name ancestor → root
    # resolves to None/blank → not a candidate.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add(db, r"D:\Raw\0808\a.jpg", "2005:08:08 10:00:00")
        _add(db, r"D:\Raw\0809\b.jpg", "2005:08:09 10:00:00")
        cands = detect_per_day_events(db, SR)
    assert cands == [] or all(r"D:\Raw" != c["event_folder"] for c in cands)


def test_needing_split_flags_scattered_multiday(tmp_path):
    from photo_organizer.planner import detect_multiday_needing_split
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Mixed Dump"
        # photos sit DIRECTLY in the event folder; the dates are SCATTERED — a
        # >3-day gap between clusters → likely several occasions dumped together.
        _add(db, root + r"\a.jpg", "2012:09:01 10:00:00")
        _add(db, root + r"\b.jpg", "2012:09:02 10:00:00")
        _add(db, root + r"\c.jpg", "2012:09:20 10:00:00")  # 18-day gap
        out = detect_multiday_needing_split(db, SR)
    assert any(c["event_folder"] == root for c in out)


def test_needing_split_skips_contiguous_trip(tmp_path):
    from photo_organizer.planner import detect_multiday_needing_split
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Taiwan Trip"
        # consecutive days (all gaps <= _EVENT_DAY_GAP) → coherent trip, filed
        # fine flat → NOT flagged.
        _add(db, root + r"\a.jpg", "2012:09:01 10:00:00")
        _add(db, root + r"\b.jpg", "2012:09:02 10:00:00")
        _add(db, root + r"\c.jpg", "2012:09:03 10:00:00")
        out = detect_multiday_needing_split(db, SR)
    assert all(c["event_folder"] != root for c in out)


def test_needing_split_skips_exact_gap_boundary(tmp_path):
    # Gap exactly _EVENT_DAY_GAP (3) is inclusive on the skip side: 09-01 → 09-04
    # has gaps=[3], all(<=3) → still a coherent trip → NOT flagged.
    from photo_organizer.planner import detect_multiday_needing_split
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Long Weekend"
        _add(db, root + r"\a.jpg", "2012:09:01 10:00:00")
        _add(db, root + r"\b.jpg", "2012:09:04 10:00:00")  # gap == 3
        out = detect_multiday_needing_split(db, SR)
    assert all(c["event_folder"] != root for c in out)


def test_needing_split_excludes_already_day_organized(tmp_path):
    from photo_organizer.planner import detect_multiday_needing_split, detect_per_day_events
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        _add(db, root + r"\0808\a.jpg", "2005:08:08 10:00:00")
        _add(db, root + r"\0809\b.jpg", "2005:08:09 10:00:00")
        cands = {c["event_folder"] for c in detect_per_day_events(db, SR)}
        out = detect_multiday_needing_split(db, SR, exclude=cands)
    # already day-organized → should NOT appear in the needs-split reminder
    assert all(c["event_folder"] != root for c in out)


def test_needing_split_ignores_single_day(tmp_path):
    from photo_organizer.planner import detect_multiday_needing_split
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\OneDay"
        _add(db, root + r"\a.jpg", "2012:09:01 10:00:00")
        _add(db, root + r"\b.jpg", "2012:09:01 12:00:00")
        out = detect_multiday_needing_split(db, SR)
    assert all(c["event_folder"] != root for c in out)
