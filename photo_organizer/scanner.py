"""
scanner.py — Phase 1: Scan & Index

Walks the source directory, reads EXIF via ExifTool (batch mode),
classifies each file, and writes rows to SQLite.

IO strategy:
- ExifTool -fast2 batch mode: ~200 files per subprocess call
- ThreadPoolExecutor: parallel batches (capped at 4 for HDD)
- DB commit every 200 rows
- Resumable: already-scanned paths are skipped
"""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .classifier import FileClassifier
from .db import Database
from .exiftool import (
    ExifToolBatch,
    parse_aperture,
    parse_codec,
    parse_datetime,
    parse_description,
    parse_dimensions,
    parse_duration,
    parse_focal_length,
    parse_framerate,
    parse_gps,
    parse_iso,
    parse_keywords,
    parse_label,
    parse_rating,
)
from .progress import PhaseProgress, print_phase_header, print_success, print_warning

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXIF_BATCH_SIZE = 200
DB_COMMIT_EVERY = 200
DEFAULT_WORKERS = 4   # conservative for spinning HDD; increase for SSD


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_files(root: Path, classifier: FileClassifier) -> list[Path]:
    """
    Walk root and return all supported image files.
    Skips hidden directories and _staging/.
    """
    files: list[Path] = []
    skip_dirs = {"_staging", ".Spotlight-V100", ".Trashes", ".fseventsd"}

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune directories we never want to scan
        dirnames[:] = [
            d for d in dirnames
            if d not in skip_dirs and not d.startswith(".")
        ]

        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if classifier.is_supported(p):
                files.append(p)

    return files


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def _build_row(path: Path, exif: dict, classifier: FileClassifier) -> dict[str, Any]:
    """Convert a Path + EXIF dict into a DB row dict."""
    try:
        st = path.stat()
        size_bytes = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        size_bytes = None
        mtime = None

    file_type = classifier.classify(path, exif)
    dt = parse_datetime(exif)
    w, h = parse_dimensions(exif)
    lat, lon, alt = parse_gps(exif)

    return {
        "path":               str(path),
        "filename":           path.name,
        "extension":          path.suffix.lstrip(".").lower(),
        "size_bytes":         size_bytes,
        "mtime":              mtime,
        "file_type":          file_type,
        "datetime_original":  dt,
        "datetime_digitized": exif.get("CreateDate"),
        "camera_make":        exif.get("Make"),
        "camera_model":       exif.get("Model"),
        "width":              w or None,
        "height":             h or None,
        "gps_lat":            lat,
        "gps_lon":            lon,
        "gps_alt":            alt,
        "lens_model":         exif.get("LensModel"),
        "iso":                parse_iso(exif),
        "aperture":           parse_aperture(exif),
        "shutter_speed":      exif.get("ExposureTime"),
        "focal_length":       parse_focal_length(exif),
        "software":           exif.get("Software"),
        # Metadata enrichment
        "rating":             parse_rating(exif),
        "keywords":           parse_keywords(exif),
        "description":        parse_description(exif),
        "label":              parse_label(exif),
        # Video-specific (NULL for images)
        "duration_seconds":   parse_duration(exif),
        "video_codec":        parse_codec(exif),
        "frame_rate":         parse_framerate(exif),
    }


# ---------------------------------------------------------------------------
# Batch processor (runs in thread pool)
# ---------------------------------------------------------------------------

def _process_batch(
    batch: list[Path],
    et: ExifToolBatch,
    classifier: FileClassifier,
    db: Database,
) -> tuple[int, int]:
    """
    Read EXIF for a batch of files, build rows, write to DB.
    Returns (n_inserted, n_errors).
    """
    try:
        exif_map = et.read(batch)
    except Exception as e:
        # ExifTool subprocess failure — log all as errors
        for p in batch:
            db.log_error(str(p), f"ExifTool subprocess error: {e}")
        return 0, len(batch)

    rows: list[dict] = []
    errors = 0

    for path in batch:
        exif = exif_map.get(str(path), {})
        try:
            row = _build_row(path, exif, classifier)
            rows.append(row)
        except Exception as e:
            db.log_error(str(path), str(e))
            errors += 1

    if rows:
        try:
            db.insert_file_batch(rows)
        except Exception as e:
            # DB write failure — rare but handle gracefully
            for row in rows:
                db.log_error(row["path"], f"DB insert error: {e}")
            errors += len(rows)
            return 0, errors

    return len(rows), errors


# ---------------------------------------------------------------------------
# Main scan entry point
# ---------------------------------------------------------------------------

def scan(
    root: Path,
    db: Database,
    workers: int = DEFAULT_WORKERS,
    use_secondary_signals: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """
    Phase 1: scan root directory and index all supported files into DB.

    Parameters
    ----------
    root:
        Root path on the external drive to scan.
    db:
        Connected Database instance.
    workers:
        Number of parallel ExifTool threads. Keep at 4 for spinning HDD.
    use_secondary_signals:
        Enable secondary resized-JPEG signals (enable after reviewing scan report).
    force:
        Re-scan even if phase already marked complete.

    Returns
    -------
    Summary dict with counts.
    """
    print_phase_header("1/5", "Scan & Index")

    if not force and db.phase_complete("scan"):
        print_success("Phase 1 already complete — skipping. Use --force to re-scan.")
        return {}

    if not root.exists():
        raise FileNotFoundError(f"Source directory not found: {root}")

    db.set_phase_status("scan", "running")
    classifier = FileClassifier(use_secondary_signals=use_secondary_signals)

    # ── Discover all files ─────────────────────────────────────────────────
    print(f"  Discovering files in {root} …")
    all_files = discover_files(root, classifier)
    total = len(all_files)
    print(f"  Found {total:,} supported files")

    # ── Skip already-scanned ───────────────────────────────────────────────
    already_scanned = db.get_scanned_paths()
    todo = [f for f in all_files if str(f) not in already_scanned]
    skipped = total - len(todo)

    if skipped:
        print(f"  Resuming — skipping {skipped:,} already scanned")

    if not todo:
        print_success("All files already scanned.")
        db.set_phase_status("scan", "complete", {"total": total, "skipped": skipped})
        return {"total": total, "skipped": skipped}

    # ── Split into batches, process with thread pool ───────────────────────
    batches: list[list[Path]] = [
        todo[i : i + EXIF_BATCH_SIZE]
        for i in range(0, len(todo), EXIF_BATCH_SIZE)
    ]

    total_inserted = 0
    total_errors = 0
    last_path = ""

    with PhaseProgress("Scanning", total=len(todo), phase="1/5", skipped=skipped) as prog:
        with ExifToolBatch(batch_size=EXIF_BATCH_SIZE) as et:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_process_batch, batch, et, classifier, db): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch = futures[future]
                    try:
                        inserted, errors = future.result()
                    except Exception as e:
                        inserted, errors = 0, len(batch)
                        print_warning(f"Batch error: {e}")

                    total_inserted += inserted
                    total_errors += errors
                    last_path = str(batch[-1].parent) if batch else ""

                    prog.advance(
                        n=len(batch),
                        current_path=last_path,
                        errors=errors,
                    )

                    # Update scan_state checkpoint after each batch
                    db.upsert_scan_state(
                        directory=last_path,
                        files_found=total,
                        files_scanned=total_inserted,
                    )

    # ── Final checkpoint ───────────────────────────────────────────────────
    summary = {
        "total_discovered": total,
        "skipped_resume":   skipped,
        "inserted":         total_inserted,
        "errors":           total_errors,
    }
    db.set_phase_status("scan", "complete", summary)

    print_success(
        f"Scan complete: {total_inserted:,} files indexed, "
        f"{total_errors:,} errors, {skipped:,} skipped"
    )

    if total_errors:
        print_warning(
            f"{total_errors} files had errors — check run_log table or photo_organizer.log"
        )

    return summary
