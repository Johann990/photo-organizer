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


def test_per_day_year_filter_scopes_to_one_year(tmp_path):
    # P3 batch triage: --year lets the user review one year's worth of
    # candidates at a time instead of the whole library.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        a = r"D:\Raw\20050814 蒙古"
        _add(db, a + r"\0808\a.jpg", "2005:08:08 10:00:00")
        _add(db, a + r"\0809\b.jpg", "2005:08:09 10:00:00")
        _add(db, a + r"\0810\c.jpg", "2005:08:10 10:00:00")
        b = r"D:\Raw\20120901 上海"
        _add(db, b + r"\0901\d.jpg", "2012:09:01 10:00:00")
        _add(db, b + r"\0902\e.jpg", "2012:09:02 10:00:00")
        cands_2005 = detect_per_day_events(db, SR, year="2005")
        cands_2012 = detect_per_day_events(db, SR, year="2012")
    assert {c["event_folder"] for c in cands_2005} == {a}
    assert {c["event_folder"] for c in cands_2012} == {b}


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


def test_needing_split_year_filter_scopes_to_one_year(tmp_path):
    from photo_organizer.planner import detect_multiday_needing_split
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        a = r"D:\Raw\Mixed Dump 2012"
        _add(db, a + r"\a.jpg", "2012:09:01 10:00:00")
        _add(db, a + r"\b.jpg", "2012:09:02 10:00:00")
        _add(db, a + r"\c.jpg", "2012:09:20 10:00:00")  # 18-day gap
        b = r"D:\Raw\Mixed Dump 2015"
        _add(db, b + r"\a.jpg", "2015:03:01 10:00:00")
        _add(db, b + r"\b.jpg", "2015:03:02 10:00:00")
        _add(db, b + r"\c.jpg", "2015:03:20 10:00:00")  # 18-day gap
        out_2012 = detect_multiday_needing_split(db, SR, year="2012")
        out_2015 = detect_multiday_needing_split(db, SR, year="2015")
    assert {c["event_folder"] for c in out_2012} == {a}
    assert {c["event_folder"] for c in out_2015} == {b}


def test_subject_candidate_flags_long_span(tmp_path):
    from photo_organizer.planner import detect_subjects_needing_confirmation
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Mixed Dump"
        _add(db, root + r"\a.jpg", "2019:12:31 10:00:00")
        _add(db, root + r"\b.jpg", "2020:01:04 10:00:00")
        _add(db, root + r"\c.jpg", "2020:02:02 10:00:00")  # 34-day span > 30
        out = detect_subjects_needing_confirmation(db, SR)
    c = next(c for c in out if c["event_folder"] == root)
    assert c["label"] == "Mixed_Dump"
    assert c["span"] == 34
    assert c["days"] == 3
    assert c["date_lo"] == "2019-12-31" and c["date_hi"] == "2020-02-02"


def test_subject_candidate_skips_short_span(tmp_path):
    # 2-day span is a normal multi-day event, not a subject candidate.
    from photo_organizer.planner import detect_subjects_needing_confirmation
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\台南移地訓練"
        _add(db, root + r"\a.jpg", "2020:02:01 10:00:00")
        _add(db, root + r"\b.jpg", "2020:02:02 10:00:00")
        out = detect_subjects_needing_confirmation(db, SR)
    assert all(c["event_folder"] != root for c in out)


def test_subject_candidate_excludes_confirmed(tmp_path):
    from photo_organizer.planner import detect_subjects_needing_confirmation
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Baby"
        _add(db, root + r"\a.jpg", "2019:01:01 10:00:00")
        _add(db, root + r"\b.jpg", "2020:06:15 10:00:00")
        db.set_folder_override(
            root, event_name="Baby", confirmed_subject=1,
            updated_at="2024-01-01T00:00:00+00:00",
        )
        db.commit()
        out = detect_subjects_needing_confirmation(db, SR)
    assert all(c["event_folder"] != root for c in out)


def test_subject_candidate_no_event_root_not_detected(tmp_path):
    # Files sitting directly under the scan root with no nameable ancestor →
    # _resolve_event_folder returns None → never a subject candidate.
    from photo_organizer.planner import detect_subjects_needing_confirmation
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add(db, r"D:\Raw\a.jpg", "2019:01:01 10:00:00")
        _add(db, r"D:\Raw\b.jpg", "2020:06:15 10:00:00")
        out = detect_subjects_needing_confirmation(db, SR)
    assert out == [] or all(r"D:\Raw" != c["event_folder"] for c in out)


def test_subject_candidate_year_filter_touches_either_boundary_year(tmp_path):
    # A subject's span can cross a year boundary (Mixed Dump here runs
    # 2019-12-31..2020-02-02) — --year scopes by "does this candidate have
    # ANY photo dated in that year", not by recomputing the span from a
    # year-restricted subset (which would just shrink/hide it).
    from photo_organizer.planner import detect_subjects_needing_confirmation
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Mixed Dump"
        _add(db, root + r"\a.jpg", "2019:12:31 10:00:00")
        _add(db, root + r"\b.jpg", "2020:01:04 10:00:00")
        _add(db, root + r"\c.jpg", "2020:02:02 10:00:00")
        out_2019 = detect_subjects_needing_confirmation(db, SR, year="2019")
        out_2020 = detect_subjects_needing_confirmation(db, SR, year="2020")
        out_2021 = detect_subjects_needing_confirmation(db, SR, year="2021")
    assert any(c["event_folder"] == root for c in out_2019)
    assert any(c["event_folder"] == root for c in out_2020)
    assert all(c["event_folder"] != root for c in out_2021)
