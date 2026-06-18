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
from datetime import datetime, timezone
from pathlib import Path

from .classifier import FileClassifier
from .db import Database
from .deduper import _sha256_file
from .scanner import discover_files


def find_stale_rows(db: Database) -> list:
    """Return non-error file rows whose recorded path no longer exists."""
    rows = db.conn.execute(
        "SELECT file_id, path, filename, sha256, status FROM files "
        "WHERE sha256 IS NOT NULL AND status != 'error'"
    ).fetchall()
    return [r for r in rows if not os.path.exists(r["path"])]


def _match_rows_to_paths(rows: list, cands: list) -> list:
    """Pair stale rows to candidate new paths within one sha256 bucket.

    Strategy:
      1. Match each row to a candidate with the IDENTICAL filename first.
      2. Assign any remaining rows to leftover candidates in sorted path order.

    Returns a list of (row, new_path) tuples. Rows with no available candidate
    are omitted (the caller treats them as LOST).
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


def relocate(db: Database, scan_roots: list, prune: bool = False) -> dict:
    """Re-point stale file rows to their moved location by sha256.

    With prune=True, rows still missing after relocation (and not status
    'done') are deleted from the DB along with their dependents, so the DB
    reflects the current library. Returns
    {"stale", "relocated", "lost", "pruned"}.
    """
    stale = find_stale_rows(db)
    if not stale:
        return {"stale": 0, "relocated": 0, "lost": 0, "pruned": 0}

    known = {
        os.path.normcase(r["path"])
        for r in db.conn.execute("SELECT path FROM files").fetchall()
    }
    classifier = FileClassifier()
    discovered: list[Path] = []
    for root in scan_roots:
        discovered.extend(discover_files(Path(root), classifier))
    unknown = [p for p in discovered if os.path.normcase(str(p)) not in known]

    new_by_sha: dict[str, list] = defaultdict(list)
    for p in unknown:
        digest = _sha256_file(p)
        if digest is None:
            db.log("WARN", f"Could not hash discovered file (skipped): {p}",
                   phase="relocate", path=str(p))
            continue
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
