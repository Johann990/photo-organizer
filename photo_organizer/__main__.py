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
from pathlib import Path

from .db import Database
from .progress import console, print_error


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


def _db_path(args, cfg) -> Path:
    """Resolve DB path: CLI flag wins, then config, then error."""
    if getattr(args, "db", None):
        return Path(args.db)
    if cfg and cfg.db:
        return cfg.db
    print_error("No database path provided. Use --db or --config.")
    sys.exit(1)


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
    target = _target_path(args, cfg)

    if target is None:
        print_error(
            "No target directory provided.\n"
            "  Use: plan --target PATH --db DB  or  plan --config config.json\n"
            "  (and set \"target\" in config.json)"
        )
        sys.exit(1)

    from .planner import plan
    with Database(db_path) as db:
        plan(db, target_root=target, force=getattr(args, "force", False))


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
        )


def cmd_undo(args):
    cfg = _load_cfg(args)
    from .executor import undo
    with Database(_db_path(args, cfg)) as db:
        undo(db, force=getattr(args, "force", False))


def cmd_review(args):
    cfg = _load_cfg(args)
    from .reviewer import review_near_dupes
    with Database(_db_path(args, cfg)) as db:
        review_near_dupes(db)


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
    p_plan.set_defaults(func=cmd_plan)

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
    p_exec.set_defaults(func=cmd_execute)

    # undo
    p_undo = sub.add_parser(
        "undo", parents=[shared],
        help="Revert all completed moves back to their original locations",
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

    # review
    p_review = sub.add_parser(
        "review", parents=[shared],
        help="Interactive near-duplicate review (Phase 3B follow-up)",
    )
    p_review.set_defaults(func=cmd_review)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except FileNotFoundError as e:
        print_error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — progress saved, safe to resume.[/yellow]")
        sys.exit(0)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    main()
