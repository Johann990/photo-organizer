"""
deduper.py — Phase 3: Duplicate Detection

Pass A — Exact duplicates via SHA-256
Pass B — Near duplicates via perceptual hash (pHash)

Both passes read files from the external drive, but only once per file.
Results are written to the duplicates table in the local DB.
No files are moved or deleted here.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import imagehash
from PIL import Image

from .db import Database
from .progress import (
    PhaseProgress,
    print_phase_header,
    print_success,
    print_warning,
    console,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHA256_CHUNK = 65_536       # 64 KB read chunks — balanced for HDD
HASH_WORKERS = 4            # keep low for spinning HDD
PHASH_HAMMING_THRESHOLD = 8 # ≤8 = near duplicate (tune empirically)
DB_COMMIT_EVERY = 500


# ---------------------------------------------------------------------------
# SHA-256 helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str | None:
    """Hash a file in chunks. Returns hex digest or None on error."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(SHA256_CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _hash_file_row(row) -> tuple[int, str, str | None]:
    """Worker: compute SHA-256 for one DB row. Returns (file_id, path, hash)."""
    digest = _sha256_file(Path(row["path"]))
    return row["file_id"], row["path"], digest


# ---------------------------------------------------------------------------
# pHash helpers
# ---------------------------------------------------------------------------

def _phash_file(path: Path) -> str | None:
    """Compute perceptual hash. Returns a 16-char hex string or None on error.

    Stored verbatim as TEXT — imagehash's native str() output is an unsigned
    64-bit value in hex, which sidesteps SQLite's signed-INTEGER overflow.
    """
    try:
        with Image.open(path) as img:
            img.thumbnail((64, 64))          # resize before hashing = faster
            return str(imagehash.phash(img))
    except Exception:
        return None


def _phash_file_row(row) -> tuple[int, str, str | None]:
    return row["file_id"], row["path"], _phash_file(Path(row["path"]))


# ---------------------------------------------------------------------------
# Pass A: exact duplicates
# ---------------------------------------------------------------------------

def dedup_exact(db: Database, workers: int = HASH_WORKERS, force: bool = False):
    """
    Compute SHA-256 for all non-RAW files (and RAW), find exact matches.
    Updates files.sha256 and inserts into duplicates table.
    """
    print_phase_header("3A/5", "Exact Duplicate Detection (SHA-256)")

    if not force and db.phase_complete("dedup_exact"):
        print_success("Phase 3A already complete — skipping.")
        return

    db.set_phase_status("dedup_exact", "running")

    # Collect files that haven't been hashed yet
    to_hash: list = []
    for batch in db.iter_files():
        for row in batch:
            if row["sha256"] is None and row["status"] not in ("error",):
                to_hash.append(row)

    total = len(to_hash)
    console.print(f"  Files to hash: {total:,}")

    if total == 0:
        print_success("All files already hashed.")
        db.set_phase_status("dedup_exact", "complete", {"hashed": 0})
        return

    hashed = 0
    errors = 0
    # sha256 → list of file_ids
    hash_map: dict[str, list[int]] = {}

    # Pre-load existing hashes from DB to merge
    for batch in db.iter_files():
        for row in batch:
            if row["sha256"]:
                hash_map.setdefault(row["sha256"], []).append(row["file_id"])

    commit_counter = 0

    with PhaseProgress("Hashing (SHA-256)", total=total, phase="3A/5") as prog:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_hash_file_row, row): row for row in to_hash}

            for future in as_completed(futures):
                file_id, path, digest = future.result()

                if digest is None:
                    db.log("ERROR", "SHA-256 failed", phase="dedup_exact",
                           file_id=file_id, path=path)
                    errors += 1
                else:
                    db.update_file(file_id, sha256=digest, status="hashed")
                    hash_map.setdefault(digest, []).append(file_id)
                    hashed += 1

                commit_counter += 1
                if commit_counter >= DB_COMMIT_EVERY:
                    db.commit()
                    commit_counter = 0

                prog.advance(1, current_path=path, errors=int(digest is None))

    db.commit()

    # ── Find exact duplicate pairs ─────────────────────────────────────────
    dup_pairs = 0
    with console.status("Finding exact duplicate pairs…"):
        for digest, ids in hash_map.items():
            if len(ids) < 2:
                continue
            # Insert all pairs (a < b enforced in db.insert_duplicate)
            ids_sorted = sorted(ids)
            for i in range(len(ids_sorted)):
                for j in range(i + 1, len(ids_sorted)):
                    db.insert_duplicate(ids_sorted[i], ids_sorted[j], "EXACT", 0)
                    dup_pairs += 1
        db.commit()

    summary = {"hashed": hashed, "errors": errors, "exact_dup_pairs": dup_pairs}
    db.set_phase_status("dedup_exact", "complete", summary)
    print_success(
        f"SHA-256 complete: {hashed:,} hashed, "
        f"{dup_pairs:,} exact duplicate pairs found, "
        f"{errors:,} errors"
    )


# ---------------------------------------------------------------------------
# Pass B: near duplicates (pHash)
# ---------------------------------------------------------------------------

def dedup_near(
    db: Database,
    workers: int = HASH_WORKERS,
    hamming_threshold: int = PHASH_HAMMING_THRESHOLD,
    force: bool = False,
):
    """
    Compute pHash for JPEG files and find near duplicates.
    Only operates on CAMERA_JPEG (not RAW, not already-flagged RESIZED).
    Near duplicates require human review — never auto-deleted.
    """
    print_phase_header("3B/5", "Near Duplicate Detection (pHash)")

    if not force and db.phase_complete("dedup_near"):
        print_success("Phase 3B already complete — skipping.")
        return

    db.set_phase_status("dedup_near", "running")

    # Only hash CAMERA_JPEGs that don't have a phash yet.
    # NOTE: VIDEO files are intentionally excluded from perceptual hashing
    # (pHash is image-only). Videos still get exact SHA-256 dedup in Pass A.
    to_hash: list = []
    for batch in db.iter_files(file_type="CAMERA_JPEG"):
        for row in batch:
            if row["phash"] is None:
                to_hash.append(row)

    total = len(to_hash)
    console.print(f"  CAMERA_JPEG files to perceptual-hash: {total:,}")

    if total == 0:
        print_success("All JPEG files already perceptual-hashed.")
        db.set_phase_status("dedup_near", "complete", {"phashed": 0})
        return

    phashed = 0
    errors = 0
    # phash (16-char hex str) → list of file_ids — for near-dup comparison
    phash_index: dict[str, list[int]] = {}

    # Pre-load existing phashes
    for batch in db.iter_files(file_type="CAMERA_JPEG"):
        for row in batch:
            if row["phash"] is not None:
                phash_index.setdefault(row["phash"], []).append(row["file_id"])

    commit_counter = 0

    with PhaseProgress("Perceptual hashing", total=total, phase="3B/5") as prog:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_phash_file_row, row): row for row in to_hash}

            for future in as_completed(futures):
                file_id, path, ph = future.result()

                if ph is None:
                    db.log("WARN", "pHash failed", phase="dedup_near",
                           file_id=file_id, path=path)
                    errors += 1
                else:
                    db.update_file(file_id, phash=ph)
                    phash_index.setdefault(ph, []).append(file_id)
                    phashed += 1

                commit_counter += 1
                if commit_counter >= DB_COMMIT_EVERY:
                    db.commit()
                    commit_counter = 0

                prog.advance(1, current_path=path)

    db.commit()

    # ── Find near-duplicate pairs via Hamming distance ─────────────────────
    # For 50k+ JPEGs a full O(n²) compare is too slow.
    # Strategy: group by identical phash first (free), then compare
    # within a sliding window sorted by phash value for close hashes.
    # The keys are fixed-width (16-char) zero-padded hex strings, so a
    # lexicographic sort is numerically identical to sorting the underlying
    # ints — the sliding-window assumption still holds.
    near_pairs = 0
    with console.status("Finding near duplicate pairs (Hamming distance)…"):
        phash_list = sorted(phash_index.keys())
        n = len(phash_list)

        for i in range(n):
            ph_a = phash_list[i]
            ids_a = phash_index[ph_a]

            # Same hash = exact perceptual match
            if len(ids_a) > 1:
                for x in range(len(ids_a)):
                    for y in range(x + 1, len(ids_a)):
                        db.insert_duplicate(ids_a[x], ids_a[y], "NEAR", 0)
                        near_pairs += 1

            # Compare against nearby hashes in sorted order
            # (hashes differing by ≤ hamming_threshold bits are numerically close
            # for many real-world cases — not perfect but fast)
            for j in range(i + 1, min(i + 200, n)):
                ph_b = phash_list[j]
                hamming = bin(int(ph_a, 16) ^ int(ph_b, 16)).count("1")
                if hamming > hamming_threshold:
                    break   # sorted order means later ones will be farther
                for id_a in ids_a:
                    for id_b in phash_index[ph_b]:
                        db.insert_duplicate(id_a, id_b, "NEAR", hamming)
                        near_pairs += 1

        db.commit()

    summary = {
        "phashed": phashed,
        "errors": errors,
        "near_dup_pairs": near_pairs,
        "hamming_threshold": hamming_threshold,
    }
    db.set_phase_status("dedup_near", "complete", summary)
    print_success(
        f"pHash complete: {phashed:,} hashed, "
        f"{near_pairs:,} near-duplicate pairs found "
        f"(Hamming ≤ {hamming_threshold}), {errors:,} errors"
    )
    if near_pairs:
        print_warning(
            f"{near_pairs:,} near-duplicate pairs require human review before deletion.\n"
            "  Run: python -m photo_organizer review --db <path>"
        )
