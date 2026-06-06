"""
validator.py — Pre-flight validation and dry-run for photo-organizer.

Run this before touching your real photo library.

What it checks
--------------
1. config.json structure and path rules
2. ExifTool availability (version printed)
3. DB path is writable
4. Target volume matches source volume (os.rename() requirement)
5. Sample scan: ~50 files per input_dir through ExifTool
   - Counts by file type
   - Shows date range and camera models found
   - Reports any EXIF read failures
   - Estimates total file count

Nothing is written to the real database.  A disposable in-memory DB is
used so every validation pass starts clean.
"""

from __future__ import annotations

import os
import random
import shutil
import sqlite3
import tempfile
from pathlib import Path

from rich import box
from rich.panel import Panel
from rich.table import Table

from .classifier import FileClassifier
from .config import PhotoConfig
from .exiftool import ExifToolBatch, parse_datetime
from .progress import console, print_phase_header, print_success, print_warning, print_error

SAMPLE_PER_DIR = 50   # files to ExifTool per input_dir during validation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _same_drive(a: Path, b: Path) -> bool:
    """
    Return True if a and b are on the same filesystem / drive.
    Uses os.stat().st_dev which works on both Windows and POSIX.
    """
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False  # can't stat — assume mismatch, warn user


def _count_files(root: Path, classifier: FileClassifier) -> int:
    """Walk root and count supported files (no EXIF — fast)."""
    n = 0
    skip = {"_staging", ".Spotlight-V100", ".Trashes", ".fseventsd"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for name in filenames:
            if not name.startswith(".") and classifier.is_supported(Path(name)):
                n += 1
    return n


def _sample_files(root: Path, classifier: FileClassifier, n: int) -> list[Path]:
    """Return up to n random supported files from root (reservoir sampling)."""
    reservoir: list[Path] = []
    skip = {"_staging", ".Spotlight-V100", ".Trashes", ".fseventsd"}
    i = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if not classifier.is_supported(p):
                continue
            i += 1
            if len(reservoir) < n:
                reservoir.append(p)
            else:
                j = random.randint(0, i - 1)
                if j < n:
                    reservoir[j] = p
    return reservoir


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------

def validate(cfg: PhotoConfig) -> bool:
    """
    Run full pre-flight validation.
    Returns True if all checks pass (warnings are OK), False if any error.
    """
    print_phase_header("0/5", "Pre-flight Validation")
    all_ok = True

    # ── 1. Config rules ──────────────────────────────────────────────────────
    console.rule("[dim]Step 1 — Config validation[/dim]")
    errors, warnings = cfg.validate()

    for e in errors:
        print_error(e)
        all_ok = False
    for w in warnings:
        print_warning(w)

    if not errors:
        print_success(f"Config OK  ({len(cfg.input_dirs)} input dirs)")
    else:
        console.print("[red]Fix the errors above before continuing.[/red]")
        return False

    # ── 2. ExifTool ──────────────────────────────────────────────────────────
    console.rule("[dim]Step 2 — ExifTool[/dim]")
    et_path = shutil.which("exiftool") or shutil.which("exiftool.exe")
    if et_path is None:
        print_error(
            "exiftool not found in PATH.\n"
            "  Windows: https://exiftool.org → rename to exiftool.exe → add to PATH"
        )
        all_ok = False
    else:
        import subprocess
        ver = subprocess.run(
            [et_path, "-ver"], capture_output=True, text=True
        ).stdout.strip()
        print_success(f"ExifTool {ver} found at {et_path}")

    # ── 3. DB path writable ──────────────────────────────────────────────────
    console.rule("[dim]Step 3 — Database path[/dim]")
    db_parent = cfg.db.parent
    if not db_parent.exists():
        print_error(f"DB parent directory does not exist: {db_parent}")
        all_ok = False
    else:
        # Try writing a temp file to confirm write permission
        try:
            probe = db_parent / ".photo_organizer_probe"
            probe.touch()
            probe.unlink()
            print_success(f"DB path writable: {cfg.db}")
        except OSError as e:
            print_error(f"DB path not writable: {db_parent} — {e}")
            all_ok = False

    # ── 4. Volume check (target must share drive with sources) ───────────────
    if cfg.target is not None:
        console.rule("[dim]Step 4 — Volume / drive check[/dim]")
        target_ref = cfg.target.parent if not cfg.target.exists() else cfg.target
        # Find the first existing ancestor of target to stat
        t_probe = cfg.target
        while not t_probe.exists() and t_probe != t_probe.parent:
            t_probe = t_probe.parent

        for src in cfg.input_dirs:
            if not src.exists():
                continue
            if _same_drive(src, t_probe):
                print_success(f"Same volume: {src}  ↔  {cfg.target}")
            else:
                print_error(
                    f"Different volume: {src}  ↔  {cfg.target}\n"
                    f"  os.rename() cannot cross drives — choose a --target on the same drive."
                )
                all_ok = False

    # ── 5. Sample scan ───────────────────────────────────────────────────────
    console.rule("[dim]Step 5 — Sample scan (ExifTool dry-run)[/dim]")
    classifier = FileClassifier(use_secondary_signals=cfg.use_secondary_signals)

    overall_types: dict[str, int] = {}
    overall_cameras: dict[str, int] = {}
    overall_dates: list[str] = []
    overall_errors = 0
    overall_sample = 0
    overall_estimated = 0

    dir_table = Table(box=box.SIMPLE_HEAVY, show_header=True)
    dir_table.add_column("Input Dir", style="cyan")
    dir_table.add_column("Est. Files", justify="right")
    dir_table.add_column("Sample", justify="right")
    dir_table.add_column("RAW", justify="right")
    dir_table.add_column("JPEG", justify="right")
    dir_table.add_column("Resized", justify="right")
    dir_table.add_column("EXIF errors", justify="right")

    with ExifToolBatch() as et:
        for src_dir in cfg.input_dirs:
            if not src_dir.exists():
                dir_table.add_row(str(src_dir), "—", "—", "—", "—", "—", "[red]dir missing[/red]")
                continue

            with console.status(f"  Counting files in {src_dir} …"):
                estimated = _count_files(src_dir, classifier)
            overall_estimated += estimated

            sample = _sample_files(src_dir, classifier, SAMPLE_PER_DIR)
            overall_sample += len(sample)

            if not sample:
                dir_table.add_row(str(src_dir), f"{estimated:,}", "0", "—", "—", "—", "—")
                continue

            exif_map = et.read(sample)

            dir_types: dict[str, int] = {}
            dir_errors = 0
            for p in sample:
                exif = exif_map.get(str(p), {})
                if not exif:
                    dir_errors += 1
                    overall_errors += 1
                ft = classifier.classify(p, exif)
                dir_types[ft] = dir_types.get(ft, 0) + 1
                overall_types[ft] = overall_types.get(ft, 0) + 1

                dt = parse_datetime(exif)
                if dt:
                    overall_dates.append(dt)

                cam = exif.get("Model")
                if cam:
                    overall_cameras[cam] = overall_cameras.get(cam, 0) + 1

            dir_table.add_row(
                str(src_dir),
                f"{estimated:,}",
                f"{len(sample)}",
                f"{dir_types.get('RAW', 0)}",
                f"{dir_types.get('CAMERA_JPEG', 0)}",
                f"{dir_types.get('RESIZED_JPEG', 0)}",
                f"[red]{dir_errors}[/red]" if dir_errors else "0",
            )

    console.print(dir_table)

    # ── Summary ──────────────────────────────────────────────────────────────
    console.rule("[dim]Sample scan summary[/dim]")

    sum_table = Table(box=box.SIMPLE, show_header=False)
    sum_table.add_column("Key", style="dim")
    sum_table.add_column("Value")

    sum_table.add_row("Estimated total files", f"{overall_estimated:,}")
    sum_table.add_row("Sample scanned", f"{overall_sample}")
    for ft, n in sorted(overall_types.items()):
        sum_table.add_row(f"  {ft}", str(n))
    if overall_dates:
        sum_table.add_row("Date range (sample)", f"{min(overall_dates)[:10]}  →  {max(overall_dates)[:10]}")
    if overall_cameras:
        top = sorted(overall_cameras.items(), key=lambda x: -x[1])[:5]
        sum_table.add_row("Top cameras (sample)", ",  ".join(f"{m} ({n})" for m, n in top))
    sum_table.add_row("EXIF errors (sample)", str(overall_errors))

    console.print(sum_table)

    # ── Final verdict ────────────────────────────────────────────────────────
    console.print()
    if all_ok and overall_errors == 0:
        console.print(
            Panel(
                "[bold green]✓ All checks passed.[/bold green]\n\n"
                "You are ready to run the full pipeline:\n\n"
                "  [cyan]python -m photo_organizer scan   --config config.json[/cyan]\n"
                "  [cyan]python -m photo_organizer report --config config.json[/cyan]\n"
                "  [cyan]python -m photo_organizer dedup  --config config.json[/cyan]\n"
                "  [cyan]python -m photo_organizer plan   --config config.json[/cyan]\n"
                "  [cyan]python -m photo_organizer execute --config config.json[/cyan]",
                title="Validation Result",
                border_style="green",
            )
        )
    elif all_ok and overall_errors > 0:
        print_warning(
            f"{overall_errors} EXIF read errors in sample — "
            "ExifTool may struggle with some files.  "
            "Errors will be logged and skipped during the real scan."
        )
    else:
        console.print(
            Panel(
                "[bold red]✗ Validation failed — fix the errors above before running.[/bold red]",
                title="Validation Result",
                border_style="red",
            )
        )

    return all_ok
