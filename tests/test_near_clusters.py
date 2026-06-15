"""
Tests for near-duplicate CLUSTER construction (`reviewer._build_near_clusters`).

Pair search (test_near_pairs.py) finds every pHash pair within a threshold; this
module covers the layer above it — turning pending NEAR pairs into review
clusters — and its three noise filters:

  1. EXACT-dupe losers are excluded (the keep_score winner of a sha256 group
     stays; byte-identical losers are already staged by plan).
  2. Junk (non-discriminative) pHashes are excluded — a hash shared by many
     unrelated files (dark/flat images) only ever produces false pairs.
  3. Grouping unions only pairs with hamming ≤ _CLUSTER_HAMMING, so single-
     linkage union-find can't chain unrelated look-alikes into a giant blob.

Run: python -m pytest tests/test_near_clusters.py
"""

from __future__ import annotations

from photo_organizer import reviewer
from photo_organizer.db import Database


def _add_file(db, fid, path, filename, *, w=4000, h=3000, size=5_000_000,
              sha=None, phash=None, camera=None):
    db.conn.execute(
        "INSERT INTO files (file_id, path, filename, extension, file_type, "
        "status, width, height, size_bytes, sha256, phash, camera_model) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, path, filename, "jpg", "CAMERA_JPEG", "scanned", w, h, size,
         sha, phash, camera),
    )


def _add_near_pair(db, a, b, hamming):
    db.conn.execute(
        "INSERT INTO duplicates (file_id_a, file_id_b, dup_type, "
        "hamming_distance, status) VALUES (?,?,?,?,'pending')",
        (min(a, b), max(a, b), "NEAR", hamming),
    )


def _cluster_of(clusters, fid):
    for c in clusters:
        if fid in c:
            return c
    return None


# ---------------------------------------------------------------------------
# Filter 3: tight grouping threshold breaks transitive chaining
# ---------------------------------------------------------------------------

def test_loose_pairs_do_not_chain(tmp_path):
    """A~B~C chained by hamming-5 links must NOT become one cluster at thr=2."""
    with Database(tmp_path / "p.db") as db:
        _add_file(db, 1, "/a/DSC_0001.jpg", "DSC_0001.jpg", phash="0" * 16)
        _add_file(db, 2, "/a/DSC_0002.jpg", "DSC_0002.jpg", phash="1" * 16)
        _add_file(db, 3, "/a/DSC_0003.jpg", "DSC_0003.jpg", phash="2" * 16)
        _add_near_pair(db, 1, 2, hamming=5)
        _add_near_pair(db, 2, 3, hamming=5)
        db.commit()

        clusters, _, _ = reviewer._build_near_clusters(db)
        # Every link exceeds the tight threshold → no cluster survives.
        assert clusters == []


def test_tight_pairs_cluster(tmp_path):
    with Database(tmp_path / "p.db") as db:
        _add_file(db, 1, "/a/DSC_0001.jpg", "DSC_0001.jpg", phash="a" * 16)
        _add_file(db, 2, "/a/DSC_0002.jpg", "DSC_0002.jpg", phash="b" * 16)
        _add_near_pair(db, 1, 2, hamming=2)
        db.commit()

        clusters, _, ch = reviewer._build_near_clusters(db)
        assert len(clusters) == 1
        assert set(clusters[0]) == {1, 2}
        assert ch[clusters[0][0]] == 2


def test_chain_keeps_tight_links_only(tmp_path):
    """1~2 tight (h=1), 2~3 loose (h=5): cluster {1,2}, 3 dropped."""
    with Database(tmp_path / "p.db") as db:
        _add_file(db, 1, "/a/DSC_0001.jpg", "DSC_0001.jpg", phash="c" * 16)
        _add_file(db, 2, "/a/DSC_0002.jpg", "DSC_0002.jpg", phash="d" * 16)
        _add_file(db, 3, "/a/DSC_0003.jpg", "DSC_0003.jpg", phash="e" * 16)
        _add_near_pair(db, 1, 2, hamming=1)
        _add_near_pair(db, 2, 3, hamming=5)
        db.commit()

        clusters, _, _ = reviewer._build_near_clusters(db)
        assert len(clusters) == 1
        assert set(clusters[0]) == {1, 2}


# ---------------------------------------------------------------------------
# Filter 1: EXACT-dupe losers are excluded from near clusters
# ---------------------------------------------------------------------------

def test_exact_loser_excluded(tmp_path):
    """Files 1 & 2 are byte-identical (same sha); only the keeper may cluster.

    The loser (lower resolution) is already staged by plan, so it must not
    appear as a near-cluster candidate even though it has a tight NEAR pair.
    """
    with Database(tmp_path / "p.db") as db:
        # 1 & 2 share sha => exact dupes; 1 is higher-res => keeper.
        _add_file(db, 1, "/a/IMG_1.jpg", "IMG_1.jpg", w=4000, h=3000,
                  sha="deadbeef", phash="1111111111111111")
        _add_file(db, 2, "/b/IMG_1.jpg", "IMG_1.jpg", w=800, h=600,
                  sha="deadbeef", phash="1111111111111112")
        # 3 is a distinct image tightly near the exact-loser 2.
        _add_file(db, 3, "/c/OTHER.jpg", "OTHER.jpg", w=4000, h=3000,
                  sha="cafef00d", phash="1111111111111113")
        _add_near_pair(db, 2, 3, hamming=1)
        _add_near_pair(db, 1, 3, hamming=1)
        db.commit()

        clusters, _, _ = reviewer._build_near_clusters(db)
        c = _cluster_of(clusters, 3)
        assert c is not None
        assert 2 not in c, "exact-loser must be excluded"
        assert set(c) == {1, 3}


# ---------------------------------------------------------------------------
# Filter 2: junk (non-discriminative) pHashes are excluded
# ---------------------------------------------------------------------------

def test_junk_phash_excluded(tmp_path):
    """A pHash shared by >= junk_phash_min files is dropped from clustering."""
    with Database(tmp_path / "p.db") as db:
        junk = "0011223344556677"
        # 8 unrelated files share the junk hash (low-information collision).
        for fid in range(1, 9):
            _add_file(db, fid, f"/a/J{fid}.jpg", f"J{fid}.jpg", phash=junk)
        # Pair two of them tightly — they would cluster if not filtered.
        _add_near_pair(db, 1, 2, hamming=0)
        # A genuine tight pair on discriminative hashes survives.
        _add_file(db, 20, "/b/A.jpg", "A.jpg", phash="aaaaaaaaaaaaaaaa")
        _add_file(db, 21, "/b/B.jpg", "B.jpg", phash="aaaaaaaaaaaaaaab")
        _add_near_pair(db, 20, 21, hamming=1)
        db.commit()

        clusters, _, _ = reviewer._build_near_clusters(db, junk_phash_min=8)
        assert _cluster_of(clusters, 1) is None
        assert _cluster_of(clusters, 2) is None
        survivor = _cluster_of(clusters, 20)
        assert survivor is not None and set(survivor) == {20, 21}


def test_below_junk_threshold_kept(tmp_path):
    """A shared hash UNDER the threshold is a real dupe, not junk — keep it."""
    with Database(tmp_path / "p.db") as db:
        shared = "0011223344556677"
        _add_file(db, 1, "/a/A.jpg", "A.jpg", phash=shared)
        _add_file(db, 2, "/a/B.jpg", "B.jpg", phash=shared)
        _add_near_pair(db, 1, 2, hamming=0)
        db.commit()

        clusters, _, _ = reviewer._build_near_clusters(db, junk_phash_min=8)
        assert len(clusters) == 1
        assert set(clusters[0]) == {1, 2}
