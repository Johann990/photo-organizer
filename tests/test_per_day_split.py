"""Tests for per-day {mmdd}/ subfolder split of flagged multi-day events."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.planner import _build_target_path

SR = [Path(r"D:\Raw")]


def _add(db, path, *, dto, conf="HIGH", ft="CAMERA_JPEG", cam="Canon EOS 5D"):
    fn = path.split("\\")[-1]
    ext = fn.rsplit(".", 1)[-1].lower()
    db.conn.execute(
        "INSERT INTO files (path, filename, extension, status, file_type, "
        "size_bytes, datetime_original, date_confidence, camera_model) "
        "VALUES (?,?,?,?,?,1000,?,?,?)",
        (path, fn, ext, "scanned", ft, dto, conf, cam),
    )
    db.commit()
    return db.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()


def test_flagged_multiday_splits_by_day(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        r1 = _add(db, root + r"\0808\IMG_1.jpg", dto="2005:08:08 10:00:00")
        r2 = _add(db, root + r"\0809\IMG_2.jpg", dto="2005:08:09 10:00:00")
        db.set_folder_override(root, per_day_split=1, updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        overrides = db.get_folder_overrides()
        eb = {root: "Masters"}
        groups = {root: {"kind": "event", "start": date(2005, 8, 8), "span": 2}}
    p1 = _build_target_path(r1, Path("D:/Media"), {"canon eos 5d"}, {},
                            event_groups=groups, scan_roots=SR,
                            overrides=overrides, event_base=eb)
    p2 = _build_target_path(r2, Path("D:/Media"), {"canon eos 5d"}, {},
                            event_groups=groups, scan_roots=SR,
                            overrides=overrides, event_base=eb)
    assert str(Path("2005-08-08(2d) 蒙古") / "0808") in str(p1)
    assert str(Path("2005-08-08(2d) 蒙古") / "0809") in str(p2)


def test_unflagged_multiday_stays_flat(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        r1 = _add(db, root + r"\0808\IMG_1.jpg", dto="2005:08:08 10:00:00")
        db.commit()
        groups = {root: {"kind": "event", "start": date(2005, 8, 8), "span": 2}}
    p1 = _build_target_path(r1, Path("D:/Media"), {"canon eos 5d"}, {},
                            event_groups=groups, scan_roots=SR,
                            event_base={root: "Masters"})
    s = str(p1)
    assert "2005-08-08(2d) 蒙古" in s
    assert (str(Path("蒙古") / "0808")) not in s
    assert s.endswith("IMG_1.jpg")


def test_per_day_outlier_goes_to_its_own_day(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        r = _add(db, root + r"\0808\IMG_X.jpg", dto="2005:08:09 10:00:00")
        db.set_folder_override(root, per_day_split=1, updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        overrides = db.get_folder_overrides()
        groups = {root: {"kind": "event", "start": date(2005, 8, 8), "span": 2}}
    p = _build_target_path(r, Path("D:/Media"), {"canon eos 5d"}, {},
                           event_groups=groups, scan_roots=SR,
                           overrides=overrides, event_base={root: "Masters"})
    assert str(Path("2005-08-08(2d) 蒙古") / "0809") in str(p)


def test_per_day_split_only_applies_to_multiday(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Kyoto"
        r = _add(db, root + r"\IMG_1.jpg", dto="2012:09:08 10:00:00")
        db.set_folder_override(root, per_day_split=1, updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        overrides = db.get_folder_overrides()
    p = _build_target_path(r, Path("D:/Media"), {"canon eos 5d"}, {},
                           event_groups={}, scan_roots=SR,
                           overrides=overrides, event_base={root: "Masters"})
    assert str(p).endswith(str(Path("2012-09-08 Kyoto") / "IMG_1.jpg"))


def test_flagged_multiday_video_splits_by_day(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        r = _add(db, root + r"\0808\MOV.mp4", dto="2005:08:08 10:00:00",
                 ft="VIDEO", cam=None)
        db.set_folder_override(root, per_day_split=1, updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        overrides = db.get_folder_overrides()
        groups = {root: {"kind": "event", "start": date(2005, 8, 8), "span": 2}}
    p = _build_target_path(r, Path("D:/Media"), {"canon eos 5d"}, {},
                           event_groups=groups, scan_roots=SR,
                           overrides=overrides, event_base={root: "Masters"})
    # video co-locates under the per-day folder's Videos/ subfolder
    assert str(Path("2005-08-08(2d) 蒙古") / "0808" / "Videos") in str(p)


def test_nodate_member_of_flagged_event_skips_mmdd(tmp_path):
    # A date-less file in a flagged multi-day event must NOT get an {mmdd}/ level;
    # it falls back to the global NoDate tree (dt is None → handled before split).
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        r = _add(db, root + r"\0808\IMG_nodate.jpg", dto=None, conf=None)
        db.set_folder_override(root, per_day_split=1, updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        overrides = db.get_folder_overrides()
        groups = {root: {"kind": "event", "start": date(2005, 8, 8), "span": 2}}
    p = _build_target_path(r, Path("D:/Media"), {"canon eos 5d"}, {},
                           event_groups=groups, scan_roots=SR,
                           overrides=overrides, event_base={root: "Masters"})
    assert "NoDate" in str(p)
    assert "蒙古" not in str(p)   # no event/per-day folder for a date-less file
