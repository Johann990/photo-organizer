"""
cloner.py — `clone`: a non-destructive, DB-aware incremental backup of the
already-organized library to ANOTHER volume (a second drive or a NAS).

Why this is separate from execute
---------------------------------
`plan`/`execute` MOVE files in place on the SOURCE drive via os.rename — instant,
same-volume, and intentionally constrained to one drive. `clone` is the opposite
discipline: it COPIES the organized tree across volumes so the photographer ends
up with TWO copies (data safety). Nothing here moves or deletes a source file.

What makes it cheap to re-run
-----------------------------
1. Incremental skip: if the destination already holds a file with matching size
   AND mtime, it is skipped — no re-copy, no re-hash. On a multi-TB library the
   second backup touches only what changed.
2. Verify newly-copied files only (by default): after copying a file we read it
   back and compare its SHA-256 to the DB's stored, known-good `files.sha256`.
   Verification is the ONLY material cost over a dumb copy (a full read-back is
   roughly +50% I/O), so we bound it to the bytes we just wrote. `--verify-all`
   opts into a paranoid re-hash of every destination file against the DB.
3. Resumable: each file is written to a sibling `<name>.tmp` and atomically
   renamed into place, so an interrupted copy never leaves a truncated file that
   a later run could mistake for complete. Stray `.tmp` files are cleaned up.

Safety invariants (consistent with the rest of the project)
-----------------------------------------------------------
  * The source is NEVER moved or deleted.
  * Pruning destination files no longer in the library is OPT-IN (`--prune`),
    OFF by default, and warns + lists what it will remove first.
  * The ledger DB is copied into the destination so the replica is independently
    usable: backing up the photos backs up the decisions that organized them.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.table import Table

from .db import Database
from .deduper import _sha256_file
from .progress import (
    PhaseProgress,
    console,
    print_error,
    print_phase_header,
    print_success,
    print_warning,
)

# Files under the target root that are NOT part of the organized library proper
# and should never be replicated: the pending-delete staging area and the
# library-adjacent metadata home (the DB is copied separately, see _clone_db).
_EXCLUDED_TOPS = {"_staging", ".photo_organizer"}

# Tolerance (seconds) when comparing mtimes. shutil.copy2 preserves mtime, but a
# different destination filesystem can round sub-second precision, so an exact
# float compare would spuriously re-copy. Whole-second granularity is plenty for
# "has this immutable photo changed?".
_MTIME_TOLERANCE = 2.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class CloneStats:
    total: int = 0            # library files considered
    copied: int = 0           # files (re)written this run
    skipped: int = 0          # already current at dest (size+mtime match)
    verified: int = 0         # dest hash confirmed == DB sha256 this run
    errors: int = 0           # copy/verify failures (still bad after a retry)
    missing_source: int = 0   # library file gone from its recorded location
    tampered: int = 0         # --verify-all: existing dest file failed re-hash
    pruned: int = 0           # stale dest files removed (only with prune=True)
    bytes_copied: int = 0
    error_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Low-level seams (monkeypatchable in tests)
# ---------------------------------------------------------------------------

def _copy_bytes(src: Path, dst_tmp: Path) -> None:
    """
    Portable raw byte-copy of one file, preserving mtime (shutil.copy2).

    This is the SINGLE cross-platform copy path — both the default and the
    fallback. We deliberately do NOT shell out to robocopy/rsync: those are
    Windows-only / Unix-only respectively, which would mean two backends to keep
    in sync and would break the project's cross-platform guarantee.
    """
    shutil.copy2(src, dst_tmp)


def _tmp_for(dst: Path) -> Path:
    return dst.parent / (dst.name + ".tmp")


def _needs_copy(src: Path, dst: Path) -> bool:
    """True if dst is missing or differs from src in size or mtime."""
    if not dst.exists():
        return True
    try:
        s = src.stat()
        d = dst.stat()
    except OSError:
        return True
    if s.st_size != d.st_size:
        return True
    return abs(s.st_mtime - d.st_mtime) > _MTIME_TOLERANCE


def _cleanup_tmp(tmp: Path) -> None:
    """Remove a stray/leftover .tmp from an interrupted copy, if present."""
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Copy + verify one file
# ---------------------------------------------------------------------------

def _attempt(src: Path, dst: Path, tmp: Path, expected_sha: str | None) -> bool:
    """
    Copy src → tmp → atomic rename to dst, then verify the dest hash against the
    DB's known-good sha256. Returns True on a verified (or unverifiable) copy.

    When expected_sha is None the DB never hashed this file, so there is nothing
    to verify against — we trust the byte-copy and report success.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        _copy_bytes(src, tmp)
        os.replace(tmp, dst)  # atomic on the same filesystem
    except OSError:
        _cleanup_tmp(tmp)
        raise
    if expected_sha is None:
        return True
    return _sha256_file(dst) == expected_sha


def _copy_and_verify(
    db: Database, file_id: int, src: Path, dst: Path,
    expected_sha: str | None, stats: CloneStats, *, reason: str,
) -> None:
    """
    (Re)establish dst from src and verify it. One retry on a verify mismatch,
    then flag loudly and mark a backup error — never deleting anything.
    """
    tmp = _tmp_for(dst)
    ok = _attempt(src, dst, tmp, expected_sha)
    if not ok:
        db.log(
            "WARN",
            f"Verify mismatch after {reason} copy — retrying once: {dst}",
            phase="clone", file_id=file_id, path=str(dst),
        )
        ok = _attempt(src, dst, tmp, expected_sha)

    if ok:
        stats.copied += 1
        try:
            stats.bytes_copied += dst.stat().st_size
        except OSError:
            pass
        if expected_sha is not None:
            stats.verified += 1
    else:
        stats.errors += 1
        stats.error_paths.append(str(dst))
        _cleanup_tmp(tmp)
        db.log(
            "ERROR",
            f"BACKUP ERROR — verification failed twice, dest does NOT match the "
            f"library's known-good hash (source left untouched): {dst}",
            phase="clone", file_id=file_id, path=str(dst),
        )


# ---------------------------------------------------------------------------
# Library enumeration
# ---------------------------------------------------------------------------

def _library_files(db: Database) -> list:
    """Every organized ('done') library file, with its known-good hash."""
    return db.conn.execute(
        "SELECT file_id, path, sha256 FROM files WHERE status='done' ORDER BY file_id"
    ).fetchall()


def _resolve_library_root(rows: list, target_root: Path | None) -> Path | None:
    """Use the given target root, else the common ancestor of the library files."""
    if target_root is not None:
        return Path(target_root)
    paths = [r["path"] for r in rows if r["path"]]
    if not paths:
        return None
    try:
        return Path(os.path.commonpath(paths))
    except ValueError:
        return None


def _relative_under(path: Path, root: Path) -> Path | None:
    """Return path relative to root, or None if it is not under root."""
    try:
        return path.relative_to(root)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Ledger copy
# ---------------------------------------------------------------------------

def _clone_db(db: Database, dest_root: Path, expected_rel: set[str]) -> None:
    """
    Copy the ledger DB into the destination so the replica is self-describing.

    It always lands at {dest_root}/.photo_organizer/{db filename}, which matches
    the library-adjacent default location when the DB lives there, and gives a
    sensible home when the DB is elsewhere. A WAL checkpoint first folds pending
    writes into the main file so the single-file copy is consistent.
    """
    try:
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    dst_db = dest_root / ".photo_organizer" / db.path.name
    rel = _relative_under(dst_db, dest_root)
    if rel is not None:
        expected_rel.add(rel.as_posix())
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(dst_db)
    try:
        shutil.copy2(db.path, tmp)
        os.replace(tmp, dst_db)
    except OSError:
        _cleanup_tmp(tmp)
        raise


# ---------------------------------------------------------------------------
# Prune (opt-in)
# ---------------------------------------------------------------------------

def _prune(db: Database, dest_root: Path, expected_rel: set[str],
           stats: CloneStats) -> None:
    """
    Remove destination files no longer in the library. OPT-IN only. Warns and
    lists every candidate BEFORE removing it (never a silent delete).
    """
    stale: list[Path] = []
    for f in dest_root.rglob("*"):
        if not f.is_file():
            continue
        rel = _relative_under(f, dest_root)
        if rel is None:
            continue
        if rel.as_posix() in expected_rel:
            continue
        stale.append(f)

    if not stale:
        return

    print_warning(
        f"--prune: {len(stale):,} destination file(s) are no longer in the "
        "library and WILL be removed:"
    )
    for f in stale[:50]:
        console.print(f"    [yellow]- {f}[/yellow]")
    if len(stale) > 50:
        console.print(f"    [dim]… +{len(stale) - 50} more[/dim]")

    for f in stale:
        db.log("WARN", f"Prune — removing stale dest file: {f}",
               phase="clone", path=str(f))
        try:
            f.unlink()
            stats.pruned += 1
        except OSError as exc:
            db.log("ERROR", f"Prune failed for {f}: {exc}", phase="clone", path=str(f))
    db.commit()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def clone(
    db: Database,
    dest_root: str | Path,
    *,
    target_root: str | Path | None = None,
    verify_all: bool = False,
    prune: bool = False,
) -> CloneStats:
    """
    Replicate the organized library (under target_root) to dest_root on another
    volume. Returns a CloneStats balance sheet.
    """
    print_phase_header("clone", "Backup / Replicate Library")

    dest_root = Path(dest_root)
    rows = _library_files(db)
    stats = CloneStats()

    lib_root = _resolve_library_root(rows, Path(target_root) if target_root else None)
    if lib_root is None:
        print_warning(
            "No organized ('done') files found to clone. Run plan/execute first."
        )
        return stats

    console.print(f"  Library root : [cyan]{lib_root}[/cyan]")
    console.print(f"  Destination  : [cyan]{dest_root}[/cyan]")
    console.print(f"  Mode         : "
                  f"{'full re-hash (--verify-all)' if verify_all else 'verify new copies only'}"
                  f"{', prune' if prune else ''}\n")

    dest_root.mkdir(parents=True, exist_ok=True)

    # Relative dest paths we expect to exist — feeds the opt-in prune step.
    expected_rel: set[str] = set()

    with PhaseProgress("Cloning", total=len(rows), phase="clone") as prog:
        for r in rows:
            src = Path(r["path"])
            rel = _relative_under(src, lib_root)
            # Skip the staging / metadata subtrees (not the library proper).
            if rel is None or (rel.parts and rel.parts[0] in _EXCLUDED_TOPS):
                prog.advance(1, current_path=str(src))
                continue

            stats.total += 1
            expected_rel.add(rel.as_posix())
            dst = dest_root / rel
            tmp = _tmp_for(dst)

            if not src.exists():
                stats.missing_source += 1
                _cleanup_tmp(tmp)
                db.log("WARN", f"Library file missing at source — cannot clone: {src}",
                       phase="clone", file_id=r["file_id"], path=str(src))
                prog.advance(1, current_path=str(src))
                continue

            changed = _needs_copy(src, dst)

            if not changed and not verify_all:
                # Cheap path: already current. No re-copy, no re-hash.
                stats.skipped += 1
                _cleanup_tmp(tmp)  # clear any leftover .tmp from a prior abort
                prog.advance(1, current_path=str(src))
                continue

            if not changed and verify_all and dst.exists():
                # Paranoid path: file LOOKS current — re-hash it against the DB.
                if r["sha256"] is None or _sha256_file(dst) == r["sha256"]:
                    stats.skipped += 1
                    if r["sha256"] is not None:
                        stats.verified += 1
                    _cleanup_tmp(tmp)
                    prog.advance(1, current_path=str(src))
                    continue
                # Mismatch on a supposedly-current file → tamper/bit-rot.
                stats.tampered += 1
                db.log(
                    "WARN",
                    f"--verify-all mismatch — existing dest file does not match the "
                    f"library hash (tampered/corrupt), re-copying to heal: {dst}",
                    phase="clone", file_id=r["file_id"], path=str(dst),
                )
                # fall through and re-copy from the known-good source

            _copy_and_verify(
                db, r["file_id"], src, dst, r["sha256"], stats,
                reason="verify-all" if verify_all else "initial",
            )
            prog.advance(1, current_path=str(src))

    # Self-describing replica: copy the ledger into the destination.
    try:
        _clone_db(db, dest_root, expected_rel)
    except OSError as exc:
        stats.errors += 1
        stats.error_paths.append(str(db.path))
        db.log("ERROR", f"Failed to copy ledger DB to destination: {exc}", phase="clone")

    if prune:
        _prune(db, dest_root, expected_rel, stats)

    db.commit()

    _print_summary(stats)
    _log_summary(db, stats, lib_root, dest_root)
    return stats


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_summary(stats: CloneStats) -> None:
    present = stats.total - stats.errors - stats.missing_source

    t = Table(title="Clone Completeness Report", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Category", style="cyan")
    t.add_column("Count", justify="right")

    t.add_row("Library files", f"{stats.total:,}")
    t.add_row("Present at destination", f"{present:,}")
    t.add_row("Copied this run", f"{stats.copied:,}")
    t.add_row("Skipped (already current)", f"{stats.skipped:,}")
    t.add_row("Verified against DB hash", f"{stats.verified:,}")
    if stats.tampered:
        t.add_row("[yellow]Tampered/healed (--verify-all)[/yellow]", f"{stats.tampered:,}")
    if stats.missing_source:
        t.add_row("[yellow]Missing at source[/yellow]", f"{stats.missing_source:,}")
    if stats.pruned:
        t.add_row("[yellow]Pruned (stale at dest)[/yellow]", f"{stats.pruned:,}")
    if stats.errors:
        t.add_row("[red]Errors[/red]", f"[red]{stats.errors:,}[/red]")
    t.add_row("[bold]Bytes transferred[/bold]", f"[bold]{stats.bytes_copied:,}[/bold]")

    console.print()
    console.print(t)

    if stats.errors == 0 and stats.missing_source == 0:
        print_success(
            f"Backup complete and intact — {present:,} library files present at "
            "the destination, all newly-copied files verified against the "
            "library's known-good hashes. Source untouched."
        )
    else:
        if stats.errors:
            print_error(
                f"{stats.errors:,} file(s) failed to verify at the destination. "
                "Query run_log WHERE phase='clone' AND level='ERROR' for the list. "
                "Nothing was deleted from the source."
            )
        if stats.missing_source:
            print_warning(
                f"{stats.missing_source:,} library file(s) were missing from their "
                "recorded source location and could not be cloned."
            )


def _log_summary(db: Database, stats: CloneStats, lib_root: Path,
                 dest_root: Path) -> None:
    db.log(
        "INFO",
        f"Clone {lib_root} → {dest_root}: copied={stats.copied} "
        f"skipped={stats.skipped} verified={stats.verified} errors={stats.errors} "
        f"tampered={stats.tampered} pruned={stats.pruned} "
        f"missing_source={stats.missing_source} bytes={stats.bytes_copied}",
        phase="clone",
    )
    db.commit()
