# Folder-merge Detection + Table Implementation Plan — S2 Plan 2a of (a,b,c)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Productionize the validated folder-overlap detection spike into a `folder-merge` command that finds "twin" folders (two folders that are essentially the same SHA-256 set — wholesale copies / "兩串很接近的粽子") and records each pair in a new `folder_overlaps` table, with a suggested keeper.

**Architecture:** A new DB-only analysis pass `folder_merge.compute_folder_overlaps()` (lifts `scripts/spike_folder_overlaps.py`, already validated on the real 280k-file library → 495 twin pairs). It builds subtree-union SHA sets per folder, an inverted `sha→folders` index, finds pairs whose mutual coverage ≥ threshold (Twin semantics, spec D2), rolls them up to the highest twin ancestor, and picks a keeper by penalizing backup/container ancestors. Results persist to `folder_overlaps` (schema v4). Decisions/UI/merge are later plans (2b, 2c).

**Tech Stack:** Python 3.11, stdlib sqlite3, existing `photo_organizer` modules (db, progress, config), pytest.

**Scope:** Plan 2a only — detection + table + CLI that POPULATES the table. NOT in scope: the `review --web --folders` UI (2b), the plan/execute merge that acts on keeper decisions (2c).

**Lessons baked in from the relocate/prune incident:**
- The detection iterates the whole library → wrap the file scan in a progress bar (`PhaseProgress`), so a long run is visibly progressing, not seemingly hung.
- Unit tests use tiny data and cannot catch real-scale perf cliffs → Task 4 is a mandatory real-library validation run with timing, on the actual DB (read-only — detection never mutates `files`).
- The DB was just pruned to 243,361 rows; detection runs on that clean state.

> **Commit workflow:** Commits go through the separate "SCM GitHub" session — skip the inline `git commit` steps; hand staged changes to SCM.

Pre-verified facts:
- `SCHEMA_VERSION = 3` at `db.py:175`; `SCHEMA_SQL` runs `CREATE TABLE IF NOT EXISTS …` on every `connect()`, so adding a table there + bumping the version brings old DBs up automatically (no explicit migration needed for a NEW table). FK enforcement is ON (`db.py:399`).
- `db.path` is the DB Path (`db.py:385`); `cfg.input_dirs` are the scan roots (`config.py`); CLI parser builder is `build_parser()`; command pattern = `cmd_X` + `sub.add_parser(...)` + `set_defaults(func=cmd_X)`.
- `PhaseProgress("label", total=N, phase="...")` context manager with `prog.advance(1, current_path=...)` (see `relocate.find_stale_rows`, `reconcile.py`).
- Validated spike lives at `scripts/spike_folder_overlaps.py` — the algorithm below is lifted from it verbatim where possible.

---

## File Structure

- **Modify** `photo_organizer/db.py` — add `folder_overlaps` to `SCHEMA_SQL`, bump `SCHEMA_VERSION` to 4, add helper methods (`clear_folder_overlaps`, `insert_folder_overlap`, `iter_folder_overlaps`).
- **Create** `photo_organizer/folder_merge.py` — `compute_folder_overlaps()` + `_pick_keeper`/`_noncanonical_score`, and `detect_and_store()`.
- **Modify** `photo_organizer/__main__.py` — `cmd_folder_merge` + `folder-merge` subparser.
- **Create** `tests/test_folder_merge.py` — detection + persistence tests.

---

### Task 1: `folder_overlaps` table (schema v4) + DB helpers

**Files:**
- Modify: `photo_organizer/db.py` (SCHEMA_SQL, SCHEMA_VERSION, helper methods)
- Test: `tests/test_folder_merge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_folder_merge.py
from __future__ import annotations

from photo_organizer.db import Database


def test_folder_overlaps_table_and_helpers(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        db.insert_folder_overlap(
            folder_a="D:\\A", folder_b="D:\\B",
            shared_count=10, a_only_count=0, b_only_count=2,
            coverage_a=1.0, coverage_b=0.83, keeper="b",
        )
        db.commit()
        rows = list(db.iter_folder_overlaps())
        assert len(rows) == 1
        r = rows[0]
        assert r["folder_a"] == "D:\\A" and r["folder_b"] == "D:\\B"
        assert r["shared_count"] == 10 and r["keeper"] == "b"
        assert r["status"] == "pending"

        db.clear_folder_overlaps()
        db.commit()
        assert list(db.iter_folder_overlaps()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_folder_merge.py::test_folder_overlaps_table_and_helpers -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'insert_folder_overlap'`

- [ ] **Step 3: Write minimal implementation**

In `db.py`, bump the version:
```python
SCHEMA_VERSION = 4
```

Add this table to the `SCHEMA_SQL` string (place it after the `duplicates` table block):
```sql
CREATE TABLE IF NOT EXISTS folder_overlaps (
    overlap_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_a     TEXT    NOT NULL,
    folder_b     TEXT    NOT NULL,
    shared_count INTEGER NOT NULL,
    a_only_count INTEGER NOT NULL,
    b_only_count INTEGER NOT NULL,
    coverage_a   REAL    NOT NULL,
    coverage_b   REAL    NOT NULL,
    keeper       TEXT    CHECK(keeper IN ('a','b') OR keeper IS NULL),
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','reviewed')),
    reviewed_at  TEXT,
    UNIQUE(folder_a, folder_b)
);
```

Add these methods to the `Database` class (near the duplicates helpers):
```python
    # ---- folder_overlaps ---------------------------------------------------

    def clear_folder_overlaps(self):
        self.conn.execute("DELETE FROM folder_overlaps")

    def insert_folder_overlap(self, *, folder_a, folder_b, shared_count,
                              a_only_count, b_only_count, coverage_a,
                              coverage_b, keeper=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO folder_overlaps "
            "(folder_a, folder_b, shared_count, a_only_count, b_only_count, "
            " coverage_a, coverage_b, keeper) VALUES (?,?,?,?,?,?,?,?)",
            (folder_a, folder_b, shared_count, a_only_count, b_only_count,
             coverage_a, coverage_b, keeper),
        )

    def iter_folder_overlaps(self, pending_only: bool = False):
        sql = "SELECT * FROM folder_overlaps"
        if pending_only:
            sql += " WHERE status='pending'"
        sql += " ORDER BY shared_count DESC"
        return self.conn.execute(sql).fetchall()
```

> Note: a pre-existing DB created at version 3 will get the new table because `SCHEMA_SQL` (with `CREATE TABLE IF NOT EXISTS`) runs on every `connect()`. Confirm by grepping `db.py` for where `SCHEMA_SQL` is executed in `connect()`/`__init__`; if the schema is only run for brand-new files, add `conn.executescript` of the folder_overlaps CREATE in `_apply_migrations` instead. (Verify before relying on it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_folder_merge.py::test_folder_overlaps_table_and_helpers -v`
Expected: PASS

- [ ] **Step 5: Commit** (hand to SCM)

```bash
git add photo_organizer/db.py tests/test_folder_merge.py
git commit -m "feat(db): folder_overlaps table (schema v4) + helpers"
```

---

### Task 2: `compute_folder_overlaps` — the detection (lifted from the validated spike)

**Files:**
- Create: `photo_organizer/folder_merge.py`
- Test: `tests/test_folder_merge.py`

- [ ] **Step 1: Write the failing test**

```python
def test_compute_folder_overlaps_twin_and_keeper(tmp_path):
    from photo_organizer.folder_merge import compute_folder_overlaps
    from photo_organizer.db import Database

    db_path = tmp_path / ".photo_organizer" / "library.db"
    # Two leaf folders under one scan root with the SAME 6 shas → twins.
    # 'backup' side should be folded (keeper = the clean side).
    def add(db, path, sha):
        db.conn.execute(
            "INSERT INTO files (path, filename, extension, file_type, status, sha256) "
            "VALUES (?,?,?,?, 'hashed', ?)",
            (path, path.split("\\")[-1], "jpg", "CAMERA_JPEG", sha),
        )
    with Database(db_path) as db:
        for i in range(6):
            add(db, f"D:\\Lib\\Trip\\IMG_{i}.jpg", f"sha{i}")
            add(db, f"D:\\Lib\\backup\\Trip\\IMG_{i}.jpg", f"sha{i}")
        db.commit()

        overlaps = compute_folder_overlaps(db, ["D:\\Lib"], coverage=0.95, min_shared=3)

        assert len(overlaps) == 1
        o = overlaps[0]
        assert {o["folder_a"], o["folder_b"]} == {"D:\\Lib\\Trip", "D:\\Lib\\backup\\Trip"}
        assert o["shared_count"] == 6
        assert o["coverage_a"] == 1.0 and o["coverage_b"] == 1.0
        # keeper is the side NOT under 'backup'
        keeper_folder = o["folder_a"] if o["keeper"] == "a" else o["folder_b"]
        assert keeper_folder == "D:\\Lib\\Trip"


def test_compute_folder_overlaps_non_twin_not_flagged(tmp_path):
    from photo_organizer.folder_merge import compute_folder_overlaps
    from photo_organizer.db import Database

    db_path = tmp_path / ".photo_organizer" / "library.db"
    def add(db, path, sha):
        db.conn.execute(
            "INSERT INTO files (path, filename, extension, file_type, status, sha256) "
            "VALUES (?,?,?,?, 'hashed', ?)",
            (path, path.split("\\")[-1], "jpg", "CAMERA_JPEG", sha),
        )
    with Database(db_path) as db:
        # A has 10 unique, B shares only 2 of them → min-coverage low → not a twin.
        for i in range(10):
            add(db, f"D:\\Lib\\A\\a{i}.jpg", f"s{i}")
        add(db, "D:\\Lib\\B\\b0.jpg", "s0")
        add(db, "D:\\Lib\\B\\b1.jpg", "s1")
        db.commit()
        overlaps = compute_folder_overlaps(db, ["D:\\Lib"], coverage=0.95, min_shared=2)
        assert overlaps == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_folder_merge.py -k compute_folder_overlaps -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'photo_organizer.folder_merge'`

- [ ] **Step 3: Write minimal implementation**

```python
# photo_organizer/folder_merge.py
"""
folder_merge.py — detect "twin" folders: two folders that hold essentially the
SAME set of files (by exact SHA-256), i.e. one is a wholesale copy of the other.

DB-only, read-only over `files`: builds subtree-union SHA sets per folder, an
inverted sha→folders index, finds folder pairs whose MUTUAL coverage ≥ threshold
(Twin semantics — both sides are ~the same set), rolls them up to the highest
twin ancestor, and suggests a keeper (the side with fewer backup/container
markers). Validated against the real 280k-file library via
scripts/spike_folder_overlaps.py. Results are recorded in `folder_overlaps`;
deciding/merging is done later (review UI + plan/execute).
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from pathlib import PureWindowsPath

from .db import Database
from .progress import PhaseProgress

# Lowercased substrings marking the LESS canonical side (fold away from these).
_NONCANONICAL = (
    "all in lightroom", "depository", "rawtank", "jpegtank", "raw.old",
    "raw_old", ".out", "完整備份", "待整理", "archive", "backup",
    "- copy", "_copy", " copy", "copy of", "temp", "tmp", ".old", ".bak",
)


def _parent(path: str) -> str:
    return str(PureWindowsPath(path).parent)


def _is_root(path: str) -> bool:
    p = PureWindowsPath(path)
    return p.parent == p


def _noncanonical_score(folder: str) -> int:
    low = folder.lower()
    return sum(1 for tok in _NONCANONICAL if tok in low)


def _pick_keeper(a: str, b: str) -> str:
    """'a' or 'b' — the more canonical side (fewer backup markers, then shorter
    path, then lexicographically smaller)."""
    sa, sb = _noncanonical_score(a), _noncanonical_score(b)
    if sa != sb:
        return "a" if sa < sb else "b"
    if len(a) != len(b):
        return "a" if len(a) < len(b) else "b"
    return "a" if a <= b else "b"


def compute_folder_overlaps(
    db: Database, scan_roots: list, *, coverage: float = 0.95,
    min_shared: int = 5, ubiquitous_cap: int = 30, show_progress: bool = False,
) -> list[dict]:
    """Return rolled-up twin-folder pairs (see module docstring)."""
    roots = {str(PureWindowsPath(r)) for r in scan_roots}

    rows = db.conn.execute(
        "SELECT path, sha256 FROM files "
        "WHERE sha256 IS NOT NULL AND status != 'error'"
    ).fetchall()

    folder_shas: dict[str, set] = defaultdict(set)

    def _accumulate(path: str, sha: str) -> None:
        fld = _parent(path)
        folder_shas[fld].add(sha)
        cur = fld
        while cur not in roots and not _is_root(cur):
            cur = _parent(cur)
            folder_shas[cur].add(sha)
            if cur in roots:
                break

    if show_progress:
        with PhaseProgress(
            "Indexing folders", total=len(rows), phase="folder-merge"
        ) as prog:
            for r in rows:
                _accumulate(r["path"], r["sha256"])
                prog.advance(1)
    else:
        for r in rows:
            _accumulate(r["path"], r["sha256"])

    # Inverted index over the subtree-union folder sets.
    sha_folders: dict[str, set] = defaultdict(set)
    for fld, shas in folder_shas.items():
        for sha in shas:
            sha_folders[sha].add(fld)

    pair_shared: dict[tuple, int] = defaultdict(int)
    for folders in sha_folders.values():
        if 2 <= len(folders) <= ubiquitous_cap:
            for a, b in combinations(sorted(folders), 2):
                # Skip same-tree ancestor/descendant pairs (containment in ONE
                # tree, not a duplicate).
                if a.startswith(b + "\\") or b.startswith(a + "\\"):
                    continue
                pair_shared[(a, b)] += 1

    flagged: dict[tuple, dict] = {}
    for (a, b), shared in pair_shared.items():
        if shared < min_shared:
            continue
        na, nb = len(folder_shas[a]), len(folder_shas[b])
        cov_a, cov_b = shared / na, shared / nb
        if min(cov_a, cov_b) >= coverage:  # Twin: both sides ~same set
            flagged[(a, b)] = {
                "folder_a": a, "folder_b": b, "shared_count": shared,
                "a_only_count": na - shared, "b_only_count": nb - shared,
                "coverage_a": cov_a, "coverage_b": cov_b,
                "keeper": _pick_keeper(a, b),
            }

    # Rollup: drop a pair if ANY ancestor pair (walked in lockstep) is a twin.
    flagged_keys = set(flagged)
    out: list[dict] = []
    for (a, b), rec in flagged.items():
        pa, pb, suppressed = a, b, False
        while True:
            pa, pb = _parent(pa), _parent(pb)
            if pa == pb or _is_root(pa) or _is_root(pb):
                break
            if tuple(sorted((pa, pb))) in flagged_keys:
                suppressed = True
                break
        if not suppressed:
            out.append(rec)

    out.sort(key=lambda r: -r["shared_count"])
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_folder_merge.py -k compute_folder_overlaps -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit** (hand to SCM)

```bash
git add photo_organizer/folder_merge.py tests/test_folder_merge.py
git commit -m "feat(folder-merge): twin-folder detection (subtree union, rollup, keeper)"
```

---

### Task 3: `detect_and_store` + `folder-merge` CLI

**Files:**
- Modify: `photo_organizer/folder_merge.py` (add `detect_and_store`)
- Modify: `photo_organizer/__main__.py` (`cmd_folder_merge` + subparser)
- Test: `tests/test_folder_merge.py`

- [ ] **Step 1: Write the failing test**

```python
def test_detect_and_store_persists_overlaps(tmp_path):
    from photo_organizer.folder_merge import detect_and_store
    from photo_organizer.db import Database

    db_path = tmp_path / ".photo_organizer" / "library.db"
    def add(db, path, sha):
        db.conn.execute(
            "INSERT INTO files (path, filename, extension, file_type, status, sha256) "
            "VALUES (?,?,?,?, 'hashed', ?)",
            (path, path.split("\\")[-1], "jpg", "CAMERA_JPEG", sha),
        )
    with Database(db_path) as db:
        for i in range(6):
            add(db, f"D:\\Lib\\Trip\\IMG_{i}.jpg", f"sha{i}")
            add(db, f"D:\\Lib\\backup\\Trip\\IMG_{i}.jpg", f"sha{i}")
        db.commit()

        n = detect_and_store(db, ["D:\\Lib"], coverage=0.95, min_shared=3)
        assert n == 1
        rows = list(db.iter_folder_overlaps())
        assert len(rows) == 1 and rows[0]["shared_count"] == 6

        # Idempotent: a second run clears then re-inserts, still 1 row.
        n2 = detect_and_store(db, ["D:\\Lib"], coverage=0.95, min_shared=3)
        assert n2 == 1 and len(list(db.iter_folder_overlaps())) == 1


def test_cli_folder_merge_wired():
    import photo_organizer.__main__ as m
    parser = m.build_parser()
    args = parser.parse_args(["folder-merge", "--db", "x.db"])
    assert args.func is m.cmd_folder_merge
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_folder_merge.py -k "detect_and_store or cli_folder_merge" -v`
Expected: FAIL — `ImportError: cannot import name 'detect_and_store'`

- [ ] **Step 3: Write minimal implementation**

Add to `folder_merge.py`:
```python
def detect_and_store(db: Database, scan_roots: list, *, coverage: float = 0.95,
                     min_shared: int = 5, show_progress: bool = False) -> int:
    """Compute twin-folder overlaps and replace the folder_overlaps table with
    them (clears prior pending detection first). Returns the number stored."""
    overlaps = compute_folder_overlaps(
        db, scan_roots, coverage=coverage, min_shared=min_shared,
        show_progress=show_progress,
    )
    db.clear_folder_overlaps()
    for o in overlaps:
        db.insert_folder_overlap(
            folder_a=o["folder_a"], folder_b=o["folder_b"],
            shared_count=o["shared_count"], a_only_count=o["a_only_count"],
            b_only_count=o["b_only_count"], coverage_a=o["coverage_a"],
            coverage_b=o["coverage_b"], keeper=o["keeper"],
        )
    db.commit()
    return len(overlaps)
```

Add to `__main__.py` (alongside other `cmd_*`):
```python
def cmd_folder_merge(args):
    cfg = _load_cfg(args)
    from .folder_merge import detect_and_store
    scan_roots = list(cfg.input_dirs) if cfg else []
    if not scan_roots:
        print_error("folder-merge needs scan roots: --config with input_dirs.")
        sys.exit(1)
    with Database(_db_path(args, cfg)) as db:
        n = detect_and_store(db, scan_roots, show_progress=True)
    console.print(f"folder-merge: {n:,} twin-folder pair(s) recorded in folder_overlaps.")
```

In `build_parser()`, alongside the other subparsers:
```python
    p_fmerge = sub.add_parser(
        "folder-merge", parents=[shared],
        help="Detect twin folders (wholesale copies) → folder_overlaps table",
    )
    p_fmerge.set_defaults(func=cmd_folder_merge)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_folder_merge.py -v`
Expected: PASS (all)

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: PASS (173 prior + new folder_merge tests), exit 0.

- [ ] **Step 6: Commit** (hand to SCM)

```bash
git add photo_organizer/folder_merge.py photo_organizer/__main__.py tests/test_folder_merge.py
git commit -m "feat(folder-merge): detect_and_store + folder-merge CLI"
```

---

### Task 4: Real-library validation (manual — read-only, with timing)

**Files:** none (runs on the real DB; detection never mutates `files`).

- [ ] **Step 1: Run folder-merge on the real (now-pruned) library**

Run: `python -m photo_organizer folder-merge --config config.json`
Expected: a progress bar ("Indexing folders"), then `folder-merge: N twin-folder pair(s) recorded…`. N should be modest (the spike found 495 BEFORE the prune; after pruning the duplicate trees it will be fewer). Note the wall-clock time.

- [ ] **Step 2: Sanity-check the table**

Run:
```bash
python -c "import sqlite3; c=sqlite3.connect('C:/PhotoTestZone/photos.db'); c.row_factory=sqlite3.Row; \
print('overlaps:', c.execute('SELECT COUNT(*) FROM folder_overlaps').fetchone()[0]); \
[print(r['shared_count'], r['keeper'], '|', r['folder_a'], 'VS', r['folder_b']) for r in c.execute('SELECT * FROM folder_overlaps ORDER BY shared_count DESC LIMIT 15')]"
```
Expected: rows look like genuine twin pairs; keeper points at the more-canonical side. If it takes far longer than a couple of minutes or memory balloons, STOP and report (perf cliff) before proceeding to 2b.

---

## Self-Review

**Spec coverage (D1/D2/D3 + §3.1/§3.2):**
- exact SHA only (D1) → detection reads `sha256`, no pHash ✓
- Twin / mutual coverage (D2) → `min(cov_a, cov_b) >= coverage` ✓
- every-level subtree union + highest-twin rollup (D3) → `_accumulate` climbs to scan roots; lockstep ancestor suppression ✓
- inverted index, skip ubiquitous, skip same-tree prefix (§3.1) ✓
- keeper heuristic penalizing backup/container (§3.3) → `_pick_keeper` ✓
- `folder_overlaps` table with keeper/status (§3.2) → Task 1 ✓
- progress bar (incident lesson) → `show_progress` + `PhaseProgress` ✓
- real-data validation (incident lesson) → Task 4 ✓

**Placeholder scan:** none. Task 1's note about where `SCHEMA_SQL` runs is a verification instruction (grep target given), not a placeholder.

**Type consistency:** `compute_folder_overlaps(...) -> list[dict]` with keys folder_a/folder_b/shared_count/a_only_count/b_only_count/coverage_a/coverage_b/keeper; `detect_and_store` consumes those exact keys and feeds `insert_folder_overlap(**)`; `iter_folder_overlaps` returns rows with the same column names. Consistent across tasks.

---

## Execution Handoff

Subagent-Driven. After 2a lands and the real-library run looks right, write Plan 2b (`review --web --folders` UI) and 2c (plan/execute merge integration).
