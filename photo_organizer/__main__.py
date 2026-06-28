"""
__main__.py — CLI entry point for photo-organizer.

Usage (with config.json — recommended):
    python -m photo_organizer validate --config config.json
    python -m photo_organizer scan     --config config.json
    python -m photo_organizer report   --config config.json
    python -m photo_organizer dedup    --config config.json
    python -m photo_organizer review   --config config.json
    python -m photo_organizer plan     --config config.json
    python -m photo_organizer execute  --config config.json

Usage (manual flags — alternative):
    python -m photo_organizer scan     E:\\Photos --db C:\\photos.db
    python -m photo_organizer plan     --db C:\\photos.db --target E:\\Organised
    python -m photo_organizer execute  --db C:\\photos.db
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import Database, SchemaVersionError, default_db_path
from .progress import console, print_error, print_success, print_warning


def _fmt_duration(seconds: float) -> str:
    """Human-friendly elapsed time: '4s', '2m 09s', '1h 03m 12s'."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# Commands that never touch a database — skip timing persistence for these.
_NO_DB_COMMANDS = {"validate", "timings"}


def _persist_timing(args, command, started_iso, finished_iso, duration, status):
    """Best-effort: record the command's wall-clock time in the DB.

    Never raises — a timing-log failure must not fail the command itself.
    """
    if command in _NO_DB_COMMANDS:
        return
    try:
        cfg = _load_cfg(args) if getattr(args, "config", None) else None
        db_path = _resolve_db_path(
            getattr(args, "db", None),
            cfg.db if cfg else None,
            _target_path(args, cfg),
        )
        if db_path is None or not Path(db_path).exists():
            return  # no DB to write to (e.g. command failed before creating one)
        with Database(db_path) as db:
            db.record_command_run(
                command, started_iso, finished_iso, duration, status
            )
    except Exception:
        pass  # timing is advisory; swallow everything


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_cfg(args):
    """Return PhotoConfig if --config was given, else None."""
    cfg_path = getattr(args, "config", None)
    if not cfg_path:
        return None
    from .config import load_config
    try:
        return load_config(cfg_path)
    except (FileNotFoundError, ValueError) as e:
        print_error(str(e))
        sys.exit(1)


def _resolve_db_path(db_arg, cfg_db, target) -> Path | None:
    """
    Pure resolution of where the database lives (no I/O, no exit) so it can be
    unit-tested. Priority:
      1. explicit --db                          (always wins)
      2. db set in the config file
      3. default home beside the library        {target}/.photo_organizer/library.db
      4. None                                   (caller errors out)

    The default in (3) makes the DB a durable, library-adjacent asset: when a
    --target exists the organising decisions live with the photos they describe.
    """
    if db_arg:
        return Path(db_arg)
    if cfg_db:
        return Path(cfg_db)
    if target is not None:
        return default_db_path(target)
    return None


def _db_path(args, cfg) -> Path:
    """Resolve DB path: CLI flag wins, then config, then library default."""
    resolved = _resolve_db_path(
        getattr(args, "db", None),
        cfg.db if cfg else None,
        _target_path(args, cfg),
    )
    if resolved is None:
        print_error(
            "No database path provided. Use --db, --config, or pass --target "
            "(the DB then defaults to {target}/.photo_organizer/library.db)."
        )
        sys.exit(1)
    return resolved


def _target_path(args, cfg) -> Path | None:
    """Resolve target path: CLI flag wins, then config."""
    if getattr(args, "target", None):
        return Path(args.target)
    if cfg and cfg.target:
        return cfg.target
    return None


def _workers(args, cfg) -> int:
    w = getattr(args, "workers", None)
    if w:
        return w
    if cfg:
        return cfg.workers
    return 4


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_validate(args):
    cfg = _load_cfg(args)
    if cfg is None:
        print_error("--config is required for the validate command.")
        sys.exit(1)
    from .validator import validate
    ok = validate(cfg)
    sys.exit(0 if ok else 1)


def cmd_scan(args):
    cfg = _load_cfg(args)
    db_path = _db_path(args, cfg)
    workers = _workers(args, cfg)
    secondary = getattr(args, "secondary", False) or (cfg.use_secondary_signals if cfg else False)
    force = getattr(args, "force", False)

    # Determine source directories
    # Priority: positional ROOT_PATH arg (single) → config input_dirs (multiple)
    root_arg = getattr(args, "root", None)
    if root_arg:
        input_dirs = [Path(root_arg)]
    elif cfg and cfg.input_dirs:
        input_dirs = cfg.input_dirs
    else:
        print_error(
            "No source directory provided.\n"
            "  Use: scan ROOT_PATH --db DB  or  scan --config config.json"
        )
        sys.exit(1)

    from .scanner import scan
    with Database(db_path) as db:
        # Register known cameras from config into DB
        if cfg and cfg.known_cameras:
            for cam in cfg.known_cameras:
                db.add_known_camera(cam.get("make"), cam["model"])

        if len(input_dirs) == 1:
            scan(root=input_dirs[0], db=db, workers=workers,
                 use_secondary_signals=secondary, force=force)
        else:
            # Multi-directory scan: run sequentially (one DB, multiple roots)
            console.print(f"  [bold]{len(input_dirs)}[/bold] input directories configured.")
            for i, d in enumerate(input_dirs, 1):
                console.rule(f"[dim]Directory {i}/{len(input_dirs)}: {d}[/dim]")
                scan(root=d, db=db, workers=workers,
                     use_secondary_signals=secondary, force=force)


def cmd_report(args):
    cfg = _load_cfg(args)
    from .reporter import report
    with Database(_db_path(args, cfg)) as db:
        report(db)


def cmd_dedup(args):
    cfg = _load_cfg(args)
    db_path = _db_path(args, cfg)
    workers = _workers(args, cfg)
    hamming = getattr(args, "hamming", None) or (cfg.hamming_threshold if cfg else 8)
    force = getattr(args, "force", False)

    from .deduper import dedup_exact, dedup_near
    with Database(db_path) as db:
        if not getattr(args, "near_only", False):
            dedup_exact(db, workers=workers, force=force)
        if not getattr(args, "exact_only", False):
            dedup_near(db, workers=workers, hamming_threshold=hamming, force=force)


def cmd_plan(args):
    cfg = _load_cfg(args)
    db_path = _db_path(args, cfg)

    # Date forensics audit only — DB-only, no plan/prompt, no target needed.
    if getattr(args, "dates_only", False):
        from .planner import audit_dates
        with Database(db_path) as db:
            summary = audit_dates(db)
        console.print(
            f"  Audited [bold]{summary['total']:,}[/bold] files — "
            f"[green]{summary['high']:,} HIGH[/green], "
            f"[yellow]{summary['medium']:,} MEDIUM[/yellow], "
            f"[red]{summary['low']:,} LOW[/red] (suspicious).\n"
            "  Review LOW dates: [cyan]SELECT path, message FROM run_log "
            "WHERE phase='review' AND message LIKE 'Suspicious-date%';[/cyan]"
        )
        return

    target = _target_path(args, cfg)

    if target is None:
        print_error(
            "No target directory provided.\n"
            "  Use: plan --target PATH --db DB  or  plan --config config.json\n"
            "  (and set \"target\" in config.json)"
        )
        sys.exit(1)

    from .planner import plan
    scan_roots = cfg.input_dirs if (cfg and cfg.input_dirs) else None
    with Database(db_path) as db:
        plan(db, target_root=target, force=getattr(args, "force", False),
             assume_yes=getattr(args, "yes", False), scan_roots=scan_roots)


def cmd_execute(args):
    cfg = _load_cfg(args)
    from .executor import execute
    with Database(_db_path(args, cfg)) as db:
        execute(
            db,
            force=getattr(args, "force", False),
            year=getattr(args, "year", None),
            camera=getattr(args, "camera", None),
            software=getattr(args, "software", None),
            file_type=getattr(args, "type", None),
            skip_preflight=getattr(args, "skip_preflight", False),
        )


def cmd_add(args):
    cfg = _load_cfg(args)
    db_path = _db_path(args, cfg)
    target = _target_path(args, cfg)
    if target is None:
        print_error(
            "No target directory provided.\n"
            "  Use: add SOURCE --target PATH --db DB  or  add SOURCE --config config.json"
        )
        sys.exit(1)

    # Sources: positional path(s) win; else config input_dirs.
    src_args = getattr(args, "sources", None)
    if src_args:
        sources = [Path(s) for s in src_args]
    elif cfg and cfg.input_dirs:
        sources = cfg.input_dirs
    else:
        print_error(
            "No source directory provided.\n"
            "  Use: add SOURCE_PATH --target PATH --db DB"
        )
        sys.exit(1)

    workers = _workers(args, cfg)
    secondary = getattr(args, "secondary", False) or (cfg.use_secondary_signals if cfg else False)
    hamming = getattr(args, "hamming", None) or (cfg.hamming_threshold if cfg else 8)

    from .adder import add
    with Database(db_path) as db:
        if cfg and cfg.known_cameras:
            for cam in cfg.known_cameras:
                db.add_known_camera(cam.get("make"), cam["model"])
        add(
            db, sources, target,
            workers=workers,
            hamming_threshold=hamming,
            use_secondary_signals=secondary,
            assume_yes=getattr(args, "yes", False),
            do_execute=not getattr(args, "no_execute", False),
        )


def cmd_reconcile(args):
    cfg = _load_cfg(args)
    from .reconcile import reconcile
    with Database(_db_path(args, cfg)) as db:
        ok = reconcile(db, verify_disk=getattr(args, "verify_disk", False))
    sys.exit(0 if ok else 1)


def cmd_audit(args):
    cfg = _load_cfg(args)
    from .auditor import audit
    with Database(_db_path(args, cfg)) as db:
        report = audit(db)
    sys.exit(0 if report.exit_ok else 1)


def cmd_sync(args):
    cfg = _load_cfg(args)
    from .sync import acknowledge_deleted, relocate_path
    action = args.sync_action
    with Database(_db_path(args, cfg)) as db:
        if action in ("rename", "move"):
            result = relocate_path(db, args.old, args.new)
            if result["refused"] == "old_path_still_exists":
                print_error(
                    f"Refused — old path still exists on disk: {args.old}\n"
                    "  (the rename/move hasn't actually happened — do it in "
                    "Explorer first, then re-run sync)"
                )
                sys.exit(1)
            if result["refused"] == "new_path_missing":
                print_error(f"Refused — new path not found on disk: {args.new}")
                sys.exit(1)
            if result["matched"] == 0:
                print_warning(f"No DB rows found under: {args.old}")
                sys.exit(1)
            console.print(
                f"  sync {action}: {result['relocated']:,} file(s) re-pointed "
                f"(matched {result['matched']:,}"
                + (f", {result['skipped_not_done']} not yet organized — left alone"
                   if result["skipped_not_done"] else "")
                + (f", {result['skipped_no_disk_match']} no disk match — left alone"
                   if result["skipped_no_disk_match"] else "")
                + ")."
            )
            console.print(
                "  [dim]To revert: python -m photo_organizer undo --op-type RENAME[/dim]"
            )
            sys.exit(0 if result["relocated"] > 0 else 1)
        else:  # delete
            yes = getattr(args, "yes", False)
            if not yes:
                if not sys.stdin.isatty():
                    print_warning(
                        "Non-interactive shell — pass --yes to confirm. Nothing changed."
                    )
                    sys.exit(1)
                try:
                    ans = input(
                        f"Acknowledge '{args.path}' as permanently deleted "
                        "(cannot be undone)? [y/N] "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = ""
                yes = ans == "y"
                if not yes:
                    console.print("[yellow]Cancelled — nothing changed.[/yellow]")
                    sys.exit(1)
            result = acknowledge_deleted(db, args.path, yes=yes)
            if result["matched"] == 0:
                print_warning(f"No DB rows found under: {args.path}")
                sys.exit(1)
            if result["skipped_still_on_disk"]:
                print_warning(
                    f"{result['skipped_still_on_disk']:,} file(s) still exist on disk — "
                    "not acknowledged (did you mean `sync move`/`sync rename`?)."
                )
            print_success(
                f"Acknowledged {result['acknowledged']:,} file(s) as deleted. "
                "This cannot be undone — the bytes are gone."
            )
            sys.exit(0 if result["acknowledged"] > 0 else 1)


def cmd_relocate(args):
    cfg = _load_cfg(args)
    from .relocate import relocate
    if getattr(args, "prune_only", False):
        from .relocate import prune_missing
        with Database(_db_path(args, cfg)) as db:
            summary = prune_missing(db, show_progress=True)
        console.print(
            f"prune-only: {summary['pruned']:,} pruned, "
            f"{summary['kept_done']:,} 'done' kept (of {summary['stale']:,} stale)."
        )
        sys.exit(0)
    scan_roots = list(cfg.input_dirs) if cfg else [getattr(args, "root", None)]
    scan_roots = [r for r in scan_roots if r]
    if not scan_roots:
        print_error("relocate needs scan roots: --config with input_dirs, or a ROOT arg.")
        sys.exit(1)
    with Database(_db_path(args, cfg)) as db:
        summary = relocate(db, scan_roots, prune=getattr(args, "prune", False),
                           show_progress=True)
    console.print(
        f"relocate: {summary['relocated']:,} re-pointed, "
        f"{summary['pruned']:,} pruned, "
        f"{summary['lost'] - summary['pruned']:,} still LOST "
        f"(of {summary['stale']:,} stale)."
    )
    sys.exit(0 if (summary["lost"] - summary["pruned"]) == 0 else 1)


def cmd_clone(args):
    cfg = _load_cfg(args)
    dest = getattr(args, "dest", None)
    if not dest:
        print_error(
            "No destination provided.\n"
            "  Use: clone DEST_ROOT --target LIB_ROOT --db DB"
        )
        sys.exit(1)
    from .cloner import clone
    with Database(_db_path(args, cfg)) as db:
        stats = clone(
            db,
            dest,
            target_root=_target_path(args, cfg),
            verify_all=getattr(args, "verify_all", False),
            prune=getattr(args, "prune", False),
        )
    # Non-zero exit when the backup is not provably complete + intact.
    sys.exit(0 if (stats.errors == 0 and stats.missing_source == 0) else 1)


def cmd_undo(args):
    cfg = _load_cfg(args)
    from .executor import undo
    with Database(_db_path(args, cfg)) as db:
        undo(
            db,
            force=getattr(args, "force", False),
            year=getattr(args, "year", None),
            camera=getattr(args, "camera", None),
            software=getattr(args, "software", None),
            file_type=getattr(args, "type", None),
            op_type=getattr(args, "op_type", None),
        )


def cmd_review(args):
    cfg = _load_cfg(args)
    with Database(_db_path(args, cfg)) as db:
        if getattr(args, "auto", False):
            from .reviewer import auto_resolve_near_dupes
            auto_resolve_near_dupes(db, commit=getattr(args, "commit", False))
        elif getattr(args, "folders", False):
            from .folderreview import serve as folder_serve
            folder_serve(db, port=getattr(args, "port", 0))
        elif getattr(args, "organize", False):
            from .folderorganize import serve as organize_serve
            scan_roots = cfg.input_dirs if (cfg and cfg.input_dirs) else None
            target_root = cfg.target if cfg else None
            organize_serve(db, port=getattr(args, "port", 0), scan_roots=scan_roots,
                           target_root=target_root, year=getattr(args, "year", None))
        elif getattr(args, "web", False):
            from .webreview import serve
            serve(db, review_all=getattr(args, "all", False),
                  port=getattr(args, "port", 0))
        else:
            from .reviewer import review_near_dupes
            review_near_dupes(db, review_all=getattr(args, "all", False))


def cmd_unknown_cameras(args):
    cfg = _load_cfg(args)
    from .reporter import report_unknown_cameras
    with Database(_db_path(args, cfg)) as db:
        report_unknown_cameras(db)


def cmd_reclassify(args):
    cfg = _load_cfg(args)
    secondary = getattr(args, "secondary", False) or (cfg.use_secondary_signals if cfg else False)
    from .reclassifier import reclassify
    with Database(_db_path(args, cfg)) as db:
        reclassify(db, use_secondary_signals=secondary)


def cmd_timings(args):
    cfg = _load_cfg(args)
    from rich.table import Table

    with Database(_db_path(args, cfg)) as db:
        rows = db.command_timings()

    if not rows:
        console.print(
            "  [dim]No timing history yet — run scan / dedup / plan / execute "
            "and their durations will be recorded here.[/dim]"
        )
        return

    table = Table(title="Command timing history", title_style="bold cyan")
    table.add_column("Command", style="bold")
    table.add_column("Runs", justify="right")
    table.add_column("Last", justify="right")
    table.add_column("Avg", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("Last run (UTC)", style="dim")

    for r in rows:
        last_at = (r["last_at"] or "")[:19].replace("T", " ")
        cmd = r["command"]
        if r["last_status"] and r["last_status"] != "ok":
            cmd += f" [{r['last_status']}]"
        table.add_row(
            cmd,
            f"{r['runs']:,}",
            _fmt_duration(r["last_s"]),
            _fmt_duration(r["avg_s"]),
            _fmt_duration(r["min_s"]),
            _fmt_duration(r["max_s"]),
            last_at,
        )
    console.print(table)


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


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo_organizer",
        description="Automated photo library organizer for large external-drive collections.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Use --config config.json for multi-directory libraries (recommended).\n"
            "Run 'validate' first to check your config before touching real files."
        ),
    )

    # Shared options available on every subcommand
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--config", metavar="PATH",
        help="Path to config.json (provides defaults for --db, --target, --workers, etc.)",
    )
    shared.add_argument(
        "--db", metavar="PATH",
        help="Path to SQLite database (overrides config; created if not exists)",
    )
    shared.add_argument(
        "--force", action="store_true",
        help="Re-run phase even if already marked complete",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # validate
    p_val = sub.add_parser(
        "validate", parents=[shared],
        help="Pre-flight check: validate config and run a sample scan (no files changed)",
    )
    p_val.set_defaults(func=cmd_validate)

    # scan
    p_scan = sub.add_parser(
        "scan", parents=[shared],
        help="Phase 1: scan directory/directories and index files",
    )
    p_scan.add_argument(
        "root", metavar="ROOT_PATH", nargs="?",
        help="Single source directory (omit when using --config with input_dirs)",
    )
    p_scan.add_argument(
        "--workers", type=int,
        help="Parallel ExifTool threads (default: 4; increase for SSD)",
    )
    p_scan.add_argument(
        "--secondary", action="store_true",
        help="Enable secondary resized-JPEG signals (run after reviewing scan report)",
    )
    p_scan.set_defaults(func=cmd_scan)

    # report
    p_report = sub.add_parser("report", parents=[shared], help="Phase 2: print scan report")
    p_report.set_defaults(func=cmd_report)

    # dedup
    p_dedup = sub.add_parser("dedup", parents=[shared], help="Phase 3: detect duplicates")
    p_dedup.add_argument("--workers", type=int)
    p_dedup.add_argument(
        "--hamming", type=int,
        help="Hamming distance threshold for near-dupes (default: 8)",
    )
    p_dedup.add_argument("--exact-only", action="store_true",
                          help="Run only exact duplicate detection (SHA-256)")
    p_dedup.add_argument("--near-only", action="store_true",
                          help="Run only near-duplicate detection (pHash)")
    p_dedup.set_defaults(func=cmd_dedup)

    # plan
    p_plan = sub.add_parser(
        "plan", parents=[shared],
        help="Phase 4: build action plan and confirm",
    )
    p_plan.add_argument(
        "--target", metavar="PATH",
        help="Root directory for reorganised library (overrides config; same drive as photos)",
    )
    p_plan.add_argument(
        "--dates-only", action="store_true",
        help="Run only the date-forensics audit (DB only, no disk read): rate every "
             "file's date confidence/source and log suspicious dates — no plan, no target.",
    )
    p_plan.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation prompt and auto-confirm the plan.",
    )
    p_plan.set_defaults(func=cmd_plan)

    # add — incremental: place ONLY new sources into an already-organized library
    p_add = sub.add_parser(
        "add", parents=[shared],
        help="Incrementally add NEW source(s) to an already-organized library "
             "(deduped against the whole library; never reshuffles organized files)",
    )
    p_add.add_argument(
        "sources", metavar="SOURCE_PATH", nargs="*",
        help="New source director(ies) to add (omit when using --config input_dirs)",
    )
    p_add.add_argument(
        "--target", metavar="PATH",
        help="Root of the existing organized library (same drive as the new source)",
    )
    p_add.add_argument("--workers", type=int, help="Parallel ExifTool/hash threads")
    p_add.add_argument(
        "--hamming", type=int,
        help="Hamming distance threshold for near-dupes vs the library (default: 8)",
    )
    p_add.add_argument(
        "--secondary", action="store_true",
        help="Enable secondary resized-JPEG signals (match the scan-time setting)",
    )
    p_add.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt (non-interactive)",
    )
    p_add.add_argument(
        "--no-execute", action="store_true",
        help="Build and confirm the plan but do NOT move files (run execute later)",
    )
    p_add.set_defaults(func=cmd_add)

    # execute
    p_exec = sub.add_parser(
        "execute", parents=[shared],
        help="Phase 5: execute confirmed operations (optionally a filtered subset)",
    )
    p_exec.add_argument("--year", help="Only move files from this EXIF year, e.g. 2023")
    p_exec.add_argument("--camera", help="Only move files whose camera model contains this text")
    p_exec.add_argument("--software", help="Only move files whose software contains this text (e.g. Lightroom)")
    p_exec.add_argument(
        "--type", metavar="FILE_TYPE",
        help="Only move this file_type (RAW / CAMERA_JPEG / DEV_JPEG / RESIZED_JPEG / VIDEO)",
    )
    p_exec.add_argument(
        "--skip-preflight", action="store_true",
        help="Bypass the up-front same-volume / writability gate (power users)",
    )
    p_exec.set_defaults(func=cmd_execute)

    # reconcile
    p_recon = sub.add_parser(
        "reconcile", parents=[shared],
        help="Conservation proof: prove every scanned file is in exactly one "
             "terminal state (balance sheet; UNACCOUNTED must be 0)",
    )
    p_recon.add_argument(
        "--verify-disk", action="store_true",
        help="Also confirm each moved/staged file still exists on disk "
             "(I/O; flags any 'done' file missing from its claimed location)",
    )
    p_recon.set_defaults(func=cmd_reconcile)

    # audit — read-only DB consistency audit (decision-ledger health check,
    # distinct from validate's pre-flight env check and reconcile's
    # conservation proof). Never touches disk.
    p_audit = sub.add_parser(
        "audit", parents=[shared],
        help="DB consistency audit: orphan refs, dangling dupes, unresolved "
             "reviews, path collisions, event-name residue, empty twins, "
             "low-confidence dates (read-only, DB-only)",
    )
    p_audit.set_defaults(func=cmd_audit)

    # sync — fast, EXPLICIT DB resync for manual Explorer edits to already-
    # organized ('done') files (Pillar 2). Unlike `relocate` (which scans the
    # whole files table to discover what moved), the user states the old/new
    # path directly — no library-wide scan. rename/move write a 'RENAME' op
    # so the EXISTING `undo --op-type RENAME` reverses it for free; delete is
    # a one-way acknowledgment (nothing to undo — the bytes are gone).
    p_sync = sub.add_parser(
        "sync",
        help="Fast DB resync for a manual Explorer edit to an already-organized "
             "file/folder: rename, move, or acknowledge a delete (no rescan)",
    )
    sync_sub = p_sync.add_subparsers(dest="sync_action", required=True)

    p_sync_rename = sync_sub.add_parser(
        "rename", parents=[shared],
        help="Re-point every file under a renamed event folder",
    )
    p_sync_rename.add_argument("old", help="Old folder path (must no longer exist on disk)")
    p_sync_rename.add_argument("new", help="New folder path (must exist on disk)")
    p_sync_rename.set_defaults(func=cmd_sync, sync_action="rename")

    p_sync_move = sync_sub.add_parser(
        "move", parents=[shared],
        help="Re-point a single manually-moved file",
    )
    p_sync_move.add_argument("old", help="Old file path (must no longer exist on disk)")
    p_sync_move.add_argument("new", help="New file path (must exist on disk)")
    p_sync_move.set_defaults(func=cmd_sync, sync_action="move")

    p_sync_delete = sync_sub.add_parser(
        "delete", parents=[shared],
        help="Acknowledge a file/folder manually deleted outside the staging workflow",
    )
    p_sync_delete.add_argument("path", help="Path that no longer exists on disk")
    p_sync_delete.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )
    p_sync_delete.set_defaults(func=cmd_sync, sync_action="delete")

    # relocate — re-point files.path for manually-moved files via sha256
    p_reloc = sub.add_parser(
        "relocate", parents=[shared],
        help="Re-point files.path for manually-moved files via sha256 (no rebuild)",
    )
    p_reloc.add_argument("root", nargs="?", help="Optional scan root (else config input_dirs)")
    p_reloc.add_argument(
        "--prune", action="store_true",
        help="Delete DB rows whose file was removed from the library (path gone, "
             "not found in scan roots). Keeps 'done' rows. Writes pruned paths to "
             "_staging/pruned_paths.txt.",
    )
    p_reloc.add_argument(
        "--prune-only", action="store_true",
        help="Prune rows whose file is gone WITHOUT re-walking/re-pointing "
             "(fast; assumes you already ran relocate).",
    )
    p_reloc.set_defaults(func=cmd_relocate)

    # clone — non-destructive incremental backup to another volume
    p_clone = sub.add_parser(
        "clone", parents=[shared],
        help="Replicate the organized library to another drive/NAS: incremental, "
             "verified against the DB's known-good hashes, never deletes the source",
    )
    p_clone.add_argument(
        "dest", metavar="DEST_ROOT",
        help="Destination root on another volume (e.g. E:\\Backup or a NAS path)",
    )
    p_clone.add_argument(
        "--target", metavar="PATH",
        help="Organized library root to replicate (overrides config); "
             "defaults to the common ancestor of organized files",
    )
    p_clone.add_argument(
        "--verify-all", action="store_true",
        help="Paranoid: re-hash EVERY destination file against the DB (slow), "
             "not just newly-copied ones",
    )
    p_clone.add_argument(
        "--prune", action="store_true",
        help="Remove destination files no longer in the library (OFF by default; "
             "warns and lists before removing)",
    )
    p_clone.set_defaults(func=cmd_clone)

    # undo
    p_undo = sub.add_parser(
        "undo", parents=[shared],
        help="Revert completed moves back to their original locations "
             "(optionally a filtered subset)",
    )
    p_undo.add_argument("--year", help="Only revert files from this EXIF year, e.g. 2023")
    p_undo.add_argument("--camera", help="Only revert files whose camera model contains this text")
    p_undo.add_argument("--software", help="Only revert files whose software contains this text (e.g. Lightroom)")
    p_undo.add_argument(
        "--type", metavar="FILE_TYPE",
        help="Only revert this file_type (RAW / CAMERA_JPEG / DEV_JPEG / RESIZED_JPEG / VIDEO)",
    )
    p_undo.add_argument(
        "--op-type", metavar="OP_TYPE", dest="op_type",
        help="Only revert this op_type, e.g. STAGE_DELETE (undo staging) or MOVE (undo organising)",
    )
    p_undo.set_defaults(func=cmd_undo)

    # unknown-cameras
    p_unk = sub.add_parser(
        "unknown-cameras", parents=[shared],
        help="Show the distribution of files with no camera model (DB only, no disk read)",
    )
    p_unk.set_defaults(func=cmd_unknown_cameras)

    # reclassify
    p_reclass = sub.add_parser(
        "reclassify", parents=[shared],
        help="Re-run file_type classification from the DB only (no disk read) — "
             "e.g. to apply the DEV_JPEG split without re-scanning",
    )
    p_reclass.add_argument(
        "--secondary", action="store_true",
        help="Use secondary resized-JPEG signals (match the setting used at scan time)",
    )
    p_reclass.set_defaults(func=cmd_reclassify)

    # folder-merge
    p_fmerge = sub.add_parser(
        "folder-merge", parents=[shared],
        help="Detect twin folders (wholesale copies) → folder_overlaps table",
    )
    p_fmerge.set_defaults(func=cmd_folder_merge)

    # timings
    p_timings = sub.add_parser(
        "timings", parents=[shared],
        help="Show how long each command (scan/dedup/plan/execute/…) took — "
             "runs, last/avg/min/max wall-clock time (DB only, no disk read)",
    )
    p_timings.set_defaults(func=cmd_timings)

    # review
    p_review = sub.add_parser(
        "review", parents=[shared],
        help="Near-duplicate review (Phase 3B follow-up); cluster-based",
    )
    p_review.add_argument(
        "--auto", action="store_true",
        help="Cluster near-dupes and auto-resolve unambiguous groups "
             "(dry-run preview unless --commit)",
    )
    p_review.add_argument(
        "--commit", action="store_true",
        help="With --auto: actually record the auto-decisions (default: dry-run)",
    )
    p_review.add_argument(
        "--all", action="store_true",
        help="Manual review: also walk distinct look-alike (burst) clusters "
             "(default skips them — they are kept)",
    )
    p_review.add_argument(
        "--web", action="store_true",
        help="Serve a local HTML contact-sheet (thumbnails + smart defaults) "
             "instead of the terminal review; decisions save live to the DB",
    )
    p_review.add_argument(
        "--port", type=int, default=0,
        help="With --web or --folders: TCP port to bind on 127.0.0.1 (default: ephemeral)",
    )
    p_review.add_argument(
        "--folders", action="store_true",
        help="Review twin-folder pairs (from folder-merge) instead of near-duplicate images",
    )
    p_review.add_argument(
        "--organize", action="store_true",
        help="Assign event names / dates to no-event or low-confidence-date folders "
             "(writes folder_overrides consulted by plan)",
    )
    p_review.add_argument(
        "--year", metavar="YYYY",
        help="With --organize: scope candidates to one year at a time "
             "(batch P3 triage instead of facing the whole library at once)",
    )
    p_review.set_defaults(func=cmd_review)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()
    command = getattr(args, "command", "?")

    started_iso = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    status = "ok"
    try:
        args.func(args)
    except KeyboardInterrupt:
        status = "interrupted"
        console.print("\n[yellow]Interrupted — progress saved, safe to resume.[/yellow]")
        sys.exit(0)
    except (SchemaVersionError, FileNotFoundError) as e:
        status = "error"
        print_error(str(e))
        sys.exit(1)
    except SystemExit as e:
        # Handlers call sys.exit() directly; non-zero code means failure.
        status = "error" if (e.code not in (0, None)) else "ok"
        raise
    except Exception as e:
        status = "error"
        print_error(f"Unexpected error: {e}")
        raise
    finally:
        duration = time.monotonic() - t0
        finished_iso = datetime.now(timezone.utc).isoformat()
        # Don't clutter output for the timings view itself.
        if command != "timings":
            tag = "" if status == "ok" else f" [{status}]"
            console.print(
                f"[dim]⏱  {command} finished in "
                f"{_fmt_duration(duration)}{tag}[/dim]"
            )
        _persist_timing(
            args, command, started_iso, finished_iso, duration, status
        )


if __name__ == "__main__":
    main()
