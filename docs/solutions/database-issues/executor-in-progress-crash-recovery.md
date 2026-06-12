---
title: "Executor Crash Recovery: in_progress Intermediate Status for Rename-then-Commit Operations"
date: 2026-06-12
category: docs/solutions/database-issues
module: executor
problem_type: database_issue
component: tooling
severity: high
symptoms:
  - "execute crashes between os.rename() and DB UPDATE — operation stays 'confirmed' forever"
  - "on re-run, source path no longer exists — op silently marked 'skipped' with 'source not found'"
  - "file exists at destination with no DB record of the move"
  - "crash recovery re-run does not detect partially-completed renames"
root_cause: missing_workflow_step
resolution_type: code_fix
related_components:
  - db
tags:
  - crash-recovery
  - in-progress-state
  - database-integrity
  - executor
  - schema-migration
  - two-phase-commit
  - rename
  - cloner
---

# Executor Crash Recovery: in_progress Intermediate Status for Rename-then-Commit Operations

## Problem

`executor.py` moves photos using `os.rename(src, dst)` then updates `operations.status = 'done'` in the DB. If the process crashes between the rename and the DB commit, the operation stays `'confirmed'` — but the file is already at the destination. On the next `execute` re-run, the source path no longer exists and the op is silently marked `'skipped'`, leaving the DB record inconsistent with the filesystem.

This DB is the permanent decision ledger — a missing move record means the organizing intent is unrecoverable.

## Symptoms

- `execute` crashes mid-run (power loss, Ctrl-C, OOM kill)
- After re-run: operation logged as `skipped` with "source not found"
- File exists at the destination path but `files.path` in the DB still points to the old location
- The organizing decision log has a gap — the move is unrecorded

## What Didn't Work

No prior runtime failure triggered this fix — it was identified as a proactive code review finding (#8). The original code had no way to distinguish "rename never started" from "rename completed but crashed before DB update" when `src` was missing. Both cases looked identical on retry and both resolved to `skipped`.

## Solution

Introduce an `in_progress` intermediate status that commits to the DB **before** the rename, creating a detectable crash fingerprint.

### ① Add `in_progress` to the schema (db.py, SCHEMA_VERSION 3)

```python
# SCHEMA_SQL — operations status CHECK
status TEXT NOT NULL DEFAULT 'planned'
       CHECK(status IN ('planned','confirmed','in_progress','done','error','skipped')),
```

Older DBs migrate automatically via `_migrate_operations_check()`, which rebuilds the `operations` table (SQLite cannot `ALTER` a CHECK constraint — see below).

### ② Write `in_progress` + commit before rename (executor.py)

```python
# Mark in_progress and flush to disk BEFORE the destructive operation
db.conn.execute(
    "UPDATE operations SET status='in_progress' WHERE op_id=?",
    (op["op_id"],),
)
db.commit()   # essential: crash must leave 'in_progress' visible in the DB

dst.parent.mkdir(parents=True, exist_ok=True)
os.rename(src, dst)

# Only after rename succeeds: mark done
db.conn.execute(
    "UPDATE operations SET status='done', executed_at=? WHERE op_id=?",
    (now, op["op_id"]),
)
```

### ③ Include `in_progress` in retry query + crash-recovery branch (executor.py)

```python
# Query picks up both fresh and crashed-mid-run operations
where = ["o.status IN ('confirmed', 'in_progress')"]

# When src is missing, check if rename already completed
if not src.exists():
    if op["status"] == "in_progress" and dst.exists():
        # rename completed before crash, DB just wasn't updated
        db.log("WARN",
            "Crash-recovery: rename already completed (src gone, dst present) — marking done.",
            ...)
        db.conn.execute(
            "UPDATE operations SET status='done', executed_at=? WHERE op_id=?",
            (now, op["op_id"]),
        )
        done += 1
    else:
        # src truly missing — log as skipped
        mark_skipped(op["op_id"], "source not found")
        skipped += 1
    continue
```

### SQLite CHECK constraint migration

SQLite does not support `ALTER TABLE ... MODIFY COLUMN`. Adding `'in_progress'` to a CHECK constraint requires rebuilding the table:

```python
def _migrate_operations_check(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operations'"
    ).fetchone()
    if not row or not row[0] or "'in_progress'" in row[0]:
        return  # already current, idempotent
    # Pattern: PRAGMA foreign_keys=OFF → BEGIN →
    # CREATE TABLE operations_new (new schema) →
    # INSERT INTO operations_new SELECT * FROM operations →
    # DROP TABLE operations →
    # ALTER TABLE operations_new RENAME TO operations →
    # recreate indexes → COMMIT
```

The same rebuild pattern is used in `_migrate_filetype_check` — reuse it rather than reinventing.

## Why This Works

`in_progress` is a write-ahead marker: it commits to the DB *before* the side-effectful `os.rename()`. After a crash, the combination `(status = 'in_progress') ∧ (src missing) ∧ (dst present)` is a unique invariant that can only arise in the crash window between a completed rename and a missing DB update. Any other combination maps to a different state tuple, so the recovery branch can act unconditionally without false positives.

The `db.commit()` before `os.rename()` is essential — without it, an in-process crash rolls back the `in_progress` update to `confirmed`, making the crash undetectable on retry.

## Prevention

- Apply the three-stage pattern (`mark in_progress + commit → destructive op → mark done`) to any operation that combines a non-transactional OS call with a DB state update.
- The pattern is only safe when the operation is idempotent on retry (rename to the same dst is a no-op if already there). For non-idempotent operations (API calls, emails), a different deduplication key is needed instead.
- Query crash-recovery events after a re-run:
  ```sql
  SELECT path, message, logged_at
  FROM   run_log
  WHERE  phase = 'execute' AND message LIKE 'Crash-recovery%'
  ORDER  BY logged_at;
  ```
- Complement with `cloner.py`'s `.tmp → os.replace` pattern: `.tmp` ensures no truncated copies survive a clone crash; `in_progress` ensures no DB gaps survive an executor crash.

## Related Issues

- `photo_organizer/executor.py` — move loop, crash-recovery branch
- `photo_organizer/db.py` — `_migrate_operations_check()`, `SCHEMA_VERSION = 3`
- Code review finding #8 (branch `feat/near-dupe-cluster-review`)
