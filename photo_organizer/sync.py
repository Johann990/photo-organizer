"""
sync.py — fast, explicit DB resync for manual Explorer edits to already-
organized ('done') files (Pillar 2).

`relocate.py`'s find_stale_rows scans the WHOLE files table to discover what
moved — fine for "did files move during scan", far too slow for "I renamed
one folder by hand" (minutes, regardless of how small the edit was). This
module instead trusts the user to state the old/new path explicitly ("tell,
don't detect"): no library-wide scan, no sha256 hashing, just a scoped
path-prefix rewrite plus a ledger entry.

relocate_path() covers BOTH a folder rename and a single-file move — the
underlying operation (rewrite files.path under `old`, record a RENAME op) is
identical; only the CLI verb differs for the user's mental model. The new
RENAME op makes the action reversible for free: executor.undo(op_type=
'RENAME') already knows how to move files.path (current) back to
operations.source_path (original) for any op_type — no new undo code needed.

acknowledge_deleted() is a separate, narrower, ONE-WAY action: it records
that an already-organized file is gone (deleted directly in Explorer,
bypassing the staging workflow) WITHOUT touching files.path/status — there
is nothing to move back. Running undo(op_type='DELETE') on the result is a
safe no-op (the current path doesn't exist, so undo's existing "current file
missing" branch just skips it) — that's the honest answer, not a bug.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _like_escape(s: str) -> str:
    """Escape '\\', '%', '_' for a SQL LIKE pattern (ESCAPE '\\'). Real
    folder names in this library are full of literal underscores
    ("三貂嶺_小蜂") which would otherwise act as a single-char wildcard."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _scope_clause(path: Path) -> tuple[str, list[str]]:
    """WHERE clause + params matching `path` itself OR any descendant under
    it, so one query covers both a single-file move and a folder rename."""
    p = str(path)
    prefix = _like_escape(p) + "\\\\" + "%"  # escaped literal separator + wildcard
    return "(path = ? OR path LIKE ? ESCAPE '\\')", [p, prefix]


def _rewrite(path_str: str, old: Path, new: Path) -> str:
    """`path_str` is `old` itself or `old/<suffix>`; rebase that same
    suffix under `new`."""
    old_s, new_s = str(old), str(new)
    if path_str == old_s:
        return new_s
    return new_s + path_str[len(old_s):]


# ---------------------------------------------------------------------------
# relocate_path — folder rename / single-file move
# ---------------------------------------------------------------------------

def relocate_path(db: Database, old_path, new_path) -> dict:
    """Re-point status='done' files.path entries from `old_path` (gone from
    disk — a folder rename or single-file move the user already performed
    in Explorer) to `new_path` (where they now live), writing a 'RENAME' op
    per affected file so `undo --op-type RENAME` can reverse it.

    Refuses (changes nothing) rather than guess when:
      - old_path still exists on disk (the move/rename hasn't actually
        happened — don't risk silently double-booking it)
      - new_path doesn't exist on disk (nothing to point at)
    A matched row not yet 'done' (still mid-pipeline) is left alone — this
    only targets already-organized files. A matched row whose rewritten
    path isn't actually found on disk is skipped individually (logged), the
    rest still proceed.

    Returns {"matched", "relocated", "skipped_not_done",
    "skipped_no_disk_match", "refused"}.
    """
    old, new = Path(old_path), Path(new_path)
    out = {"matched": 0, "relocated": 0, "skipped_not_done": 0,
           "skipped_no_disk_match": 0, "refused": None}

    if old.exists():
        out["refused"] = "old_path_still_exists"
        db.log("WARN", f"sync refused: old path still on disk: {old}", phase="sync")
        db.commit()
        return out
    if not new.exists():
        out["refused"] = "new_path_missing"
        db.log("WARN", f"sync refused: new path not found on disk: {new}", phase="sync")
        db.commit()
        return out

    clause, params = _scope_clause(old)
    rows = db.conn.execute(
        f"SELECT file_id, path, status FROM files WHERE {clause}", params
    ).fetchall()
    out["matched"] = len(rows)

    now = _now()
    for row in rows:
        if row["status"] != "done":
            out["skipped_not_done"] += 1
            continue
        new_row_path = _rewrite(row["path"], old, new)
        if not Path(new_row_path).exists():
            out["skipped_no_disk_match"] += 1
            db.log(
                "WARN",
                f"sync: expected '{new_row_path}' on disk after rewrite, not found — skipped",
                phase="sync", file_id=row["file_id"], path=row["path"],
            )
            continue
        mt = datetime.fromtimestamp(
            Path(new_row_path).stat().st_mtime, tz=timezone.utc
        ).isoformat()
        db.update_file(row["file_id"], path=new_row_path, mtime=mt)
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, target_path, "
            "status, planned_at, executed_at) VALUES (?, 'RENAME', ?, ?, 'done', ?, ?)",
            (row["file_id"], row["path"], new_row_path, now, now),
        )
        db.log(
            "INFO", f"sync relocated: {row['path']} -> {new_row_path}",
            phase="sync", file_id=row["file_id"], path=new_row_path,
        )
        out["relocated"] += 1

    db.commit()
    return out


# ---------------------------------------------------------------------------
# acknowledge_deleted — one-way: record loss, never restore
# ---------------------------------------------------------------------------

def acknowledge_deleted(db: Database, path, yes: bool = False) -> dict:
    """Record a 'DELETE' op for status='done' rows under `path` that the
    user removed directly in Explorer (bypassing the staging workflow).
    Does NOT touch files.path/status — nothing to point it at, this is a
    one-way acknowledgment so DB-health tools (reconcile/audit) stop
    flagging it as an anomaly, not a reversible move.

    Refuses a row whose recorded path still exists on disk (it isn't
    actually gone — don't acknowledge a loss that didn't happen). Requires
    `yes=True` (the CLI maps this to --yes or an interactive prompt) since
    nothing here can be undone at the byte level.

    Returns {"matched", "acknowledged", "skipped_not_done",
    "skipped_still_on_disk", "refused"}.
    """
    p = Path(path)
    out = {"matched": 0, "acknowledged": 0, "skipped_not_done": 0,
           "skipped_still_on_disk": 0, "refused": None}

    if not yes:
        out["refused"] = "not_confirmed"
        return out

    clause, params = _scope_clause(p)
    rows = db.conn.execute(
        f"SELECT file_id, path, status FROM files WHERE {clause}", params
    ).fetchall()
    out["matched"] = len(rows)

    now = _now()
    for row in rows:
        if row["status"] != "done":
            out["skipped_not_done"] += 1
            continue
        if Path(row["path"]).exists():
            out["skipped_still_on_disk"] += 1
            db.log(
                "WARN", f"sync delete refused: still on disk: {row['path']}",
                phase="sync", file_id=row["file_id"], path=row["path"],
            )
            continue
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, target_path, "
            "status, planned_at, executed_at) VALUES (?, 'DELETE', ?, NULL, 'done', ?, ?)",
            (row["file_id"], row["path"], now, now),
        )
        db.log(
            "WARN", f"sync acknowledged deleted: {row['path']}",
            phase="sync", file_id=row["file_id"], path=row["path"],
        )
        out["acknowledged"] += 1

    db.commit()
    return out
