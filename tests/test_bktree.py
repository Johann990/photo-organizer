"""
Regression test for the near-duplicate finder.

The old implementation sorted 16-char hex pHashes lexicographically and only
compared each hash against the next ~200 in sorted order, BREAKING on the first
hash beyond the Hamming threshold ("sorted order means later ones are farther").

That assumption is false: numeric/lexicographic position of a 64-bit pHash has
almost no relationship to Hamming distance — the high-order bits dominate sort
position. The canonical counterexample:

    0x0000000000000000  and  0x8000000000000000

differ by a single bit (Hamming = 1, a very close near-dupe) yet sort to opposite
ends of the list, so the old window NEVER compared them and silently dropped the
pair. The BK-tree replacement finds it with zero false negatives.

Run: python -m pytest tests/test_bktree.py
"""

from __future__ import annotations


from photo_organizer.bktree import BKTree, hamming_distance
from photo_organizer.db import Database
from photo_organizer.deduper import dedup_near

# The two maximally-far-apart-but-Hamming-1 hashes the old window missed.
LOW = "0000000000000000"
HIGH = "8000000000000000"
# A few unrelated hashes, all > 8 bits away from BOTH LOW and HIGH, yet
# numerically "between" them in sorted order.
UNRELATED = ["0000000000000fff", "0f0f0f0f0f0f0f0f", "ffffffff00000000"]


def test_hamming_distance_basic():
    assert hamming_distance(0x0, 0x0) == 0
    assert hamming_distance(0x8000000000000000, 0x0) == 1
    assert hamming_distance(0xFFFFFFFFFFFFFFFF, 0x0) == 64


def test_bktree_finds_hamming1_across_sort_extremes():
    """The core counterexample: BK-tree finds the Hamming-1 pair the old
    sorted-window approach structurally could not."""
    keys = [int(h, 16) for h in [LOW, HIGH, *UNRELATED]]
    tree = BKTree()
    tree.add_many(keys)

    neighbours = tree.query(int(LOW, 16), 8)
    found = {k for k, _ in neighbours}

    assert int(LOW, 16) in found  # self
    assert int(HIGH, 16) in found  # the pair the old window missed
    # The unrelated hashes are all > 8 bits away from LOW and must be excluded.
    for h in UNRELATED:
        assert int(h, 16) not in found

    # And the reported distance is exactly 1.
    dist = dict(neighbours)[int(HIGH, 16)]
    assert dist == 1


def test_bktree_query_returns_all_within_distance():
    """Exhaustive cross-check against brute force on a small set."""
    keys = [int(h, 16) for h in [LOW, HIGH, *UNRELATED]]
    tree = BKTree()
    tree.add_many(keys)

    for k in keys:
        expected = {(o, hamming_distance(k, o)) for o in keys if hamming_distance(k, o) <= 8}
        got = set(tree.query(k, 8))
        assert got == expected, k


def test_bktree_incremental_single_query():
    """A single-item query against a pre-built tree works (incremental-add use)."""
    tree = BKTree()
    tree.add_many(int(h, 16) for h in UNRELATED)
    tree.add(int(LOW, 16))
    # New key queried against the existing index finds its Hamming-1 partner
    # only after it too is present — but querying for neighbours of a brand-new
    # key against the existing tree must not crash and must respect the radius.
    assert tree.query(int(HIGH, 16), 1) == [(int(LOW, 16), 1)]


def _make_db(tmp_path):
    db = Database(tmp_path / "photos.db").connect()
    return db


def _insert_jpeg(db, file_id: int, phash: str | None):
    db._conn.execute(
        "INSERT INTO files (file_id, path, filename, extension, file_type, phash) "
        "VALUES (?, ?, ?, ?, 'CAMERA_JPEG', ?)",
        (file_id, f"/img{file_id}.jpg", f"img{file_id}.jpg", "jpg", phash),
    )
    db._conn.commit()


def _insert_hash_trigger(db, file_id: int = 99):
    """A CAMERA_JPEG row with no phash and a bogus path.

    dedup_near early-returns when nothing needs hashing, so we give it one row
    to "hash": opening the nonexistent path fails harmlessly (logged WARN), the
    guard is cleared, and the BK-tree pairing block runs over the rows that DO
    have a phash. No real image or disk read required.
    """
    _insert_jpeg(db, file_id, None)


def test_dedup_near_end_to_end_finds_hamming1_pair(tmp_path):
    """End-to-end: fabricate CAMERA_JPEG rows with known pHashes (no disk reads,
    no real images) and confirm dedup_near records the Hamming-1 near pair the
    old sorted window would have missed."""
    db = _make_db(tmp_path)
    try:
        _insert_jpeg(db, 1, LOW)
        _insert_jpeg(db, 2, HIGH)
        for i, h in enumerate(UNRELATED, start=3):
            _insert_jpeg(db, i, h)
        _insert_hash_trigger(db)

        dedup_near(db, force=True)

        rows = db._conn.execute(
            "SELECT file_id_a, file_id_b, dup_type, hamming_distance FROM duplicates"
        ).fetchall()
        pairs = {(r["file_id_a"], r["file_id_b"]): r["hamming_distance"] for r in rows}

        # The LOW/HIGH pair (file 1 & 2) must be present at Hamming distance 1.
        assert (1, 2) in pairs, pairs
        assert pairs[(1, 2)] == 1
        # All recorded pairs are NEAR type.
        assert all(r["dup_type"] == "NEAR" for r in rows)

        # Phase summary count matches the recorded pairs (no double counting).
        summary = db._conn.execute(
            "SELECT summary_json FROM phases WHERE phase_name='dedup_near'"
        ).fetchone()[0]
        import json

        assert json.loads(summary)["near_dup_pairs"] == len(pairs)
    finally:
        db.close()


def test_dedup_near_identical_phash_grouped(tmp_path):
    """Two files with the SAME pHash → one NEAR pair at Hamming 0 (preserved)."""
    db = _make_db(tmp_path)
    try:
        _insert_jpeg(db, 1, "0f0f0f0f0f0f0f0f")
        _insert_jpeg(db, 2, "0f0f0f0f0f0f0f0f")
        _insert_hash_trigger(db)

        dedup_near(db, force=True)

        rows = db._conn.execute(
            "SELECT file_id_a, file_id_b, hamming_distance FROM duplicates"
        ).fetchall()
        assert len(rows) == 1
        assert (rows[0]["file_id_a"], rows[0]["file_id_b"]) == (1, 2)
        assert rows[0]["hamming_distance"] == 0
    finally:
        db.close()
