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
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Pool
from pathlib import Path

import imagehash
from PIL import Image

from . import imaging  # noqa: F401 — registers HEIC opener with Pillow on import
from .bktree import BKTree
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
# Near-duplicate pair search (parallel BK-tree)
# ---------------------------------------------------------------------------
#
# Finding all distinct-hash pairs within `threshold` is CPU-bound and
# embarrassingly parallel: each BK-tree query is independent and the tree is
# read-only once built. We split the distinct hashes across worker processes;
# each worker builds its own tree (cheap vs. the query cost it amortises) and
# queries its slice. multiprocessing sidesteps the GIL that would otherwise
# serialise the popcount-heavy queries.

# Per-process BK-tree, built once by the Pool initializer and reused for every
# chunk that worker handles (avoids rebuilding the tree per chunk).
_WORKER_TREE: BKTree | None = None

# Below this many distinct hashes the multiprocessing spawn overhead isn't
# worth it — run serially in-process instead.
_PARALLEL_MIN_KEYS = 2_000


def _init_pair_worker(all_keys: list[int]) -> None:
    global _WORKER_TREE
    _WORKER_TREE = BKTree()
    _WORKER_TREE.add_many(all_keys)


def _query_pair_chunk(args):
    """Worker: query a slice of keys against the per-process tree.

    Returns (n_keys_processed, pairs) where each pair is (a, b, hamming) with
    b > a so every unordered pair is emitted exactly once.
    """
    chunk_keys, threshold = args
    tree = _WORKER_TREE
    out: list[tuple[int, int, int]] = []
    for a in chunk_keys:
        for b, h in tree.query(a, threshold):
            if b > a:
                out.append((a, b, h))
    return len(chunk_keys), out


def _find_near_pairs(keys, threshold, n_proc=1, on_progress=None):
    """Yield (a_int, b_int, hamming) for every distinct-hash pair within
    `threshold`. Each unordered pair is emitted once (b > a).

    Lossless: the serial (n_proc<=1) and parallel paths return the SAME set of
    pairs — the parallel path only partitions which keys each worker queries.
    `on_progress(n)` is called with the number of keys processed so far so the
    caller can drive a progress bar.
    """
    keys = list(keys)

    if n_proc <= 1:
        tree = BKTree()
        tree.add_many(keys)
        for a in keys:
            for b, h in tree.query(a, threshold):
                if b > a:
                    yield (a, b, h)
            if on_progress:
                on_progress(1)
        return

    # Round-robin slices balance load (interleaved keys → similar work each).
    n_chunks = n_proc * 8
    chunks = [keys[i::n_chunks] for i in range(n_chunks)]
    chunks = [c for c in chunks if c]
    with Pool(n_proc, initializer=_init_pair_worker, initargs=(keys,)) as pool:
        for n_done, pairs in pool.imap_unordered(
            _query_pair_chunk, [(c, threshold) for c in chunks]
        ):
            yield from pairs
            if on_progress:
                on_progress(n_done)


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

    # Hash CAMERA_JPEG, DEV_JPEG, and HEIC files — all three are image types
    # whose visual content can be meaningfully compared with pHash.
    # NOTE: VIDEO files are intentionally excluded (pHash is image-only;
    # videos still get exact SHA-256 dedup in Pass A).
    _PHASH_TYPES = ["CAMERA_JPEG", "DEV_JPEG", "HEIC"]
    to_hash: list = []
    for batch in db.iter_files(file_types=_PHASH_TYPES):
        for row in batch:
            if row["phash"] is None:
                to_hash.append(row)

    total = len(to_hash)
    console.print(f"  Image files to perceptual-hash (CAMERA_JPEG/DEV_JPEG/HEIC): {total:,}")

    if total == 0:
        print_success("All image files already perceptual-hashed.")
        db.set_phase_status("dedup_near", "complete", {"phashed": 0})
        return

    phashed = 0
    errors = 0
    # phash (16-char hex str) → list of file_ids — for near-dup comparison
    phash_index: dict[str, list[int]] = {}

    # Pre-load existing phashes
    for batch in db.iter_files(file_types=_PHASH_TYPES):
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
    # pHash Hamming distance is a metric, so a BK-tree answers
    # "all hashes within `hamming_threshold` of x" with ZERO false negatives,
    # visiting only ~O(log N) nodes per query. This replaces an earlier
    # sorted-sliding-window heuristic that silently missed near-dupes whose
    # high-order pHash bits differed (e.g. 0x0…0 vs 0x8…0 are Hamming-1 yet
    # sort to opposite ends). See tests/test_bktree.py for the counterexample.
    near_pairs = 0
    # Distinct 16-char hex pHash → its 64-bit int value.
    ph_by_int: dict[int, str] = {int(ph, 16): ph for ph in phash_index}

    _PAIR_FLUSH = 10_000   # executemany every N pairs — big batches amortise overhead
    pair_buf: list[tuple[int, int, str, int]] = []

    def _flush(buf: list) -> int:
        if not buf:
            return 0
        db.insert_duplicate_batch(buf)
        db.commit()
        n = len(buf)
        buf.clear()
        return n

    # Exact perceptual matches (same hash → multiple files) are independent of
    # the distance search; emit them up front, single-pass and cheap.
    for ids in phash_index.values():
        if len(ids) > 1:
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    pair_buf.append((ids[x], ids[y], "NEAR", 0))
    near_pairs += _flush(pair_buf)

    # Distinct-hash pairs within the threshold — parallelised across cores.
    # CPU-bound and independent per query, so use processes (not the I/O-tuned
    # `workers` count, which is kept low for spinning disks during hashing).
    n_keys = len(ph_by_int)
    n_proc = 1 if n_keys < _PARALLEL_MIN_KEYS else max(1, (os.cpu_count() or 2) - 1)
    if n_proc > 1:
        console.print(f"  Searching {n_keys:,} distinct hashes across {n_proc} processes…")

    with PhaseProgress(
        "Finding near-duplicate pairs (Hamming distance)",
        total=n_keys,
        phase="3B",
    ) as p:
        for a_int, b_int, hamming in _find_near_pairs(
            ph_by_int.keys(), hamming_threshold,
            n_proc=n_proc, on_progress=p.advance,
        ):
            for id_a in phash_index[ph_by_int[a_int]]:
                for id_b in phash_index[ph_by_int[b_int]]:
                    pair_buf.append((id_a, id_b, "NEAR", hamming))
            if len(pair_buf) >= _PAIR_FLUSH:
                near_pairs += _flush(pair_buf)

    near_pairs += _flush(pair_buf)

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
