# Relocate `--prune` Implementation Plan — S1 Plan 1.5

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a `--prune` option to the `relocate` command that removes DB rows whose file was intentionally removed from the library (path gone, content not found in scan roots), along with their dependent `duplicates`/`operations`/`run_log` rows — so the DB reflects the current library before folder-merge (Plan 2).

**Architecture:** Extend `relocate()` with `prune=False`. After re-pointing movable files, the remaining LOST rows (path gone, not relocated) are the prune candidates. With `prune=True`, prunable rows (status != 'done') are batch-deleted FK-safe (dependents first, then `files`); their paths are written to `_staging/pruned_paths.txt` for audit; a summary is logged. LOST rows with status 'done' are NEVER pruned (a missing organized file is a real loss, not cleanup) — they're logged ERROR and kept.

**Tech Stack:** Python 3.11, stdlib sqlite3, existing `photo_organizer` modules, pytest.

**Context / why this exists:** A real `relocate` run reported 39,615 LOST. Investigation confirmed the user intentionally moved non-library content (`All In Lightroom\Depository_aged\YOU醬@120`, `小小菁`, plus deleted duplicate trees) OUT of the library. Those rows are now orphans; folder-merge (Plan 2) would treat their phantom folders as real. `--prune` cleans them. The connection runs `PRAGMA foreign_keys=ON` (`db.py:399`); `duplicates(file_id_a,file_id_b,keep_file_id)`, `operations(file_id)`, `run_log(file_id)` all REFERENCE `files(file_id)` with no cascade, so deletion order must be dependents-first.

> **Commit workflow:** Commits go through the separate "SCM GitHub" session. Skip the `git commit` steps when executing; hand staged changes to SCM.

---

## File Structure

- **Modify** `photo_organizer/relocate.py` — add `prune` param to `relocate()` + a `_prune_rows(db, rows)` helper.
- **Modify** `photo_organizer/__main__.py` — add `--prune` arg to the `relocate` subparser; pass to `relocate()`; include pruned count in the printed summary.
- **Modify** `tests/test_relocate.py` — add prune tests.

---

### Task 1: `_prune_rows` — FK-safe batch delete of orphaned rows

**Files:**
- Modify: `photo_organizer/relocate.py`
- Test: `tests/test_relocate.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_relocate.py
def test_prune_rows_deletes_orphan_and_dependents_keeps_done(tmp_path):
    from photo_organizer.relocate import _prune_rows

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        # Orphan source row (status hashed) + a survivor row.
        orphan = _add(db, tmp_path / "gone" / "a.jpg", sha256="aaaa")
        survivor = _add(db, tmp_path / "keep" / "b.jpg", sha256="bbbb")
        # A done row whose path is also gone — must NOT be pruned.
        done = _add(db, tmp_path / "gone" / "c.jpg", sha256="cccc", status="done")
        # Dependents referencing the orphan.
        db.conn.execute(
            "INSERT INTO duplicates (file_id_a, file_id_b, dup_type) VALUES (?,?,'EXACT')",
            (orphan, survivor),
        )
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, status) "
            "VALUES (?, 'MOVE', ?, 'planned')",
            (orphan, str(tmp_path / "gone" / "a.jpg")),
        )
        db.conn.execute(
            "INSERT INTO run_log (level, phase, file_id, path, message, logged_at) "
            "VALUES ('WARN','relocate',?,?,'LOST x','2026-01-01T00:00:00+00:00')",
            (orphan, str(tmp_path / "gone" / "a.jpg")),
        )
        db.commit()

        orphan_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (orphan,)
        ).fetchone()
        done_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (done,)
        ).fetchone()

        pruned, kept_done = _prune_rows(db, [orphan_row, done_row], tmp_path / "report.txt")

        assert pruned == 1 and kept_done == 1
        # orphan + its dependents gone
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (orphan,)).fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM duplicates WHERE file_id_a=? OR file_id_b=?", (orphan, orphan)).fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM operations WHERE file_id=?", (orphan,)).fetchone()[0] == 0
        # survivor + done row untouched
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (survivor,)).fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (done,)).fetchone()[0] == 1
        # audit file written with the pruned path
        assert (tmp_path / "report.txt").read_text(encoding="utf-8").strip().endswith("a.jpg")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relocate.py -k prune_rows_deletes -v`
Expected: FAIL — `ImportError: cannot import name '_prune_rows'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to photo_organizer/relocate.py
def _prune_rows(db: Database, lost_rows: list, report_path: Path) -> tuple[int, int]:
    """Batch-delete orphaned (path-gone) rows + their dependents, FK-safe.

    Rows with status 'done' are NOT pruned (a missing organized file is real
    loss, not cleanup) — they are returned in the kept-done count and logged
    ERROR by the caller. Returns (pruned_count, kept_done_count).

    Prunable paths are written to `report_path` BEFORE deletion (durable audit
    that survives the run_log rows being removed).
    """
    prunable = [r for r in lost_rows if r["status"] != "done"]
    kept_done = [r for r in lost_rows if r["status"] == "done"]
    if not prunable:
        return 0, len(kept_done)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "\n".join(r["path"] for r in prunable) + "\n", encoding="utf-8"
    )

    ids = [r["file_id"] for r in prunable]
    db.conn.execute("CREATE TEMP TABLE IF NOT EXISTS _prune_ids (file_id INTEGER PRIMARY KEY)")
    db.conn.execute("DELETE FROM _prune_ids")
    db.conn.executemany("INSERT INTO _prune_ids(file_id) VALUES (?)", [(i,) for i in ids])
    # Dependents first (FK ON, no cascade), then the files rows.
    db.conn.execute(
        "DELETE FROM duplicates WHERE file_id_a IN (SELECT file_id FROM _prune_ids) "
        "OR file_id_b IN (SELECT file_id FROM _prune_ids) "
        "OR keep_file_id IN (SELECT file_id FROM _prune_ids)"
    )
    db.conn.execute("DELETE FROM operations WHERE file_id IN (SELECT file_id FROM _prune_ids)")
    db.conn.execute("DELETE FROM run_log WHERE file_id IN (SELECT file_id FROM _prune_ids)")
    db.conn.execute("DELETE FROM files WHERE file_id IN (SELECT file_id FROM _prune_ids)")
    db.conn.execute("DROP TABLE _prune_ids")
    db.commit()
    return len(prunable), len(kept_done)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_relocate.py -k prune_rows_deletes -v`
Expected: PASS

- [ ] **Step 5: Commit** (hand to SCM)

```bash
git add photo_organizer/relocate.py tests/test_relocate.py
git commit -m "feat(relocate): _prune_rows — FK-safe delete of orphaned rows, keep 'done'"
```

---

### Task 2: wire `prune` into `relocate()`

**Files:**
- Modify: `photo_organizer/relocate.py`
- Test: `tests/test_relocate.py`

- [ ] **Step 1: Write the failing test**

```python
def test_relocate_prune_removes_lost_default_off(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    scan_root = tmp_path / "lib"
    scan_root.mkdir(parents=True)
    with Database(db_path) as db:
        gone = _add(db, scan_root / "deleted.jpg", sha256="deadbeef")
        db.commit()

        # Default (prune off): row remains.
        summary = relocate(db, [scan_root])
        assert summary["lost"] == 1 and summary.get("pruned", 0) == 0
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (gone,)).fetchone()[0] == 1

        # prune=True: orphan row removed.
        summary2 = relocate(db, [scan_root], prune=True)
        assert summary2["pruned"] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (gone,)).fetchone()[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relocate.py -k relocate_prune_removes -v`
Expected: FAIL — `relocate()` has no `prune` kwarg / no `pruned` key.

- [ ] **Step 3: Write minimal implementation**

Change the `relocate` signature and its tail. Replace the current LOST-logging tail:

```python
def relocate(db: Database, scan_roots: list, prune: bool = False) -> dict:
    """Re-point stale file rows to their moved location by sha256.

    With prune=True, rows still missing after relocation (and not status
    'done') are deleted from the DB along with their dependents, so the DB
    reflects the current library. Returns
    {"stale", "relocated", "lost", "pruned"}.
    """
    # ... unchanged body through building `lost` ...

    if prune:
        report = db.path.parent / "_staging" / "pruned_paths.txt"
        pruned, kept_done = _prune_rows(db, lost, report)
        for r in lost:
            if r["status"] == "done":
                db.log(
                    "ERROR",
                    f"LOST 'done' file (organized file missing, NOT pruned): {r['path']}",
                    phase="relocate", file_id=r["file_id"], path=r["path"],
                )
        db.log(
            "INFO",
            f"Pruned {pruned} orphaned row(s) (files removed from library); "
            f"paths listed in {report}.",
            phase="relocate",
        )
        db.commit()
        return {"stale": len(stale), "relocated": relocated,
                "lost": len(lost), "pruned": pruned}

    for r in lost:
        db.log(
            "WARN",
            f"LOST — path missing, no sha256 match on disk: {r['path']}",
            phase="relocate", file_id=r["file_id"], path=r["path"],
        )
    db.commit()
    return {"stale": len(stale), "relocated": relocated,
            "lost": len(lost), "pruned": 0}
```

> Note: `db.path` is the DB file Path (used elsewhere for the `_staging` default). Confirm the attribute name by grepping `self.path` / `db.path` in `db.py` and `planner.py`; adjust if it's `db._path` exposed via a `path` property.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_relocate.py -v`
Expected: PASS (all relocate tests, including the earlier ones)

- [ ] **Step 5: Commit** (hand to SCM)

```bash
git add photo_organizer/relocate.py tests/test_relocate.py
git commit -m "feat(relocate): add prune option to drop orphaned rows after re-point"
```

---

### Task 3: CLI `--prune` flag

**Files:**
- Modify: `photo_organizer/__main__.py` (cmd_relocate + the `relocate` subparser)

- [ ] **Step 1: Write the failing test**

```python
def test_cli_relocate_prune_flag():
    import photo_organizer.__main__ as m
    parser = m.build_parser()
    args = parser.parse_args(["relocate", "--db", "x.db", "--prune"])
    assert args.func is m.cmd_relocate and args.prune is True
    args2 = parser.parse_args(["relocate", "--db", "x.db"])
    assert args2.prune is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relocate.py -k cli_relocate_prune -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'prune'`

- [ ] **Step 3: Write minimal implementation**

In `cmd_relocate`, pass the flag and report pruned:

```python
    with Database(_db_path(args, cfg)) as db:
        summary = relocate(db, scan_roots, prune=getattr(args, "prune", False))
    console.print(
        f"relocate: {summary['relocated']:,} re-pointed, "
        f"{summary['pruned']:,} pruned, "
        f"{summary['lost'] - summary['pruned']:,} still LOST "
        f"(of {summary['stale']:,} stale)."
    )
    sys.exit(0 if (summary["lost"] - summary["pruned"]) == 0 else 1)
```

In the `relocate` subparser block, add:

```python
    p_reloc.add_argument(
        "--prune", action="store_true",
        help="Delete DB rows whose file was removed from the library (path gone, "
             "not found in scan roots). Keeps 'done' rows. Writes pruned paths to "
             "_staging/pruned_paths.txt.",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_relocate.py -v`
Expected: PASS

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: PASS (166 prior + new prune tests), exit 0.

- [ ] **Step 6: Commit** (hand to SCM)

```bash
git add photo_organizer/__main__.py tests/test_relocate.py
git commit -m "feat(relocate): wire --prune CLI flag"
```

---

### Task 4: Real-library run (manual — with DB backup)

**Files:** none (operates on the real DB)

- [ ] **Step 1: Back up the decision ledger first**

Run: `cp "C:/PhotoTestZone/photos.db" "C:/PhotoTestZone/photos.db.bak-before-prune"`
Expected: a copy exists (the DB is the durable decision record).

- [ ] **Step 2: Run relocate with prune**

Run: `python -m photo_organizer relocate --prune --config config.json`
Expected: `relocate: N re-pointed, ~39615 pruned, 0 still LOST (of ~39615 stale).`
(`re-pointed` will be ~0 this run, since the movable ones were already re-pointed in the prior run.)

- [ ] **Step 3: Verify the DB now balances**

Run: `python -m photo_organizer reconcile --verify-disk --config config.json`
Expected: no LOST FILE rows; balance sheet clean.
Also: `SELECT COUNT(*) FROM files;` should drop by ~39,615 from 282,976 (≈243,361).

---

## Self-Review

**Spec/intent coverage:**
- Orphaned rows removed FK-safe (dependents first) → Task 1 ✓
- 'done' rows never pruned (logged ERROR) → Task 1 + Task 2 ✓
- Audit trail survives row deletion (paths written to file before delete; summary logged with file_id=None) → Task 1/2 ✓
- prune opt-in, default off (current behavior preserved) → Task 2 test asserts both ✓
- CLI flag + accurate summary/exit code → Task 3 ✓
- DB backup before the real destructive run → Task 4 ✓

**Placeholder scan:** none. Task 2's `db.path` note is a verification instruction (grep target given), not a placeholder.

**Type consistency:** `_prune_rows(db, lost_rows, report_path) -> (int, int)`; `relocate(..., prune=False) -> {"stale","relocated","lost","pruned"}`; `cmd_relocate` reads `summary["pruned"]`. Consistent across tasks.

---

## Execution Handoff

Subagent-Driven. After this lands and the real-DB prune is verified, proceed to Plan 2 (folder-merge) on the now-accurate DB.
