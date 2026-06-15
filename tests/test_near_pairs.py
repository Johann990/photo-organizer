"""
Tests for the near-duplicate pair search (`_find_near_pairs`).

The critical guarantee: parallel and serial paths are LOSSLESS and identical —
they return exactly the same pair set as a brute-force O(n²) reference. The
multiprocessing path only partitions which keys each worker queries; it must
never add or drop a pair.
"""

from __future__ import annotations

from photo_organizer.bktree import hamming_distance
from photo_organizer.deduper import _find_near_pairs


def _brute_force(keys, threshold):
    """Reference: every unordered pair within threshold (b > a)."""
    keys = sorted(set(keys))
    out = set()
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            d = hamming_distance(a, b)
            if d <= threshold:
                lo, hi = (a, b) if a < b else (b, a)
                out.add((lo, hi, d))
    return out


def _normalize(pairs):
    """Collapse to a set of (min, max, dist), tolerating either emit order."""
    out = set()
    for a, b, d in pairs:
        lo, hi = (a, b) if a < b else (b, a)
        out.add((lo, hi, d))
    return out


# A spread of 64-bit values: some near-clusters, some far apart.
_KEYS = [
    0x0000000000000000,
    0x0000000000000001,   # Hamming 1 from the first
    0x0000000000000003,   # Hamming 2 from the first
    0x000000000000000F,   # Hamming 4 from the first
    0x00000000000000FF,   # Hamming 8 from the first
    0x8000000000000000,   # high bit only — the BK-tree-vs-sort counterexample
    0x8000000000000001,   # Hamming 1 from the high-bit key
    0xFFFFFFFFFFFFFFFF,   # all bits — far from everything
    0xFFFFFFFFFFFFFFFE,   # Hamming 1 from all-bits
    0x0F0F0F0F0F0F0F0F,
    0x0F0F0F0F0F0F0F0E,   # Hamming 1
    0x1234567890ABCDEF,
]


def test_serial_matches_bruteforce():
    for threshold in (0, 1, 2, 4, 6, 8):
        got = _normalize(_find_near_pairs(_KEYS, threshold, n_proc=1))
        assert got == _brute_force(_KEYS, threshold), f"threshold={threshold}"


def test_parallel_matches_serial():
    # Enough keys + n_proc>1 to actually spawn the Pool path.
    keys = [(i * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF for i in range(300)]
    for threshold in (4, 6):
        serial = _normalize(_find_near_pairs(keys, threshold, n_proc=1))
        parallel = _normalize(_find_near_pairs(keys, threshold, n_proc=3))
        assert parallel == serial, f"threshold={threshold}"


def test_each_pair_emitted_once():
    pairs = list(_find_near_pairs(_KEYS, 8, n_proc=1))
    assert len(pairs) == len(set((min(a, b), max(a, b)) for a, b, _ in pairs))
