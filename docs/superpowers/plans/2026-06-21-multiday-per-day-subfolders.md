# Multi-day Per-Day Subfolders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user opt a multi-day event into per-day `{mmdd}/` subfolders inside its event folder, auto-detect "already day-organized multi-day" events to suggest it, and unify single/multi-day event-folder naming.

**Architecture:** Add a `per_day_split` flag to `folder_overrides` (keyed by the **event-root** folder). The planner's `_event_subdir` renames single/multi-day folders to a consistent ISO format and, when a multi-day event's root is flagged, nests files under a `{mmdd}/` subfolder by each file's own date. A DB-only `detect_per_day_events` rule surfaces candidates in `review --organize`.

**Tech Stack:** Python 3.11, SQLite (stdlib sqlite3), pytest, stdlib http.server (folderorganize). Windows-first.

**Spec:** `docs/superpowers/specs/2026-06-21-multiday-per-day-subfolders-design.md`

**Key decisions locked here:**
- `per_day_split` is keyed by the **event-root** folder = `str(_resolve_event_folder(path))`. (Distinct from `event_name`/`date_override`, which O2 keys by the file's immediate parent. A folder may therefore have two override rows; that is fine.)
- Naming: single-day `{YYYY-MM-DD} {event}`; multi-day `{YYYY-MM-DD}({N}d) {event}`; subject unchanged (`{event}/{YYYY}/`). Separator is a space.
- `{mmdd}` = each file's own effective date (`_effective_date_with_override`), so outliers self-correct.

---

### Task 1: DB — `per_day_split` column, migration, helper

**Files:**
- Modify: `photo_organizer/db.py` (SCHEMA_SQL folder_overrides block; `SCHEMA_VERSION`; `_apply_migrations`; new `_migrate_folder_overrides_per_day_split`; `set_folder_override`)
- Test: `tests/test_folder_overrides_db.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_folder_overrides_db.py`:
```python
def test_per_day_split_defaults_zero(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        db.set_folder_override("D:\\Trips\\Kyoto", event_name="Kyoto", updated_at=_now())
        db.commit()
        row = db.get_folder_overrides()["D:\\Trips\\Kyoto"]
        assert row["per_day_split"] == 0


def test_set_per_day_split(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        db.set_folder_override(
            "D:\\Trips\\Kyoto", event_name="Kyoto", per_day_split=1, updated_at=_now()
        )
        db.commit()
        row = db.get_folder_overrides()["D:\\Trips\\Kyoto"]
        assert row["per_day_split"] == 1


def test_schema_version_is_6():
    from photo_organizer.db import SCHEMA_VERSION
    assert SCHEMA_VERSION == 6


def test_migration_adds_per_day_split_to_v5_db(tmp_path):
    # Simulate a pre-existing v5 DB without the column, then open with current code.
    import sqlite3
    db_path = tmp_path / ".photo_organizer" / "library.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE folder_overrides (source_folder TEXT PRIMARY KEY, "
        "event_name TEXT, date_override TEXT, note TEXT, updated_at TEXT)"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '5')")
    conn.execute(
        "INSERT INTO folder_overrides (source_folder, event_name) VALUES (?, ?)",
        ("D:\\Old", "OldEvent"),
    )
    conn.commit()
    conn.close()

    with Database(db_path) as db:
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(folder_overrides)")]
        assert "per_day_split" in cols
        # existing row survives with default 0
        assert db.get_folder_overrides()["D:\\Old"]["per_day_split"] == 0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_folder_overrides_db.py -k "per_day_split or version_is_6 or migration_adds" -v`
Expected: FAIL (`per_day_split` unknown column / SCHEMA_VERSION == 5).

- [ ] **Step 3: Add the column to SCHEMA_SQL**

In `photo_organizer/db.py`, the `folder_overrides` CREATE TABLE block (added in O1) currently ends:
```sql
CREATE TABLE IF NOT EXISTS folder_overrides (
    source_folder TEXT PRIMARY KEY,
    event_name    TEXT,
    date_override TEXT,
    note          TEXT,
    updated_at    TEXT
);
```
Change the body to add the new column before the closing paren:
```sql
CREATE TABLE IF NOT EXISTS folder_overrides (
    source_folder TEXT PRIMARY KEY,
    event_name    TEXT,
    date_override TEXT,
    note          TEXT,
    updated_at    TEXT,
    per_day_split INTEGER NOT NULL DEFAULT 0
);
```

- [ ] **Step 4: Bump SCHEMA_VERSION**

Change `SCHEMA_VERSION = 5` to `SCHEMA_VERSION = 6` in `photo_organizer/db.py`.

- [ ] **Step 5: Add the ADD COLUMN migration**

In `photo_organizer/db.py`, find `_apply_migrations` (ends ~line 244 with calls to `_migrate_filetype_check(conn)` / `_migrate_operations_check(conn)`). Add a third call:
```python
    _migrate_filetype_check(conn)
    _migrate_operations_check(conn)
    _migrate_folder_overrides_per_day_split(conn)
```
Then add the function next to the other `_migrate_*` functions:
```python
def _migrate_folder_overrides_per_day_split(conn: sqlite3.Connection) -> None:
    """Add folder_overrides.per_day_split to an existing DB. Idempotent: only
    ALTERs when the column is absent (a fresh DB already has it via SCHEMA_SQL)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(folder_overrides)").fetchall()]
    if "per_day_split" not in cols:
        conn.execute(
            "ALTER TABLE folder_overrides ADD COLUMN per_day_split INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
```

- [ ] **Step 6: Update `set_folder_override`**

Replace `set_folder_override` (db.py ~761) with:
```python
    def set_folder_override(self, source_folder: str, *, event_name=None,
                            date_override=None, note=None, per_day_split=0,
                            updated_at: str) -> None:
        """Upsert a per-folder override. event_name/date_override = None clears
        that column; per_day_split (0/1) flags an event for per-day {mmdd}/ split.
        Stores the full row keyed by source_folder."""
        self.conn.execute(
            "INSERT INTO folder_overrides "
            "(source_folder, event_name, date_override, note, per_day_split, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source_folder) DO UPDATE SET "
            "event_name=excluded.event_name, date_override=excluded.date_override, "
            "note=excluded.note, per_day_split=excluded.per_day_split, "
            "updated_at=excluded.updated_at",
            (source_folder, event_name, date_override, note, int(per_day_split), updated_at),
        )
```
(`get_folder_overrides` uses `SELECT *`, so it returns the new column automatically.)

- [ ] **Step 7: Run tests, verify pass**

Run: `python -m pytest tests/test_folder_overrides_db.py -v`
Expected: PASS (all, including the new 4).

- [ ] **Step 8: Run full suite (other callers of set_folder_override must still pass)**

Run: `python -m pytest -q`
Expected: PASS. (Existing `set_folder_override(...)` callers omit `per_day_split` → default 0.)

- [ ] **Step 9: Commit**

```bash
git add photo_organizer/db.py tests/test_folder_overrides_db.py
git commit -m "feat(per-day): folder_overrides.per_day_split column + migration (MD1)"
```

---

### Task 2: planner — unify single/multi-day event folder naming

**Files:**
- Modify: `photo_organizer/planner.py` (`_event_subdir`)
- Modify (assertion updates): `tests/test_subject_collection.py`, `tests/test_folder_overrides_plan.py`, `tests/test_video_colocation.py`, `tests/test_container_names.py` (any that assert old `_`/`_Nd_` formats)
- Test: `tests/test_event_naming.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/test_event_naming.py`:
```python
"""Tests for the unified single/multi-day event folder naming in _event_subdir."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from photo_organizer.planner import _event_subdir


def test_single_day_naming_uses_space():
    dt = datetime(2012, 9, 8, 10, 0, 0)
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "上海", None)
    assert str(sub) == str(Path("D:/Media/Masters/2012/2012-09-08 上海"))


def test_single_day_no_event_is_date_only():
    dt = datetime(2012, 9, 8, 10, 0, 0)
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "", None)
    assert str(sub) == str(Path("D:/Media/Masters/2012/2012-09-08"))


def test_multiday_naming_paren_span():
    dt = datetime(2005, 8, 8, 10, 0, 0)
    group = {"kind": "event", "start": date(2005, 8, 6), "span": 9}
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "蒙古", group)
    assert str(sub) == str(Path("D:/Media/Masters/2005/2005-08-06(9d) 蒙古"))


def test_multiday_no_event_is_date_span_only():
    dt = datetime(2005, 8, 8, 10, 0, 0)
    group = {"kind": "event", "start": date(2005, 8, 6), "span": 9}
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "", group)
    assert str(sub) == str(Path("D:/Media/Masters/2005/2005-08-06(9d)"))


def test_subject_naming_unchanged():
    dt = datetime(2012, 9, 8, 10, 0, 0)
    group = {"kind": "subject", "label": "愷"}
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "愷", group)
    assert str(sub) == str(Path("D:/Media/Masters/愷/2012"))
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_event_naming.py -v`
Expected: FAIL (old format `2012-09-08_上海` / `2005-08-06_9d_蒙古`).

- [ ] **Step 3: Update `_event_subdir` naming**

In `photo_organizer/planner.py`, replace the multi-day and single-day branches of `_event_subdir` (the function ends ~line 971). Keep the subject branch unchanged. New body:
```python
    if group and group["kind"] == "subject":
        folder = ov_event or group["label"] or event
        return base / folder / dt.strftime("%Y")
    if group and group["kind"] == "event":
        start = group["start"]
        stamp = f"{start.strftime('%Y-%m-%d')}({group['span']}d)"
        folder = f"{stamp} {event}" if event else stamp
        return base / start.strftime("%Y") / folder
    day = dt.strftime("%Y-%m-%d")
    folder = f"{day} {event}" if event else day
    return base / dt.strftime("%Y") / folder
```

- [ ] **Step 4: Run new tests, verify pass**

Run: `python -m pytest tests/test_event_naming.py -v`
Expected: PASS.

- [ ] **Step 5: Update existing tests that assert the old format**

Run the full suite to find every assertion broken by the rename:
Run: `python -m pytest -q`
Expected: several FAILs in `test_subject_collection.py`, `test_folder_overrides_plan.py`, `test_video_colocation.py`, possibly `test_container_names.py`.

For each failure, update the expected path string to the new format:
- `{date}_{event}` → `{date} {event}` (e.g. `2012-09-08_上海` → `2012-09-08 上海`)
- `{start}_{N}d_{event}` → `{start}({N}d) {event}` (e.g. `2005-08-06_9d_蒙古` → `2005-08-06(9d) 蒙古`)
- `{start}_{N}d` (no event) → `{start}({N}d)`

Do NOT weaken assertions — only translate the format. Read each failing assertion, apply the mapping, keep the rest of the assertion intact.

- [ ] **Step 6: Run full suite, verify green**

Run: `python -m pytest -q`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add photo_organizer/planner.py tests/
git commit -m "feat(event-naming): unify single/multi-day folders to ISO + space (MD2)"
```

---

### Task 3: planner — per-day `{mmdd}/` split for flagged multi-day events

**Files:**
- Modify: `photo_organizer/planner.py` (`_build_target_path` photo + video branches; thread per_day_split lookup)
- Test: `tests/test_per_day_split.py` (new)

**Design:** per_day_split is keyed by the **event root** = `str(_resolve_event_folder(path, scan_roots))`. When a file's event resolves to a flagged root AND the file is in a multi-day `event` group, append `/{mmdd}/` (file's own effective date) to the event subdir.

- [ ] **Step 1: Write failing tests**

Create `tests/test_per_day_split.py`:
```python
"""Tests for per-day {mmdd}/ subfolder split of flagged multi-day events."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.planner import _build_target_path


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
    # Event "蒙古" with per-day subfolders 0808/0809; flagged per_day_split=1.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        r1 = _add(db, root + r"\0808\IMG_1.jpg", dto="2005:08:08 10:00:00")
        r2 = _add(db, root + r"\0809\IMG_2.jpg", dto="2005:08:09 10:00:00")
        db.set_folder_override(root, per_day_split=1, updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        overrides = db.get_folder_overrides()
        eb = {root: "Masters"}
        # multi-day group keyed by resolved event folder == root
        groups = {root: {"kind": "event", "start": date(2005, 8, 8), "span": 2}}

    p1 = _build_target_path(r1, Path("D:/Media"), {"canon eos 5d"}, {},
                            event_groups=groups, overrides=overrides, event_base=eb)
    p2 = _build_target_path(r2, Path("D:/Media"), {"canon eos 5d"}, {},
                            event_groups=groups, overrides=overrides, event_base=eb)
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
                            event_groups=groups, event_base={root: "Masters"})
    s = str(p1)
    assert "2005-08-08(2d) 蒙古" in s
    assert (str(Path("蒙古") / "0808")) not in s   # no per-day subfolder
    assert s.endswith("IMG_1.jpg")


def test_per_day_outlier_goes_to_its_own_day(tmp_path):
    # A file physically in 0808 but dated 08-09 lands under 0809 (own date wins).
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        r = _add(db, root + r"\0808\IMG_X.jpg", dto="2005:08:09 10:00:00")
        db.set_folder_override(root, per_day_split=1, updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        overrides = db.get_folder_overrides()
        groups = {root: {"kind": "event", "start": date(2005, 8, 8), "span": 2}}

    p = _build_target_path(r, Path("D:/Media"), {"canon eos 5d"}, {},
                           event_groups=groups, overrides=overrides,
                           event_base={root: "Masters"})
    assert str(Path("2005-08-08(2d) 蒙古") / "0809") in str(p)


def test_per_day_split_only_applies_to_multiday(tmp_path):
    # per_day_split set, but the event is single-day (no group) → no {mmdd}/ level.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Kyoto"
        r = _add(db, root + r"\IMG_1.jpg", dto="2012:09:08 10:00:00")
        db.set_folder_override(root, per_day_split=1, updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        overrides = db.get_folder_overrides()

    p = _build_target_path(r, Path("D:/Media"), {"canon eos 5d"}, {},
                           event_groups={}, overrides=overrides,
                           event_base={root: "Masters"})
    # single-day folder, file directly inside (no mmdd subfolder)
    assert str(p).endswith(str(Path("2012-09-08 Kyoto") / "IMG_1.jpg"))
```

> Note: these tests assume `_resolve_event_folder(D:\Raw\20050814 蒙古\0808\IMG.jpg)` resolves to `D:\Raw\20050814 蒙古` (climbs past the `0808` date-serial subfolder). Verify this holds when implementing; if the resolver needs `scan_roots` to climb correctly, pass `scan_roots=[Path(r"D:\Raw")]` in the test calls.

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_per_day_split.py -v`
Expected: FAIL (no per-day split yet — flagged event still flat).

- [ ] **Step 3: Implement per-day split in `_build_target_path`**

In `photo_organizer/planner.py`, in the PHOTO branch of `_build_target_path` (the `else` after `if dt is None`, where it computes `subdir = _event_subdir(base, dt, event, group, _ov_event)`), wrap with the per-day check. Replace:
```python
        subdir = _event_subdir(base, dt, event, group, _ov_event)
    return subdir / row["filename"]
```
with:
```python
        subdir = _event_subdir(base, dt, event, group, _ov_event)
        subdir = _maybe_per_day(subdir, dt, group, resolved, overrides)
    return subdir / row["filename"]
```
Add this helper above `_build_target_path`:
```python
def _maybe_per_day(subdir: Path, dt: datetime, group: dict | None,
                   resolved: Path | None, overrides: dict | None) -> Path:
    """Append a {mmdd}/ subfolder (file's own day) when the file's event root is
    flagged per_day_split AND the event is multi-day. No-op otherwise."""
    if not (group and group.get("kind") == "event" and resolved is not None):
        return subdir
    ov = (overrides or {}).get(str(resolved))
    if ov is not None and ov["per_day_split"]:
        return subdir / dt.strftime("%m%d")
    return subdir
```

- [ ] **Step 4: Apply the same to the VIDEO branch**

In the VIDEO branch of `_build_target_path`, where it currently does
`subdir = _event_subdir(base, dt, event, group, _ov_event) / "Videos"`,
change to apply per-day to the EVENT folder (before the `Videos` suffix), so a flagged event's videos co-locate under the same per-day folder:
```python
            ev_subdir = _event_subdir(base, dt, event, group, _ov_event)
            ev_subdir = _maybe_per_day(ev_subdir, dt, group, resolved, overrides)
            subdir = ev_subdir / "Videos"
```
(Result: `{base}/{年}/2005-08-08(2d) 蒙古/0808/Videos/...` for a flagged multi-day event.)

- [ ] **Step 5: Run tests, verify pass**

Run: `python -m pytest tests/test_per_day_split.py -v`
Expected: PASS. (If event resolution needs `scan_roots`, add it to the test calls per the Step 1 note.)

- [ ] **Step 6: Run full suite**

Run: `python -m pytest -q`
Expected: PASS (existing multi-day tests unaffected — they don't set per_day_split).

- [ ] **Step 7: Commit**

```bash
git add photo_organizer/planner.py tests/test_per_day_split.py
git commit -m "feat(per-day): {mmdd}/ split for flagged multi-day events (MD3)"
```

---

### Task 4: planner — `detect_per_day_events` rule

**Files:**
- Modify: `photo_organizer/planner.py` (new `detect_per_day_events`; add `_EVENT_DAY_GAP` constant)
- Test: `tests/test_detect_per_day.py` (new)

**Rule:** an event root E (resolves to an event name) is a candidate iff: (a) ≥2 immediate child subfolders contain photos with confident dates; (b) each such child has ≥90% of its confident-dated photos on a single calendar day; (c) the children's representative days span ≥2 distinct days, consecutive within `_EVENT_DAY_GAP` (default 3); (d) total span ≤ `MAX_EVENT_SPAN_DAYS`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_detect_per_day.py`:
```python
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
    # A child with photos split ~50/50 across two days fails the 90% rule.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\Dump"
        _add(db, root + r"\sub1\a.jpg", "2005:08:08 10:00:00")
        _add(db, root + r"\sub1\b.jpg", "2005:09:20 10:00:00")  # different month
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
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_detect_per_day.py -v`
Expected: FAIL (`detect_per_day_events` not defined).

- [ ] **Step 3: Implement `detect_per_day_events`**

In `photo_organizer/planner.py`, add the constant near the other event tunables (search `MAX_EVENT_SPAN_DAYS`):
```python
_EVENT_DAY_GAP = 3          # max gap (days) between adjacent per-day subfolders
_PER_DAY_DOMINANCE = 0.90   # a subfolder must be >=90% one calendar day
```
Add the function (near `_compute_event_groups`):
```python
def detect_per_day_events(db: Database, scan_roots: list[Path] | None = None) -> list[dict]:
    """DB-only. Find event-root folders already organized into per-day subfolders:
    >=2 photo subfolders, each >=90% a single day, days spanning >=2 and roughly
    consecutive (adjacent gap <= _EVENT_DAY_GAP), total span <= MAX_EVENT_SPAN_DAYS.
    Returns [{event_folder, days:[date], span:int, subfolders:[str]}], a suggestion
    list for review --organize (the user opts in via per_day_split)."""
    from collections import Counter, defaultdict

    # subfolder -> Counter(date) over confident-dated photos
    sub_dates: dict[str, Counter] = defaultdict(Counter)
    sub_parent: dict[str, str] = {}
    for batch in db.iter_files():
        for row in batch:
            if row["status"] == "error":
                continue
            if row["file_type"] not in ("RAW", "CAMERA_JPEG", "DEV_JPEG", "HEIC"):
                continue
            if row["date_confidence"] not in ("HIGH", "MEDIUM"):
                continue
            dt = _parse_exif_dt(row["datetime_original"])
            if dt is None:
                continue
            sub = str(Path(row["path"]).parent)
            sub_dates[sub][dt.date()] += 1
            if sub not in sub_parent:
                resolved = _resolve_event_folder(Path(row["path"]), scan_roots)
                sub_parent[sub] = str(resolved) if resolved else ""

    # group single-day subfolders under their resolved event root
    by_event: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for sub, counter in sub_dates.items():
        total = sum(counter.values())
        day, n = counter.most_common(1)[0]
        if total == 0 or n / total < _PER_DAY_DOMINANCE:
            continue  # subfolder is not predominantly one day
        root = sub_parent.get(sub, "")
        if not root or root == sub:
            continue  # no event root above this subfolder
        by_event[root].append((sub, day))

    out: list[dict] = []
    for root, subs in by_event.items():
        if len(subs) < 2:
            continue
        days = sorted({d for _, d in subs})
        if len(days) < 2:
            continue
        span = (days[-1] - days[0]).days + 1
        if span > MAX_EVENT_SPAN_DAYS:
            continue
        gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
        if any(g > _EVENT_DAY_GAP for g in gaps):
            continue  # not roughly consecutive → unrelated piles
        out.append({
            "event_folder": root,
            "days": days,
            "span": span,
            "subfolders": sorted(s for s, _ in subs),
        })
    out.sort(key=lambda c: -len(c["subfolders"]))
    return out
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_detect_per_day.py -v`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add photo_organizer/planner.py tests/test_detect_per_day.py
git commit -m "feat(per-day): detect_per_day_events rule (MD4)"
```

---

### Task 5: folderorganize — per-day-split candidates, toggle, POST, reminder

**Files:**
- Modify: `photo_organizer/folderorganize.py` (`FolderOrganizeState` add per-day candidates + reminder list; `_card_html` add per_day_split toggle; JS; `/folder-override` POST accepts per_day_split)
- Test: `tests/test_folder_organize.py`

**Note:** per-day-split candidates are EVENT-ROOT folders (from `detect_per_day_events`), a different set from the existing no-event/low-date candidates (immediate parents). Render them as their own group/section with a `per_day_split` toggle; saving posts `/folder-override` with `per_day_split` keyed by the event-root path.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_folder_organize.py`:
```python
def test_per_day_candidate_listed(tmp_path):
    from photo_organizer.folderorganize import FolderOrganizeState
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        _add_file(db, root + r"\0808\a.jpg",
                  datetime_original="2005:08:08 10:00:00", date_confidence="HIGH")
        _add_file(db, root + r"\0809\b.jpg",
                  datetime_original="2005:08:09 10:00:00", date_confidence="HIGH")
        st = FolderOrganizeState(db)
        roots = {c["event_folder"] for c in st.per_day_candidates}
        assert root in roots


def test_post_per_day_split_saves(tmp_path):
    import urllib.request
    from photo_organizer.folderorganize import serve
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        _add_file(db, root + r"\0808\a.jpg",
                  datetime_original="2005:08:08 10:00:00", date_confidence="HIGH")
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({"source_folder": root, "per_day_split": 1}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-override",
                data=payload, method="POST",
                headers={"Content-Type": "application/json", "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            urllib.request.urlopen(req)
            assert db.get_folder_overrides()[root]["per_day_split"] == 1
        finally:
            httpd.shutdown()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_folder_organize.py -k "per_day" -v`
Expected: FAIL (`per_day_candidates` attribute / per_day_split not saved).

- [ ] **Step 3: Compute per-day candidates + reminder in state**

In `photo_organizer/folderorganize.py`, in `FolderOrganizeState.__init__`, after building `self.folders`, add:
```python
        from .planner import detect_per_day_events
        self.per_day_candidates = detect_per_day_events(db)
        # existing overrides so the UI can show current per_day_split state
        self._overrides = db.get_folder_overrides()
```
(If `FolderOrganizeState.__init__` does not already have `scan_roots`, call `detect_per_day_events(db)` with no scan_roots — it climbs from each file path; passing roots is optional.)

- [ ] **Step 4: Accept per_day_split in the POST handler**

In `folderorganize.py` `do_POST`, the `/folder-override` branch currently calls `set_folder_override(source_folder, event_name=..., date_override=..., updated_at=...)`. Add per_day_split passthrough:
```python
                ev = (payload.get("event_name") or "").strip() or None
                dt_ov = (payload.get("date_override") or "").strip() or None
                pds = int(payload.get("per_day_split", 0) or 0)
                with state.lock:
                    state.db.set_folder_override(
                        src, event_name=ev, date_override=dt_ov,
                        per_day_split=pds, note=None, updated_at=_now())
                    state.db.commit()
```
(Match the existing variable names in that handler — `src` is the source_folder. If event_name/date aren't part of a per-day-only POST, the `.strip() or None` keeps them None, which clears them; to AVOID clobbering an existing event_name when only toggling per_day_split, read the existing row first:)
```python
                existing = state.db.get_folder_overrides().get(src)
                ev = payload.get("event_name")
                ev = (ev.strip() or None) if ev is not None else (existing["event_name"] if existing else None)
                dt_ov = payload.get("date_override")
                dt_ov = (dt_ov.strip() or None) if dt_ov is not None else (existing["date_override"] if existing else None)
                pds = int(payload["per_day_split"]) if "per_day_split" in payload else (existing["per_day_split"] if existing else 0)
```
Use this read-existing-then-merge form so a per-day toggle POST and an event-name POST don't overwrite each other's fields.

- [ ] **Step 5: Render per-day candidates section + toggle**

In `_render_page` (folderorganize.py), add a section ABOVE the existing folder groups when `state.per_day_candidates` is non-empty:
```python
    pd_html = ""
    if state.per_day_candidates:
        rows = []
        for c in state.per_day_candidates:
            root = c["event_folder"]
            ov = state._overrides.get(root)
            checked = "checked" if (ov and ov["per_day_split"]) else ""
            rows.append(
                f'<div class="card" data-folder="{html.escape(root, quote=True)}">'
                f'<div class="path">📂 {html.escape(root)}</div>'
                f'<div class="meta">{c["span"]} 天 · {len(c["subfolders"])} 個每日子夾</div>'
                f'<label><input type="checkbox" class="pdtoggle" {checked}> '
                f'依日分夾 (per-day split)</label> '
                f'<button onclick="savePerDay(this)">save</button>'
                f'</div>'
            )
        pd_html = ('<details class="group" open><summary class="ghead">'
                   f'📂 多日活動已按日分夾 — 建議依日分夾 ({len(state.per_day_candidates)})'
                   '</summary>' + "".join(rows) + '</details>')
```
Insert `pd_html` into the page body before the existing groups. Add the JS function to `_PAGE_JS`:
```javascript
function savePerDay(btn){
  const card = btn.closest('.card');
  const folder = card.dataset.folder;
  const pds = card.querySelector('.pdtoggle').checked ? 1 : 0;
  fetch('/folder-override', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({source_folder: folder, per_day_split: pds})})
    .then(r => { if (r.ok) btn.textContent = '✓ saved'; });
}
```

- [ ] **Step 6: (Reminder section — multi-day WITHOUT per-day subfolders)**

Add a read-only reminder. In `FolderOrganizeState.__init__`, reuse `_compute_event_groups` is overkill; instead add a light check: an event root that is multi-day (its files span >1 day) but has NO qualifying per-day subfolders (not in `per_day_candidates`). Keep it simple — derive from already-loaded data:
```python
        pd_roots = {c["event_folder"] for c in self.per_day_candidates}
        # multi-day events lacking per-day subfolders → suggest manual split
        self.split_reminders = sorted(
            r for r in self._multiday_event_roots(db) if r not in pd_roots
        )
```
Add helper `_multiday_event_roots(self, db)` returning event-root folders whose confident photo dates span >1 day (DB-only; reuse `_resolve_event_folder` + a date-span per resolved root). Render `self.split_reminders` as a plain read-only `<details>` list titled "多日但無每日子夾 — 建議去檔案總管手動拆". (No toggle, no POST.) If implementing this is large, keep the reminder list to event roots only and cap display at 50 with a "+N more" line.

- [ ] **Step 7: Run tests, verify pass**

Run: `python -m pytest tests/test_folder_organize.py -v`
Expected: PASS (all, incl. the 2 new).

- [ ] **Step 8: Run full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add photo_organizer/folderorganize.py tests/test_folder_organize.py
git commit -m "feat(per-day): review --organize per-day-split candidates + toggle + reminder (MD5)"
```

---

### Task 6: Docs — CLAUDE.md event tree + naming

**Files:**
- Modify: `CLAUDE.md` (照片輸出目錄 + 影片支援 sections)

- [ ] **Step 1: Update the photo output tree section**

In `CLAUDE.md`, the "照片整理目錄 / Photo output tree" section documents `{YYYY-MM-DD}_{event}` and `{起始日}_{N}d_{event}`. Update to the new naming:
- single-day: `{YYYY}/{YYYY-MM-DD} {event}/`
- multi-day: `{YYYY}/{YYYY-MM-DD}({N}d) {event}/`
- add: multi-day events opted into per-day split (via `review --organize`) → `.../{YYYY-MM-DD}({N}d) {event}/{mmdd}/{原始檔名}`
- note subject unchanged: `{event}/{YYYY}/`

- [ ] **Step 2: Update the video co-location section**

In the "影片支援 / Video support" section, update the multi-day example folder name to the new format and note that a flagged multi-day event nests videos under `.../{YYYY-MM-DD}({N}d) {event}/{mmdd}/Videos/`.

- [ ] **Step 3: Add a `review --organize` per-day note**

Briefly document: `review --organize` now also detects already-day-organized multi-day events and offers a per-day-split toggle (`folder_overrides.per_day_split`), plus a read-only reminder for multi-day folders that still need manual splitting in Explorer (then `relocate`).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: per-day subfolder layout + unified event naming (MD6)"
```

---

## Verification (after all tasks)

```bash
python -m pytest -q          # full suite green
```
Real-library smoke (in the Run-Phase session):
```bash
python -c "from photo_organizer.db import Database; from photo_organizer.planner import detect_per_day_events; \
from pathlib import Path; \
db=Database(r'C:/PhotoTestZone/photos.db').connect(); \
print('per-day candidates:', len(detect_per_day_events(db, [Path(p) for p in ('D:/Albums','D:/DCIM','D:/DCIM_Working','D:/DCIM_Storage')])))"
```
Then `review --organize` → confirm the per-day-split section renders the 蒙古-style events; toggle + save; `plan --force` → spot-check a flagged event files into `{YYYY-MM-DD}({N}d) {event}/{mmdd}/`.

## Notes / known follow-ups
- `add` (incremental) path does not pass `event_base`/per_day overrides; per-day split applies on full `plan` only. Out of scope here (documented limitation).
- Global event-name rename means previously-executed folders (old `_` format) differ from new output; only matters if re-planned. User is pre-execute.
