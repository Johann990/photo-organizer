"""
adder.py — Incremental `add`: drop NEW photos into an already-organized library.

The initial bulk pipeline (scan → dedup → review → plan → execute) organizes a
whole collection in one go. `add` is the maintenance counterpart: a photographer
drops next month's folder onto the drive and runs `add <new_source>`. Only the
NEW files are scanned, deduped against the WHOLE existing library, and placed —
with ZERO churn to what is already organized.

The critical invariant — FROZEN EVENTS, NEVER RESHUFFLE
-------------------------------------------------------
Already-moved files (a 'done' MOVE op) are IMMUTABLE. Their event folder name is
a FACT, not something to recompute. So `add`:
  * never creates an operation for, moves, or renames any 'done' file;
  * never recomputes event spans across the whole library (the existing
    planner._compute_event_groups walks ALL files — we deliberately do NOT use it
    for `add`); event groups for NEW files are computed over the NEW files only;
  * places a new file INTO an existing materialized event folder (using that
    folder's EXISTING name) when it shares the event's source-folder lineage or
    its date falls inside the event's existing date range; otherwise it creates
    a brand-new event folder.

Dedup against the existing library is DB-only here (it reads the sha256 / phash
columns already populated by the scan/dedup machinery — no disk re-read):
  * Exact (SHA-256): a new file whose hash already exists among 'done' library
    files is a redundant copy → recorded as an EXACT duplicate and STAGE_DELETEd,
    NOT placed a second time.
  * Near-dupe (pHash): each new image is queried against an index of the existing
    library's pHashes. A BKTree is the PREFERRED backend (photo_organizer.bktree)
    — it answers "all hashes within Hamming d" with zero false negatives. If that
    module is ever absent, we fall back to a brute-force scan of the existing
    index so this path WORKS regardless. Near-dupes are recorded for human review
    and never auto-deleted; the new file is still placed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich import box
from rich.table import Table

from .db import Database
from .planner import (
    MAX_EVENT_SPAN_DAYS,
    _build_target_path,
    _effective_date,
    _parse_exif_dt,
    _resolve_event_folder,
    _sanitize_event,
    keep_score,
)
from .progress import console, print_phase_header, print_success, print_warning

# pHash perceptual-match threshold (mirror deduper.PHASH_HAMMING_THRESHOLD).
DEFAULT_HAMMING = 8

# File types organized like a photo (event folders under Masters/ or Others/).
_PHOTO_TYPES = ("RAW", "CAMERA_JPEG", "DEV_JPEG", "HEIC")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Existing (materialized) events — derived from 'done' MOVE operations
# ---------------------------------------------------------------------------

@dataclass
class _Event:
    """A materialized event folder already on disk. Its name is FROZEN."""
    dir: Path
    base: str                      # "Masters" or "Others"
    lineage: set[str] = field(default_factory=set)  # original source parent dirs
    date_lo: date | None = None    # event's existing date range (from folder name)
    date_hi: date | None = None


_EVENT_NAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})"        # leading ISO date
    r"(?:\((\d+)d\)|_(\d+)d)?"     # optional span: '(Nd)' (current) or '_Nd' (legacy)
)


def _event_folder_date_range(name: str) -> tuple[date, date] | None:
    """
    Parse the date range encoded in a materialized event-folder name. Handles the
    current naming (ISO date + optional '(Nd)' span + space) AND the legacy
    underscore form, so `add` matches events regardless of when they were filed:
      '2023-06-15 Kyoto'      → single day  → (2023-06-15, 2023-06-15)
      '2023-06-15(3d) Kyoto'  → multi-day   → (2023-06-15, 2023-06-17)
      '2023-06-15_3d_Kyoto'   → legacy      → (2023-06-15, 2023-06-17)
      '2023-06-15'            → single day
    Returns None when the name carries no leading date (e.g. NoDate folders).
    """
    m = _EVENT_NAME_RE.match(name)
    if not m:
        return None
    try:
        start = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None
    span_str = m.group(2) or m.group(3)   # '(Nd)' or '_Nd'
    if span_str:
        span = int(span_str)
        if span >= 1:
            return start, start + timedelta(days=span - 1)
    return start, start


def _existing_events(db: Database) -> list[_Event]:
    """
    Reconstruct the photo events already materialized in the library from the
    'done' MOVE operations. Only folders under Masters/ or Others/ are treated
    as matchable photo events; Videos/ and NoDate/ are excluded.
    """
    rows = db.conn.execute(
        "SELECT o.source_path, f.path AS lib_path "
        "FROM operations o JOIN files f USING(file_id) "
        "WHERE o.op_type = 'MOVE' AND o.status = 'done'"
    ).fetchall()

    events: dict[str, _Event] = {}
    for r in rows:
        event_dir = Path(r["lib_path"]).parent
        parts = event_dir.parts
        if "Masters" in parts:
            base = "Masters"
        elif "Others" in parts:
            base = "Others"
        else:
            continue  # Videos/, NoDate/, or outside the photo tree
        key = str(event_dir)
        ev = events.get(key)
        if ev is None:
            rng = _event_folder_date_range(event_dir.name)
            ev = _Event(
                dir=event_dir, base=base,
                date_lo=rng[0] if rng else None,
                date_hi=rng[1] if rng else None,
            )
            events[key] = ev
        ev.lineage.add(str(Path(r["source_path"]).parent))
    return list(events.values())


def _base_for(row: Any, known_cameras: set[str]) -> str:
    """Masters/ when the camera is known, else Others/ (mirrors the planner)."""
    model = (row["camera_model"] or "").lower()
    return "Masters" if (model and model in known_cameras) else "Others"


def _match_event(row: Any, events: list[_Event], known_cameras: set[str]) -> _Event | None:
    """
    Decide whether a new photo belongs to an EXISTING materialized event.

    1. Same source-folder lineage. A single matching event wins outright. If a
       source folder maps to MULTIPLE events (it spanned > MAX_EVENT_SPAN_DAYS at
       initial plan time and was split into per-day folders), disambiguate by the
       file's date; if none contains it, don't force a guess → new folder.
    2. Otherwise, the file's date falls inside an existing event's date range
       (same Masters/Others base).
    Returns the matched event, or None → caller creates a new event folder.
    """
    src_parent = str(Path(row["path"]).parent)
    dt, _ = _effective_date(row)
    d = dt.date() if dt is not None else None

    lineage_matches = [ev for ev in events if src_parent in ev.lineage]
    if len(lineage_matches) == 1:
        return lineage_matches[0]
    if lineage_matches:
        if d is not None:
            for ev in lineage_matches:
                if ev.date_lo and ev.date_lo <= d <= ev.date_hi:
                    return ev
        return None

    if d is not None:
        base = _base_for(row, known_cameras)
        for ev in events:
            if ev.base == base and ev.date_lo and ev.date_lo <= d <= ev.date_hi:
                return ev
    return None


# ---------------------------------------------------------------------------
# Event spans over the NEW files ONLY (never the whole library)
# ---------------------------------------------------------------------------

def _new_event_groups(
    rows: list[Any], scan_roots: list[Path] | None = None
) -> dict[str, dict]:
    """
    Event / subject groups computed over the NEW rows ONLY.

    Deliberately scoped to the new files so adding cannot change the span/name of
    any already-materialized event. Mirrors planner._compute_event_groups' shape
    (keyed by RESOLVED event folder → {"kind": "event"|"subject", …}) so it plugs
    straight into _build_target_path.
    """
    dates_by_folder: dict[str, list[date]] = defaultdict(list)
    label_by_folder: dict[str, str] = {}
    for row in rows:
        if row["file_type"] not in _PHOTO_TYPES:
            continue
        dt = _parse_exif_dt(row["datetime_original"])
        if dt is None:
            continue
        folder = _resolve_event_folder(Path(row["path"]), scan_roots)
        if folder is None:
            continue
        key = str(folder)
        dates_by_folder[key].append(dt.date())
        label_by_folder.setdefault(key, _sanitize_event(folder.name))

    groups: dict[str, dict] = {}
    for key, dates in dates_by_folder.items():
        dmin, dmax = min(dates), max(dates)
        span = (dmax - dmin).days + 1
        if span > MAX_EVENT_SPAN_DAYS:
            groups[key] = {"kind": "subject", "label": label_by_folder[key]}
        elif span > 1:
            groups[key] = {"kind": "event", "start": dmin, "span": span}
    return groups


# ---------------------------------------------------------------------------
# Near-dupe index over the EXISTING library — BKTree preferred, brute fallback
# ---------------------------------------------------------------------------

def _build_near_tree(existing_by_int: dict[int, list[int]]):
    """
    Build a BKTree from all existing pHash ints. Returns the tree, or None if
    the bktree module is unavailable (caller falls back to brute force).
    Call ONCE before the per-file query loop — not inside it.
    """
    try:
        from .bktree import BKTree
    except ImportError:
        return None
    tree = BKTree()
    tree.add_many(existing_by_int.keys())
    return tree


def _query_near_index(existing_by_int: dict[int, list[int]], key: int,
                      max_distance: int, tree=None) -> list[tuple[int, int]]:
    """
    Return [(stored_int, hamming)] within max_distance of `key` among the
    existing library's pHash ints.

    Pass a pre-built BKTree via `tree` (see _build_near_tree) to avoid
    rebuilding the index on every call. Falls back to brute-force when tree
    is None (BKTree unavailable).
    """
    if tree is None:
        return [
            (k, bin(k ^ key).count("1"))
            for k in existing_by_int
            if bin(k ^ key).count("1") <= max_distance
        ]
    return tree.query(key, max_distance)


# ---------------------------------------------------------------------------
# Core: plan operations for the NEW files only
# ---------------------------------------------------------------------------

def plan_additions(
    db: Database,
    target_root: Path,
    *,
    hamming_threshold: int = DEFAULT_HAMMING,
    scan_roots: list[Path] | None = None,
) -> dict[str, int]:
    """
    Build 'planned' operations for the NEW (un-organized) files only and return a
    summary. DB-only: reads the sha256 / phash columns already populated by the
    scan/dedup machinery; touches no disk and no 'done' file.

    A NEW file is one that is neither in a terminal 'done'/'error' state nor the
    subject of a 'done' operation. Re-running is idempotent: existing
    planned/confirmed ops for the new files are cleared and rebuilt; 'done' ops
    are never touched.
    """
    # ── Identify NEW files (exclude anything already organized) ───────────────
    done_op_ids = {
        r["file_id"]
        for r in db.conn.execute(
            "SELECT DISTINCT file_id FROM operations WHERE status = 'done'"
        ).fetchall()
    }
    new_rows: list[Any] = []
    for batch in db.iter_files():
        for row in batch:
            if row["status"] in ("done", "error"):
                continue
            if row["file_id"] in done_op_ids:
                continue
            new_rows.append(row)

    summary: dict[str, Any] = {
        "new": len(new_rows), "moved": 0, "into_existing": 0,
        "new_events": 0, "exact_dup": 0, "near_dup": 0,
        "_new_ids": [],  # kept internal; add() uses this for scoped ops
    }
    if not new_rows:
        return summary

    new_ids = [row["file_id"] for row in new_rows]
    summary["_new_ids"] = new_ids

    # Idempotency: clear only this file-set's pending ops; never 'done' ops.
    ph = ",".join("?" * len(new_ids))
    db.conn.execute(
        f"DELETE FROM operations WHERE status IN ('planned','confirmed') "
        f"AND file_id IN ({ph})",
        new_ids,
    )
    db.commit()

    known_cameras = db.get_known_camera_models()

    # ── Exact dedup against the existing library (SHA-256) ────────────────────
    # Existing 'done' copies grouped by hash → keepers a new copy must defer to.
    existing_sha: dict[str, list[int]] = defaultdict(list)
    for r in db.conn.execute(
        "SELECT file_id, sha256 FROM files WHERE status = 'done' AND sha256 IS NOT NULL"
    ).fetchall():
        existing_sha[r["sha256"]].append(r["file_id"])

    stage_ids: set[int] = set()
    # Group the new rows by hash so new-vs-new exact copies keep just one.
    new_by_sha: dict[str, list[Any]] = defaultdict(list)
    for row in new_rows:
        if row["sha256"]:
            new_by_sha[row["sha256"]].append(row)

    for sha, rows in new_by_sha.items():
        if sha in existing_sha:
            # Every new copy is redundant — the library already holds this byte
            # sequence. Record the duplicate and stage the new copy.
            keeper = existing_sha[sha][0]
            for row in rows:
                stage_ids.add(row["file_id"])
                db.insert_duplicate(row["file_id"], keeper, "EXACT", 0)
                summary["exact_dup"] += 1
        elif len(rows) > 1:
            # Several brand-new copies of the same bytes — keep the best, stage
            # the rest (consistent with the planner's keep_score policy).
            best = max(rows, key=lambda r: keep_score(r, known_cameras))
            for row in rows:
                if row["file_id"] != best["file_id"]:
                    stage_ids.add(row["file_id"])
                    db.insert_duplicate(row["file_id"], best["file_id"], "EXACT", 0)
                    summary["exact_dup"] += 1
    db.commit()

    # ── Near-dupe against the existing library (pHash) ────────────────────────
    # Build the existing index from 'done' images that carry a pHash; query each
    # new image against it. Near-dupes are recorded for review, never auto-staged.
    existing_by_int: dict[int, list[int]] = defaultdict(list)
    _old_format_count = 0
    for r in db.conn.execute(
        "SELECT file_id, phash FROM files WHERE status = 'done' AND phash IS NOT NULL"
    ).fetchall():
        try:
            existing_by_int[int(r["phash"], 16)].append(r["file_id"])
        except (TypeError, ValueError):
            _old_format_count += 1

    if _old_format_count:
        print_warning(
            f"{_old_format_count:,} existing pHash value(s) are in the old signed-INTEGER "
            "format and were skipped — near-dup detection against those files is disabled. "
            "Fix: python scripts/migrate_phash_to_hex.py <db_path>"
        )

    if existing_by_int:
        # Build the BKTree ONCE before the per-file loop (O(N log N) total, not per file).
        near_tree = _build_near_tree(existing_by_int)
        for row in new_rows:
            if row["file_id"] in stage_ids or not row["phash"]:
                continue
            try:
                key = int(row["phash"], 16)
            except (TypeError, ValueError):
                continue
            for b_int, hamming in _query_near_index(
                existing_by_int, key, hamming_threshold, near_tree
            ):
                for existing_fid in existing_by_int[b_int]:
                    db.insert_duplicate(row["file_id"], existing_fid, "NEAR", hamming)
                    summary["near_dup"] += 1
        db.commit()

    # ── Placement: into a matched existing event, else a NEW event folder ─────
    events = _existing_events(db)
    new_spans = _new_event_groups(new_rows, scan_roots)
    staging_root = target_root / "_staging" / "to_delete"
    counters: dict[tuple, int] = {}
    now = _now()
    ops: list[dict] = []

    for row in new_rows:
        fid = row["file_id"]
        if fid in stage_ids:
            ops.append({
                "file_id": fid, "op_type": "STAGE_DELETE",
                "source_path": row["path"],
                "target_path": str(staging_root / f"{fid}_{row['filename']}"),
                "status": "planned", "planned_at": now,
            })
            continue

        ft = row["file_type"]
        if ft == "UNKNOWN":
            continue  # leave truly-unknown files in place (as the planner does)

        if ft in _PHOTO_TYPES:
            matched = _match_event(row, events, known_cameras)
            if matched is not None:
                # FROZEN: place INTO the existing folder, keep its exact name.
                target = matched.dir / row["filename"]
                summary["into_existing"] += 1
            else:
                target = _build_target_path(
                    row, target_root, known_cameras, counters, new_spans,
                    scan_roots,
                )
                summary["new_events"] += 1
        else:  # VIDEO → its own dated tree (sequence collisions handled at execute)
            target = _build_target_path(
                row, target_root, known_cameras, counters, new_spans,
                scan_roots,
            )
            summary["new_events"] += 1

        ops.append({
            "file_id": fid, "op_type": "MOVE",
            "source_path": row["path"], "target_path": str(target),
            "status": "planned", "planned_at": now,
        })
        summary["moved"] += 1

    if ops:
        db.conn.executemany(
            "INSERT OR IGNORE INTO operations "
            "(file_id, op_type, source_path, target_path, status, planned_at) "
            "VALUES (:file_id, :op_type, :source_path, :target_path, :status, :planned_at)",
            ops,
        )
        db.commit()

    return summary


# ---------------------------------------------------------------------------
# CLI orchestrator
# ---------------------------------------------------------------------------

def _confirm(summary: dict[str, int], target_root: Path, assume_yes: bool) -> bool:
    """Preview the additions and ask the user to confirm (unless assume_yes)."""
    t = Table(title="ADD — incremental placement (preview)", box=box.DOUBLE_EDGE,
              show_header=True)
    t.add_column("Action", style="cyan", min_width=40)
    t.add_column("Files", justify="right")
    t.add_column("Notes", style="dim")
    t.add_row("[green]Place new files[/green]", f"{summary['moved']:,}",
              "MOVE into the library")
    t.add_row("[dim]  ↳ into existing event folders[/dim]",
              f"{summary['into_existing']:,}", "frozen names, no reshuffle")
    t.add_row("[dim]  ↳ into new event folders[/dim]",
              f"{summary['new_events']:,}", "computed over new files only")
    if summary["exact_dup"]:
        t.add_row("[red]Stage exact duplicates[/red]", f"{summary['exact_dup']:,}",
                  "already in library — not re-placed")
    if summary["near_dup"]:
        t.add_row("[yellow]Near-dupes vs library[/yellow]", f"{summary['near_dup']:,}",
                  "recorded for review — NOT staged")
    console.print()
    console.print(t)
    console.print(f"\n  Target root: [dim]{target_root}[/dim]\n")

    if assume_yes:
        return True
    try:
        return input("Confirm and place these new files? [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def add(
    db: Database,
    sources: list[Path],
    target_root: Path,
    *,
    workers: int = 4,
    hamming_threshold: int = DEFAULT_HAMMING,
    use_secondary_signals: bool = False,
    assume_yes: bool = False,
    do_execute: bool = True,
) -> dict[str, int]:
    """
    Incrementally add NEW sources to an already-organized library.

    Reuses the existing phase functions:
      1. scan each new source (the scanner already skips known paths);
      2. hash + perceptual-hash the new files (dedup_exact / dedup_near);
      3. plan_additions — dedup new vs existing + place new files only;
      4. confirm, then execute (place on disk) unless do_execute is False.
    """
    print_phase_header("add", "Incremental Add")

    from .scanner import scan
    for src in sources:
        console.rule(f"[dim]Scanning new source: {src}[/dim]")
        scan(root=src, db=db, workers=workers,
             use_secondary_signals=use_secondary_signals, force=True)

    # Hash + perceptual-hash the freshly scanned files (force past the
    # already-complete phase guards; both only touch files missing a hash).
    from .deduper import dedup_exact, dedup_near
    dedup_exact(db, workers=workers, force=True)
    dedup_near(db, workers=workers, hamming_threshold=hamming_threshold, force=True)

    summary = plan_additions(db, target_root, hamming_threshold=hamming_threshold,
                             scan_roots=sources)

    if summary["new"] == 0:
        print_success("No new files to add — the library is already up to date.")
        return summary

    batch_ids: list[int] = summary.pop("_new_ids", [])
    ph = ",".join("?" * len(batch_ids)) if batch_ids else "0"

    if not _confirm(summary, target_root, assume_yes):
        console.print("[yellow]Cancelled — no operations confirmed.[/yellow]")
        if batch_ids:
            db.conn.execute(
                f"DELETE FROM operations WHERE status = 'planned' AND file_id IN ({ph})",
                batch_ids,
            )
        db.commit()
        return summary

    if batch_ids:
        db.conn.execute(
            f"UPDATE operations SET status = 'confirmed' "
            f"WHERE status = 'planned' AND file_id IN ({ph})",
            batch_ids,
        )
        db.conn.execute(
            f"UPDATE files SET status = 'confirmed' "
            f"WHERE file_id IN ({ph})",
            batch_ids,
        )
    db.commit()
    print_success(
        f"{summary['moved']:,} new files planned "
        f"({summary['into_existing']:,} into existing events, "
        f"{summary['new_events']:,} new), {summary['exact_dup']:,} exact dups staged."
    )

    if do_execute:
        from .executor import execute
        execute(db)
    else:
        console.print(
            "  Run: [cyan]python -m photo_organizer execute --db <path>[/cyan]"
        )
    if summary["near_dup"]:
        print_warning(
            f"{summary['near_dup']:,} near-duplicate(s) against the existing library "
            "were recorded for review (not auto-deleted)."
        )
    return summary
