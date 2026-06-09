"""
planner.py — Phase 4: Action Plan Generator

Queries the DB and builds a complete list of operations:
  - STAGE_DELETE for resized JPEGs and exact-duplicate non-keepers
  - MOVE+RENAME for all kept files into the target directory structure
  - Surface near-duplicate pairs for human review (never auto-staged)

No files are touched here.  User reviews the summary and must type 'y'
to confirm before Phase 5 can run.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich import box
from rich.table import Table

from .db import Database
from .progress import console, print_phase_header, print_success


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_exif_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    raw = s.strip()
    # Strip a trailing timezone offset such as "+08:00" / "-05:00" / "Z".
    # Apple/QuickTime CreationDate (video) embeds it; we keep the wall-clock time.
    raw = re.sub(r"(?:Z|[+-]\d{2}:?\d{2})$", "", raw).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _parse_mtime(s: str | None) -> datetime | None:
    """
    Parse the stored filesystem mtime (ISO-8601, e.g. '2023-06-15T10:30:00+00:00').
    Used only as a LAST-RESORT date when a file has no EXIF date — mtime can be
    unreliable (copying may reset it), so it is preferred over NoDate/ but always
    logged.  Returns a naive datetime (tz dropped) to match _parse_exif_dt.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=None)


def _effective_date(row: Any) -> tuple[datetime | None, bool]:
    """
    Resolve a file's date: EXIF datetime first, else filesystem mtime.
    Returns (datetime_or_None, used_mtime).
    """
    dt = _parse_exif_dt(row["datetime_original"])
    if dt is not None:
        return dt, False
    return _parse_mtime(row["mtime"]), True


def _sanitize_camera(model: str | None) -> str:
    """Return a filesystem-safe, truncated camera model string."""
    if not model:
        return "Unknown"
    s = re.sub(r"[^\w]", "_", model).strip("_")
    return s[:20] or "Unknown"


def _sanitize_event(folder_name: str | None) -> str:
    """
    Filesystem-safe, truncated 'event & location' string derived from the
    source file's parent folder name.  Returns "" when unusable (empty or a
    drive root like 'E:\\'), so the caller can omit the segment entirely.
    """
    if not folder_name:
        return ""
    # A drive-root parent (e.g. "E:\\") has no real name component.
    if re.fullmatch(r"[A-Za-z]:[\\/]?", folder_name):
        return ""
    s = re.sub(r"[^\w]+", "_", folder_name).strip("_")
    return s[:40]


def _is_unorganised_folder_name(name: str | None) -> bool:
    """
    True when a source folder name carries NO human-meaningful event/location —
    i.e. it is just a date, a plain number/sequence, or a camera dump folder.
    These are almost certainly folders that were never manually organised.

    Examples flagged: "" (root), "2023", "2023-06", "2023-06-15", "20230615",
    "12345", "100CANON", "100MSDCF", "DCIM", "IMG_1234", "DSC01234", "P1010001".
    Examples NOT flagged: "Kyoto", "Japan_Trip", "Wedding 2023", "Grandma 90th".
    """
    n = (name or "").strip()
    if not n:
        return True
    if re.fullmatch(r"[A-Za-z]:[\\/]?", n):            # drive root, e.g. E:\
        return True
    if re.fullmatch(r"\d{4}([-_.]?\d{1,2}){0,2}", n):  # 2023 / 2023-06 / 2023-06-15
        return True
    if re.fullmatch(r"\d{8}([-_]?\d{6})?", n):         # 20230615 / 20230615_143022
        return True
    if re.fullmatch(r"\d+", n):                        # pure number / sequence
        return True
    if re.fullmatch(r"\d{3}[-_]?[A-Za-z0-9]{2,6}", n): # 100CANON / 101NIKON / 100_FUJI
        return True
    low = n.lower()
    if low == "dcim":
        return True
    # single camera-filename-style dump (IMG_1234, DSC01234, P1010001, GOPR0001)
    if re.fullmatch(r"(img|dsc|dscf|p|pic|photo|mvi|gopr)[-_]?\d{3,}", low):
        return True
    return False


# Source folders whose photo dates span more than this many days are treated
# as "not a single outing" (e.g. a phone dump) and fall back to per-day folders.
MAX_EVENT_SPAN_DAYS = 30


def _compute_event_spans(db: Database, stage_ids: set[int]) -> dict[str, dict]:
    """
    Group kept photos by their source parent folder and measure each group's
    date span (earliest → latest EXIF date).

    Returns a map  parent_path_str → {"start": date, "span": int}  ONLY for
    groups that span 2…MAX_EVENT_SPAN_DAYS days (a genuine multi-day event).

    Single-day groups are omitted (they fall back to {YYYY-MM-DD}_{event}).
    Groups spanning > MAX_EVENT_SPAN_DAYS days are omitted and logged as WARN —
    they fall back to per-day folders, since such a folder is almost certainly
    a bulk dump rather than one outing.
    """
    from collections import defaultdict as _dd

    dates_by_parent: dict[str, list] = _dd(list)
    for batch in db.iter_files():
        for row in batch:
            if row["status"] == "error" or row["file_id"] in stage_ids:
                continue
            if row["file_type"] not in ("RAW", "CAMERA_JPEG", "DEV_JPEG", "HEIC"):
                continue  # videos use their own layout; UNKNOWN stays in place
            dt = _parse_exif_dt(row["datetime_original"])
            if dt is None:
                continue
            dates_by_parent[str(Path(row["path"]).parent)].append(dt.date())

    spans: dict[str, dict] = {}
    for parent, dates in dates_by_parent.items():
        dmin, dmax = min(dates), max(dates)
        span = (dmax - dmin).days + 1
        if span <= 1:
            continue  # single day → handled as {YYYY-MM-DD}_{event}
        if span > MAX_EVENT_SPAN_DAYS:
            db.log(
                "WARN",
                f"Source folder spans {span} days "
                f"({dmin.isoformat()}…{dmax.isoformat()}) — exceeds "
                f"{MAX_EVENT_SPAN_DAYS}-day event limit; using per-day folders.",
                phase="review", path=parent,
            )
            continue
        spans[parent] = {"start": dmin, "span": span}
    return spans


def keep_score(row: Any, known_cameras: set[str]) -> tuple:
    """Rank duplicate copies; the highest tuple is the one to KEEP.

    Shared by exact-duplicate resolution (planner) and near-duplicate review
    (reviewer) so a single, consistent policy decides which copy survives —
    whether the relationship is recorded as EXACT or NEAR.

    Order (user-chosen):
      1. highest resolution            (best image wins; ties for exact copies)
      2. largest file size             (least re-compressed)
      3. known-camera / Masters folder (camera_model in known_cameras)
      4. real event-name parent folder (not a date/serial/camera-dump name)
      5. shortest path                 (main location over deep backup)
      6. lowest file_id                (deterministic final tiebreak)
    """
    area = (row["width"] or 0) * (row["height"] or 0)
    model = (row["camera_model"] or "").lower()
    in_known = 1 if (model and model in known_cameras) else 0
    parent = Path(row["path"]).parent.name
    has_event = 1 if (not _is_unorganised_folder_name(parent)
                      and _sanitize_event(parent)) else 0
    return (area, row["size_bytes"] or 0, in_known, has_event,
            -len(row["path"]), -row["file_id"])


# ---------------------------------------------------------------------------
# Union-Find: reconstruct exact-duplicate groups from pair rows
# ---------------------------------------------------------------------------

def _exact_dup_groups(db: Database) -> list[list[int]]:
    rows = db.conn.execute(
        "SELECT file_id_a, file_id_b FROM duplicates WHERE dup_type = 'EXACT'"
    ).fetchall()

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        # iterative path compression
        while parent.get(x, x) != root:
            nxt = parent.get(x, x)
            parent[x] = root
            x = nxt
        return root

    def union(a: int, b: int) -> None:
        pa, pb = find(a), find(b)
        if pa != pb:
            parent[pa] = pb

    for row in rows:
        for fid in (row["file_id_a"], row["file_id_b"]):
            parent.setdefault(fid, fid)
        union(row["file_id_a"], row["file_id_b"])

    groups: dict[int, list[int]] = defaultdict(list)
    for fid in parent:
        groups[find(fid)].append(fid)

    return [g for g in groups.values() if len(g) > 1]


def _pick_keeper(db: Database, group: list[int], known_cameras: set[str]) -> int:
    """Return the file_id to KEEP, per the shared keep_score policy."""
    ph = ",".join("?" * len(group))
    rows = db.conn.execute(
        f"SELECT file_id, path, width, height, size_bytes, camera_model "
        f"FROM files WHERE file_id IN ({ph})", group
    ).fetchall()
    return max(rows, key=lambda r: keep_score(r, known_cameras))["file_id"]


# ---------------------------------------------------------------------------
# Target path builder
# ---------------------------------------------------------------------------

def _build_target_path(
    row: Any,
    target_root: Path,
    known_cameras: set[str],
    counters: dict[tuple, int],
    event_spans: dict[str, dict] | None = None,
) -> Path:
    """
    Compute the reorganised destination path for a file to keep.

    Photos (event folder, ORIGINAL filename kept):
      Single-day event:
        Masters/{YYYY}/{YYYY-MM-DD}_{event}/{original_name}
      Multi-day event (2…MAX_EVENT_SPAN_DAYS days, from event_spans):
        Masters/{YYYY}/{start-date}_{N}d_{event}/{original_name}
        — ALL of the event's files land in this one folder (year = start year).
      Others/  — camera not in known_cameras list
      NoDate/  — no EXIF datetime
      The {event} segment is dropped when the parent folder name is unusable,
      leaving just {YYYY-MM-DD}/ (or {start-date}_{N}d/).  RAW+JPEG pairs keep
      the same stem naturally because the original camera filenames are preserved.
    Videos (own layout — date only, parent folder as event/location):
      Videos/{YYYY}/{YYYY-MM-DD}_{event}_{seq:04d}.EXT
      Videos/NoDate/{event}_{seq:04d}.EXT  — no EXIF datetime
      The {event} segment is dropped when the parent folder name is unusable.

    Filename collisions inside a folder are resolved by the executor
    (it appends _conflict_N when a destination already exists).
    """
    ext = (row["extension"] or "jpg").upper()
    dt, _ = _effective_date(row)  # EXIF date, else filesystem mtime fallback

    # ── Videos: dedicated tree, date-only, event = source parent folder ──────
    if row["file_type"] == "VIDEO":
        event = _sanitize_event(Path(row["path"]).parent.name)
        if dt is None:
            subdir = target_root / "Videos" / "NoDate"
            date_part = ""
        else:
            subdir = target_root / "Videos" / dt.strftime("%Y")
            date_part = dt.strftime("%Y-%m-%d")
        parts = [p for p in (date_part, event) if p]
        stem = "_".join(parts) if parts else "video"

        key = (str(subdir), stem)
        seq = counters.get(key, 0)
        counters[key] = seq + 1
        return subdir / f"{stem}_{seq:04d}.{ext}"

    # ── Photos: per-day event folder, original filename preserved ───────────
    event = _sanitize_event(Path(row["path"]).parent.name)

    if dt is None:
        subdir = target_root / "NoDate"
    else:
        # known_cameras contains lowercase strings; compare case-insensitively
        model_lower = (row["camera_model"] or "").lower()
        in_known = bool(model_lower and model_lower in known_cameras)
        base = target_root / ("Masters" if in_known else "Others")

        # Multi-day event? Use the precomputed start-date + span; the whole
        # event collapses into one folder under its start year.
        span_info = (event_spans or {}).get(str(Path(row["path"]).parent))
        if span_info:
            start = span_info["start"]
            label = f"{start.isoformat()}_{span_info['span']}d"
            folder = f"{label}_{event}" if event else label
            subdir = base / start.strftime("%Y") / folder
        else:
            day = dt.strftime("%Y-%m-%d")
            folder = f"{day}_{event}" if event else day
            subdir = base / dt.strftime("%Y") / folder

    return subdir / row["filename"]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def plan(db: Database, target_root: Path, force: bool = False) -> None:
    """
    Phase 4: build the operations table and prompt the user to confirm.

    Parameters
    ----------
    target_root:
        Root of the reorganised library.  Must be on the same volume as the
        source photos so that Phase 5 can use os.rename() (no data copy).
    """
    print_phase_header("4/5", "Action Plan")

    if not force and db.phase_complete("review"):
        print_success("Phase 4 already complete. Use --force to redo.")
        return

    if force:
        db.conn.execute(
            "DELETE FROM operations WHERE status IN ('planned', 'confirmed')"
        )
        db.commit()

    db.set_phase_status("review", "running")

    # ── 1. Identify files to stage-delete ────────────────────────────────────
    stage_ids: set[int] = set()

    # 1a. All RESIZED_JPEGs
    for batch in db.iter_files(file_type="RESIZED_JPEG"):
        for row in batch:
            stage_ids.add(row["file_id"])

    # 1b. Exact duplicate non-keepers
    # Heavily-mirrored libraries can have 100k+ exact groups, so this must stay
    # O(N): bulk-load metadata once and pick keepers in memory. We do NOT write
    # duplicates.keep_file_id for EXACT — it is never read back (staging is driven
    # by stage_ids), and the old per-group UPDATE scanned the whole duplicates
    # table once per group (O(groups × rows) → hours on big mirrors).
    known_cameras = db.get_known_camera_models()
    with console.status("Resolving exact duplicate groups…"):
        groups = _exact_dup_groups(db)
        dup_keepers: set[int] = set()
        dup_non_keepers: set[int] = set()

        member_ids = [fid for g in groups for fid in g]
        meta: dict[int, Any] = {}
        for i in range(0, len(member_ids), 900):
            chunk = member_ids[i:i + 900]
            ph = ",".join("?" * len(chunk))
            for r in db.conn.execute(
                f"SELECT file_id, path, width, height, size_bytes, camera_model "
                f"FROM files WHERE file_id IN ({ph})", chunk
            ):
                meta[r["file_id"]] = r

        for group in groups:
            keeper = max(group, key=lambda f: keep_score(meta[f], known_cameras))
            dup_keepers.add(keeper)
            for fid in group:
                if fid != keeper:
                    dup_non_keepers.add(fid)
                    stage_ids.add(fid)

    # 1c. Near-duplicate losers the user marked for deletion in `review`.
    # The reviewer only records the decision (duplicates.status='reviewed' +
    # keep_file_id); we derive the loser here and stage it.  Doing it in plan
    # (rather than in the reviewer) means a `plan --force` rebuild cannot wipe
    # the decision, and the loser never also receives a MOVE op.
    near_dup_losers: set[int] = set()
    for r in db.conn.execute(
        "SELECT file_id_a, file_id_b, keep_file_id FROM duplicates "
        "WHERE dup_type = 'NEAR' AND status = 'reviewed' AND keep_file_id IS NOT NULL"
    ).fetchall():
        loser = r["file_id_a"] if r["keep_file_id"] == r["file_id_b"] else r["file_id_b"]
        near_dup_losers.add(loser)
        stage_ids.add(loser)

    # 1d. Safety net — never stage EVERY byte-identical copy of a file.
    # EXACT and NEAR resolution choose keepers independently, so their staging
    # sets can overlap in a way that stages all copies of one sha256 group
    # (e.g. a NEAR loser that is also another group's only EXACT survivor).
    # Guarantee each sha256 group retains its best copy; rescue + log any group
    # found fully staged.  This also protects every NEAR keeper's content,
    # because a staged keeper always has a byte-identical twin kept here.
    sha_groups: dict[str, list[int]] = defaultdict(list)
    for r in db.conn.execute(
        "SELECT file_id, sha256 FROM files WHERE sha256 IS NOT NULL"
    ).fetchall():
        sha_groups[r["sha256"]].append(r["file_id"])
    rescued = 0
    for fids in sha_groups.values():
        if all(f in stage_ids for f in fids):
            keeper = _pick_keeper(db, fids, known_cameras)
            stage_ids.discard(keeper)
            near_dup_losers.discard(keeper)
            dup_non_keepers.discard(keeper)
            rescued += 1
    if rescued:
        db.log(
            "WARN",
            f"{rescued} sha256 group(s) were fully staged by overlapping "
            "EXACT/NEAR decisions; kept the best copy of each to prevent loss.",
            phase="review",
        )
        db.commit()

    # ── 2. Near-duplicate stats (info only — never auto-staged) ──────────────
    near_pairs: int = db.conn.execute(
        "SELECT COUNT(*) FROM duplicates WHERE dup_type = 'NEAR' AND status = 'pending'"
    ).fetchone()[0]

    near_file_ids: set[int] = set()
    for row in db.conn.execute(
        "SELECT file_id_a, file_id_b FROM duplicates "
        "WHERE dup_type = 'NEAR' AND status = 'pending'"
    ).fetchall():
        near_file_ids.update([row["file_id_a"], row["file_id_b"]])

    # ── 3. Build all operations in one pass ───────────────────────────────────
    staging_root = target_root / "_staging" / "to_delete"
    counters: dict[tuple, int] = {}
    now = _now()
    ops: list[dict] = []
    mtime_fallback = 0   # files dated from filesystem mtime (no EXIF date)

    # Pre-measure multi-day events so each source folder maps to one event folder.
    with console.status("Measuring event date spans…"):
        event_spans = _compute_event_spans(db, stage_ids)
        db.commit()

    for batch in db.iter_files():
        for row in batch:
            fid = row["file_id"]
            if row["status"] == "error":
                continue

            if fid in stage_ids:
                # Include file_id in staging name to avoid collisions
                target = staging_root / f"{fid}_{row['filename']}"
                ops.append({
                    "file_id": fid,
                    "op_type": "STAGE_DELETE",
                    "source_path": row["path"],
                    "target_path": str(target),
                    "status": "planned",
                    "planned_at": now,
                })
            else:
                if row["file_type"] == "UNKNOWN":
                    continue  # leave truly-unknown files in place
                _dt, used_mtime = _effective_date(row)
                if used_mtime and _dt is not None:
                    mtime_fallback += 1
                target = _build_target_path(
                    row, target_root, known_cameras, counters, event_spans
                )
                ops.append({
                    "file_id": fid,
                    "op_type": "MOVE",
                    "source_path": row["path"],
                    "target_path": str(target),
                    "status": "planned",
                    "planned_at": now,
                })

    with console.status(f"Writing {len(ops):,} operations to database…"):
        db.conn.executemany(
            """
            INSERT OR IGNORE INTO operations
                (file_id, op_type, source_path, target_path, status, planned_at)
            VALUES (:file_id, :op_type, :source_path, :target_path, :status, :planned_at)
            """,
            ops,
        )
        db.commit()

    # ── 3b. No-event source folders — likely never organised ─────────────────
    # A source folder whose name is empty/a date/a plain number/a camera dump
    # (e.g. "2023-06-15", "20230615", "100CANON", "DCIM") carries no real
    # event/location. Group these by SOURCE folder so the user can revisit them.
    no_event_src: dict[str, int] = {}
    for op in ops:
        if op["op_type"] != "MOVE":
            continue
        parent = Path(op["source_path"]).parent
        if _is_unorganised_folder_name(parent.name) or not _sanitize_event(parent.name):
            no_event_src[str(parent)] = no_event_src.get(str(parent), 0) + 1

    if no_event_src:
        for folder, n in sorted(no_event_src.items()):
            db.log(
                "INFO",
                f"No-event source folder (date/serial name): {folder} ({n} files)",
                phase="review", path=folder,
            )
        db.commit()

    # ── 3c. Files dated from filesystem mtime (no EXIF date) ──────────────────
    if mtime_fallback:
        db.log(
            "WARN",
            f"{mtime_fallback} files had no EXIF date — dated from filesystem "
            "mtime instead (may be inaccurate if files were copied).",
            phase="review",
        )
        db.commit()

    # ── 4. Summary stats ─────────────────────────────────────────────────────
    resized_n: int = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE file_type = 'RESIZED_JPEG'"
    ).fetchone()[0]
    move_n: int = db.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE op_type = 'MOVE' AND status = 'planned'"
    ).fetchone()[0]
    stage_n: int = db.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE op_type = 'STAGE_DELETE' AND status = 'planned'"
    ).fetchone()[0]
    stage_bytes: int = db.conn.execute(
        """
        SELECT COALESCE(SUM(f.size_bytes), 0)
        FROM operations o JOIN files f USING(file_id)
        WHERE o.op_type = 'STAGE_DELETE' AND o.status = 'planned'
        """
    ).fetchone()[0]

    # ── 5. Print preview ──────────────────────────────────────────────────────
    t = Table(title="ACTION PLAN (preview)", box=box.DOUBLE_EDGE, show_header=True)
    t.add_column("Action", style="cyan", min_width=46)
    t.add_column("Files", justify="right")
    t.add_column("Notes", style="dim")

    t.add_row(
        "[red]Stage for deletion — Resized JPEGs[/red]",
        f"{resized_n:,}",
        "path contains 'resized'",
    )
    t.add_row(
        "[red]Stage for deletion — Exact duplicates[/red]",
        f"{len(dup_non_keepers):,}",
        "SHA-256 match, inferior path kept",
    )
    if near_dup_losers:
        t.add_row(
            "[red]Stage for deletion — Near-dupes (reviewed)[/red]",
            f"{len(near_dup_losers):,}",
            "your keep/discard choices from 'review'",
        )
    t.add_row(
        "[green]Move + rename → Masters/ or Others/[/green]",
        f"{move_n:,}",
        "renamed by date/camera",
    )
    if mtime_fallback:
        t.add_row(
            "[dim]  ↳ of which dated by file mtime[/dim]",
            f"{mtime_fallback:,}",
            "no EXIF date — mtime fallback",
        )
    if near_pairs:
        t.add_row(
            "[yellow]Near-duplicates (needs review)[/yellow]",
            f"{len(near_file_ids):,} files",
            f"{near_pairs:,} pairs — NOT auto-staged",
        )

    console.print()
    console.print(t)

    gb = stage_bytes / 1_073_741_824
    console.print(
        f"\n  Space to reclaim: [bold red]~{gb:.1f} GB[/bold red]  "
        f"(moved to _staging/to_delete/, [italic]not[/italic] permanently deleted)\n"
    )
    console.print(f"  Target root: [dim]{target_root}[/dim]\n")

    # ── No-event folders: surface for later manual organising ────────────────
    if no_event_src:
        total_ne = sum(no_event_src.values())
        ne_table = Table(
            title=f"⚠  No-event source folders (date / serial name) — {len(no_event_src):,} folders, {total_ne:,} files",
            box=box.SIMPLE, show_header=True,
        )
        ne_table.add_column("Source folder (likely never organised)", style="yellow")
        ne_table.add_column("Files", justify="right")
        for folder, n in sorted(no_event_src.items(), key=lambda kv: -kv[1])[:30]:
            ne_table.add_row(folder, f"{n:,}")
        if len(no_event_src) > 30:
            ne_table.add_row(f"[dim]… +{len(no_event_src) - 30} more[/dim]", "")
        console.print(ne_table)
        console.print(
            "  Their names are just a date / number / camera dump (e.g. 2023-06-15, 100CANON, DCIM),\n"
            "  so the target folder has no event/location label.\n"
            "  Full list recorded in run_log — review later with:\n"
            "    [cyan]SELECT path, message FROM run_log "
            "WHERE phase='review' AND message LIKE 'No-event%';[/cyan]\n"
        )

    if near_pairs:
        console.print(
            f"  [yellow]⚠[/yellow]  {near_pairs:,} near-duplicate pairs require human review before deletion.\n"
            "     Run: [cyan]python -m photo_organizer review --db <path>[/cyan]\n"
        )

    # ── 6. Confirm ───────────────────────────────────────────────────────────
    try:
        ans = input(
            "Mark all operations as confirmed and proceed to Phase 5? [y/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled — no operations confirmed.[/yellow]")
        db.conn.execute("DELETE FROM operations WHERE status = 'planned'")
        db.commit()
        db.set_phase_status("review", "pending")
        return

    if ans != "y":
        console.print("[yellow]Cancelled — no operations confirmed.[/yellow]")
        db.conn.execute("DELETE FROM operations WHERE status = 'planned'")
        db.commit()
        db.set_phase_status("review", "pending")
        return

    db.conn.execute(
        "UPDATE operations SET status = 'confirmed' WHERE status = 'planned'"
    )
    db.conn.execute(
        "UPDATE files SET status = 'confirmed' "
        "WHERE file_id IN (SELECT file_id FROM operations WHERE status = 'confirmed')"
    )
    db.commit()

    confirmed_n: int = db.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE status = 'confirmed'"
    ).fetchone()[0]

    db.set_phase_status("review", "complete", {
        "target_root": str(target_root),
        "stage_delete": stage_n,
        "move": move_n,
        "near_dupe_pairs": near_pairs,
        "space_bytes": stage_bytes,
    })

    print_success(f"Plan confirmed — {confirmed_n:,} operations ready for Phase 5.")
    console.print(
        "  Run: [cyan]python -m photo_organizer execute --db <path>[/cyan]"
    )
