# Relocate (SHA-256 re-point) Implementation Plan — S1 Plan 1 of 2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `relocate` command that re-points `files.path` for files the user manually moved, by matching stale DB rows to their new on-disk location via SHA-256 — preserving `file_id` and every organizing decision, without rebuilding the DB.

**Architecture:** A new read-mostly module `relocate.py`. It finds DB rows whose `path` no longer exists on disk, discovers on-disk files not yet in the DB, hashes only those, and matches stale rows to new paths by identical SHA-256 (content survives a move → hash is unchanged). Only `files.path`/`mtime` is UPDATEd; all `duplicates`/`operations`/date-forensics/review decisions stay attached to the same `file_id`. Rows with no SHA match are logged as LOST (`run_log` phase='relocate'); nothing on disk is moved or deleted.

**Tech Stack:** Python 3.11, stdlib `sqlite3`/`os`/`hashlib`, existing `photo_organizer` modules (`scanner.discover_files`, `classifier.FileClassifier`, `deduper._sha256_file`, `db.Database.update_file`/`db.log`), pytest.

**Scope note:** This is Plan 1 of the S1 set (per the design spec `docs/superpowers/specs/2026-06-17-folder-merge-dedup-design.md` §3.5 / D8). It is independently shippable: it makes `execute` reliable after manual moves regardless of the folder-merge feature. Plan 2 (folder-merge detection + table + `review --web --folders` UI + plan/execute integration) follows.

---

## File Structure

- **Create** `photo_organizer/relocate.py` — all relocate logic (one responsibility: re-point stale paths by content hash).
- **Modify** `photo_organizer/__main__.py` — add `cmd_relocate` + a `relocate` subparser (mirrors `cmd_reconcile` at `__main__.py:313`).
- **Create** `tests/test_relocate.py` — DB-level tests using real temp files (mirrors `tests/test_add.py` fixture style).

Pre-verified facts this plan relies on (already confirmed against the codebase):
- `db.update_file(file_id, **kwargs)` exists; `path` and `mtime` are in `_UPDATABLE_FILE_COLS` (`db.py:528`). It does NOT commit (caller batches).
- `db.log(level, message, phase=None, path=None, file_id=None)` exists.
- `scanner.discover_files(root: Path, classifier: FileClassifier) -> list[Path]` (`scanner.py:56`) skips `_staging`/hidden dirs.
- `classifier.FileClassifier(use_secondary_signals=False)` ctor (`classifier.py:140`); `.is_supported(path)`.
- `deduper._sha256_file(path: Path) -> str | None` (`deduper.py:48`).
- Test DB rows are inserted via `db.conn.execute("INSERT INTO files (...) VALUES (...)")` (see `tests/test_add.py:34`).
- `mtime` is stored ISO-8601 (e.g. `"2023-06-15T10:30:00+00:00"`).

> **Commit workflow:** This project commits in the separate "SCM GitHub" session (see `CLAUDE.md`). The `git commit` steps below are written for completeness of the TDD loop; when executing, hand the staged commit to the SCM session/subagent rather than committing inline.

---

### Task 1: `find_stale_rows` — list DB rows whose path is gone

**Files:**
- Create: `photo_organizer/relocate.py`
- Test: `tests/test_relocate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_relocate.py
from __future__ import annotations

from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.relocate import find_stale_rows, relocate


def _add(db, path, *, sha256, status="hashed", filename=None, mtime=None):
    p = Path(path)
    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status, "
        "sha256, mtime) VALUES (?,?,?,?,?,?,?)",
        (str(p), filename or p.name, p.suffix.lstrip(".").lower(),
         "CAMERA_JPEG", status, sha256, mtime),
    )
    return cur.lastrowid


def test_find_stale_rows_flags_only_missing_paths(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    present = tmp_path / "here.jpg"
    present.write_bytes(b"x")
    with Database(db_path) as db:
        gone = _add(db, tmp_path / "gone.jpg", sha256="aaaa")
        _add(db, present, sha256="bbbb")
        db.commit()

        stale = find_stale_rows(db)
        assert [r["file_id"] for r in stale] == [gone]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relocate.py::test_find_stale_rows_flags_only_missing_paths -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'photo_organizer.relocate'`

- [ ] **Step 3: Write minimal implementation**

```python
# photo_organizer/relocate.py
"""
relocate.py — re-point files.path for manually-moved files via SHA-256.

A moved file keeps its bytes, so its SHA-256 is unchanged. This finds DB rows
whose path no longer exists on disk, discovers on-disk files not yet in the DB,
hashes only those, and matches stale rows to their new location by identical
sha256 — updating only files.path/mtime so file_id and every organizing decision
(duplicates / operations / date forensics / review) stay intact.

Read-mostly: never moves or deletes a file. Rows with no sha256 match on disk
are logged as LOST (run_log phase='relocate').
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from .db import Database


def find_stale_rows(db: Database) -> list:
    """Return non-error file rows whose recorded path no longer exists."""
    rows = db.conn.execute(
        "SELECT file_id, path, filename, sha256 FROM files "
        "WHERE sha256 IS NOT NULL AND status != 'error'"
    ).fetchall()
    return [r for r in rows if not os.path.exists(r["path"])]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_relocate.py::test_find_stale_rows_flags_only_missing_paths -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add photo_organizer/relocate.py tests/test_relocate.py
git commit -m "feat(relocate): find_stale_rows — list DB rows whose path is gone"
```

---

### Task 2: `_match_rows_to_paths` — pair stale rows to new paths within one SHA

**Files:**
- Modify: `photo_organizer/relocate.py`
- Test: `tests/test_relocate.py`

Multiple files can share one SHA-256 (identical bytes copied N times). Within a
single SHA bucket, pair stale rows to candidate new paths by **identical filename
first**, then leftovers by sorted order, so we never swap one identical copy's
identity for another's arbitrarily.

- [ ] **Step 1: Write the failing test**

```python
def test_match_rows_to_paths_prefers_filename_then_order():
    from photo_organizer.relocate import _match_rows_to_paths

    rows = [
        {"file_id": 1, "filename": "IMG_1.jpg"},
        {"file_id": 2, "filename": "IMG_2.jpg"},
    ]
    cands = [Path("D:/new/IMG_2.jpg"), Path("D:/new/IMG_1.jpg")]
    matched = _match_rows_to_paths(rows, cands)
    by_id = {row["file_id"]: str(p) for row, p in matched}
    assert by_id == {1: "D:\\new\\IMG_1.jpg".replace("\\", os.sep)
                     if False else str(Path("D:/new/IMG_1.jpg")),
                     2: str(Path("D:/new/IMG_2.jpg"))}


def test_match_rows_to_paths_falls_back_to_order_when_names_differ():
    from photo_organizer.relocate import _match_rows_to_paths

    rows = [{"file_id": 1, "filename": "old_a.jpg"},
            {"file_id": 2, "filename": "old_b.jpg"}]
    cands = [Path("D:/new/z.jpg"), Path("D:/new/a.jpg")]
    matched = _match_rows_to_paths(rows, cands)
    # 2 rows, 2 candidates → both matched (order-based), none dropped
    assert len(matched) == 2
    assert {str(p) for _, p in matched} == {str(c) for c in cands}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relocate.py -k match_rows_to_paths -v`
Expected: FAIL — `ImportError: cannot import name '_match_rows_to_paths'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to photo_organizer/relocate.py

def _match_rows_to_paths(rows: list, cands: list) -> list:
    """Pair stale rows to candidate new paths (same sha256 bucket).

    Identical filename first, then leftovers by sorted path order. Returns a
    list of (row, new_path). Rows with no candidate are simply absent.
    """
    by_name: dict[str, list] = defaultdict(list)
    for p in cands:
        by_name[p.name].append(p)

    out: list = []
    remaining_rows: list = []
    for row in rows:
        bucket = by_name.get(row["filename"])
        if bucket:
            out.append((row, bucket.pop(0)))
        else:
            remaining_rows.append(row)

    leftover = sorted((p for b in by_name.values() for p in b), key=str)
    for row, p in zip(remaining_rows, leftover):
        out.append((row, p))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_relocate.py -k match_rows_to_paths -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add photo_organizer/relocate.py tests/test_relocate.py
git commit -m "feat(relocate): _match_rows_to_paths — filename-first pairing within a sha bucket"
```

---

### Task 3: `relocate` — discover, hash, re-point, and log LOST

**Files:**
- Modify: `photo_organizer/relocate.py`
- Test: `tests/test_relocate.py`

- [ ] **Step 1: Write the failing test**

```python
def test_relocate_repoints_moved_file_and_preserves_decisions(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    scan_root = tmp_path / "lib"
    old_dir = scan_root / "OldFolder"
    new_dir = scan_root / "MovedFolder"
    new_dir.mkdir(parents=True)

    # The file physically lives at its NEW location already (user moved it),
    # but the DB still records the OLD path.
    content = b"hello-photo-bytes"
    import hashlib
    sha = hashlib.sha256(content).hexdigest()
    (new_dir / "IMG_1.jpg").write_bytes(content)

    with Database(db_path) as db:
        fid = _add(db, old_dir / "IMG_1.jpg", sha256=sha)
        # A decision attached to this file_id (must survive relocate).
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, status) "
            "VALUES (?, 'MOVE', ?, 'planned')",
            (fid, str(old_dir / "IMG_1.jpg")),
        )
        db.commit()

        summary = relocate(db, [scan_root])

        assert summary == {"stale": 1, "relocated": 1, "lost": 0}
        row = db.conn.execute(
            "SELECT path FROM files WHERE file_id = ?", (fid,)
        ).fetchone()
        assert row["path"] == str(new_dir / "IMG_1.jpg")
        # The decision row is still attached to the SAME file_id.
        op = db.conn.execute(
            "SELECT op_type FROM operations WHERE file_id = ?", (fid,)
        ).fetchone()
        assert op["op_type"] == "MOVE"


def test_relocate_logs_lost_when_no_sha_match(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    scan_root = tmp_path / "lib"
    scan_root.mkdir(parents=True)
    with Database(db_path) as db:
        fid = _add(db, scan_root / "deleted.jpg", sha256="deadbeef")
        db.commit()

        summary = relocate(db, [scan_root])
        assert summary == {"stale": 1, "relocated": 0, "lost": 1}
        log = db.conn.execute(
            "SELECT message FROM run_log WHERE phase='relocate' "
            "AND file_id = ?", (fid,)
        ).fetchone()
        assert log is not None and "LOST" in log["message"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relocate.py -k relocate_ -v`
Expected: FAIL — `relocate()` not yet implemented (current stub absent)

- [ ] **Step 3: Write minimal implementation**

```python
# add to photo_organizer/relocate.py
def relocate(db: Database, scan_roots: list) -> dict:
    """Re-point stale file rows to their moved location by sha256.

    Returns {"stale": int, "relocated": int, "lost": int}. Updates only
    files.path/mtime; logs unmatched (truly missing) rows as LOST.
    """
    from datetime import datetime, timezone

    from .classifier import FileClassifier
    from .deduper import _sha256_file
    from .scanner import discover_files

    stale = find_stale_rows(db)
    if not stale:
        return {"stale": 0, "relocated": 0, "lost": 0}

    known = {
        r["path"]
        for r in db.conn.execute("SELECT path FROM files").fetchall()
    }
    classifier = FileClassifier()
    discovered: list[Path] = []
    for root in scan_roots:
        discovered.extend(discover_files(Path(root), classifier))
    unknown = [p for p in discovered if str(p) not in known]

    new_by_sha: dict[str, list] = defaultdict(list)
    for p in unknown:
        digest = _sha256_file(p)
        if digest:
            new_by_sha[digest].append(p)

    stale_by_sha: dict[str, list] = defaultdict(list)
    for r in stale:
        stale_by_sha[r["sha256"]].append(r)

    relocated = 0
    lost: list = []
    used: set[str] = set()
    for sha, rows in stale_by_sha.items():
        cands = [p for p in new_by_sha.get(sha, []) if str(p) not in used]
        matched = _match_rows_to_paths(rows, cands)
        matched_ids = set()
        for row, newp in matched:
            mt = datetime.fromtimestamp(
                newp.stat().st_mtime, tz=timezone.utc
            ).isoformat()
            db.update_file(row["file_id"], path=str(newp), mtime=mt)
            db.log(
                "INFO", f"Relocated by sha256: {row['path']} -> {newp}",
                phase="relocate", file_id=row["file_id"], path=str(newp),
            )
            used.add(str(newp))
            matched_ids.add(row["file_id"])
            relocated += 1
        lost.extend(r for r in rows if r["file_id"] not in matched_ids)

    for r in lost:
        db.log(
            "WARN",
            f"LOST — path missing, no sha256 match on disk: {r['path']}",
            phase="relocate", file_id=r["file_id"], path=r["path"],
        )
    db.commit()
    return {"stale": len(stale), "relocated": relocated, "lost": len(lost)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_relocate.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add photo_organizer/relocate.py tests/test_relocate.py
git commit -m "feat(relocate): re-point moved files by sha256, log LOST, preserve decisions"
```

---

### Task 4: CLI — `relocate` subcommand

**Files:**
- Modify: `photo_organizer/__main__.py` (add `cmd_relocate` near `cmd_reconcile:313`; add subparser near `p_recon:567`)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_relocate.py
def test_cli_relocate_command_is_wired():
    import photo_organizer.__main__ as m

    parser = m.build_parser() if hasattr(m, "build_parser") else m._build_parser()
    args = parser.parse_args(["relocate", "--db", "x.db"])
    assert args.func is m.cmd_relocate
```

> Note: if the parser builder has a different name, grep `__main__.py` for `add_subparsers` (currently `__main__.py:450`) and use that function's name in the test.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relocate.py -k cli_relocate -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'cmd_relocate'`

- [ ] **Step 3: Write minimal implementation**

```python
# photo_organizer/__main__.py — add alongside the other cmd_* functions
def cmd_relocate(args):
    cfg = _load_cfg(args)
    from .relocate import relocate
    scan_roots = list(cfg.input_dirs) if cfg else [getattr(args, "root", None)]
    scan_roots = [r for r in scan_roots if r]
    if not scan_roots:
        print_error("relocate needs scan roots: --config with input_dirs, or a ROOT arg.")
        sys.exit(1)
    with Database(_db_path(args, cfg)) as db:
        summary = relocate(db, scan_roots)
    console.print(
        f"relocate: {summary['relocated']:,} re-pointed, "
        f"{summary['lost']:,} LOST (of {summary['stale']:,} stale)."
    )
    sys.exit(0 if summary["lost"] == 0 else 1)
```

```python
# in the parser-building function, alongside p_recon (__main__.py:567)
    p_reloc = sub.add_parser(
        "relocate", parents=[shared],
        help="Re-point files.path for manually-moved files via sha256 (no rebuild)",
    )
    p_reloc.add_argument("root", nargs="?", help="Optional scan root (else config input_dirs)")
    p_reloc.set_defaults(func=cmd_relocate)
```

> `shared`, `sub`, `_load_cfg`, `_db_path`, `print_error`, `console` are all already in scope in `__main__.py` (used by every other command). `cfg.input_dirs` is the config field (`config.py:46`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_relocate.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python -m pytest -q`
Expected: PASS — prior baseline (156) + new relocate tests, exit 0.

- [ ] **Step 6: Commit**

```bash
git add photo_organizer/__main__.py tests/test_relocate.py
git commit -m "feat(relocate): wire 'relocate' CLI subcommand"
```

---

### Task 5: Real-library smoke (manual, no test file)

**Files:** none (manual verification against the user's real DB)

- [ ] **Step 1: Dry-look — how many rows are stale right now**

Run:
```bash
python -c "from photo_organizer.db import Database; from photo_organizer.relocate import find_stale_rows; \
import sys; db=Database('C:/PhotoTestZone/photos.db'); \
print('stale rows:', len(find_stale_rows(db)))"
```
Expected: a small number (the folders the user manually moved).

- [ ] **Step 2: Run relocate against the real library**

Run: `python -m photo_organizer relocate --config config.json`
Expected: `relocate: N re-pointed, 0 LOST (of N stale).` If LOST > 0, inspect:
`SELECT path, message FROM run_log WHERE phase='relocate' AND message LIKE 'LOST%';`

- [ ] **Step 3: Confirm with reconcile**

Run: `python -m photo_organizer reconcile --verify-disk --config config.json`
Expected: no LOST FILE rows for the previously-stale paths.

---

## Self-Review

**Spec coverage (§3.5 / D8):**
- "find stale rows" → Task 1 ✓
- "match by sha256, multiple-same-sha best pairing" → Task 2 (`_match_rows_to_paths`) ✓
- "update only path (and mtime), preserve file_id + decisions" → Task 3 (asserts `operations` row survives) ✓
- "log relocated + LOST to run_log phase='relocate'" → Task 3 ✓
- "never move/delete a file on disk" → relocate only `update_file` + `db.log`; no os.rename/unlink anywhere ✓
- "genuinely new files left for `add`" → `unknown` paths with no matching stale sha are simply never touched ✓
- CLI surface → Task 4 ✓

**Placeholder scan:** No TBD/TODO; every code step is complete. The Task 4 test notes the parser-builder name may differ — that is a verification instruction, not a placeholder (the grep target `__main__.py:450` is given).

**Type consistency:** `find_stale_rows` returns sqlite3.Row list; `_match_rows_to_paths(rows, cands)` consumes rows with `["file_id"]`/`["filename"]` and Path cands, returns `(row, Path)` pairs; `relocate` returns the `{"stale","relocated","lost"}` dict used by `cmd_relocate`. Names consistent across tasks.

---

## Execution Handoff

Plan complete. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.
2. **Inline Execution** — execute in this session with checkpoints.

After relocate ships, Plan 2 (folder-merge detection + `folder_overlaps` table + `review --web --folders` UI + plan/execute merge integration) is written the same way.
