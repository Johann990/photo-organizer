"""
Tests for the HTML contact-sheet review front-end (photo_organizer.webreview).

The web mode is ONLY a new human front-end: it must reuse the existing
decision-recording seam (`reviewer._record_decision`) unchanged and never
auto-delete.  These tests cover the pure helpers (sharpness proxy, within-
cluster soft-focus flagging, default selection per cluster type), the thumbnail
cache, and the decision endpoint writing `status='reviewed'` + `keep_file_id`.

Run: python -m pytest tests/test_webreview.py
"""

from __future__ import annotations

import json
import urllib.request

from PIL import Image, ImageDraw, ImageFilter

from photo_organizer.db import Database
from photo_organizer.webreview import (
    ReviewState,
    ThumbCache,
    apply_decision,
    default_selection,
    flag_soft_focus,
    record_selection,
    serve,
    sharpness,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _striped_image(size: int = 256) -> Image.Image:
    """A high-frequency black/white striped image (very sharp edges)."""
    img = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(img)
    for x in range(0, size, 4):
        d.rectangle([x, 0, x + 1, size], fill="black")
    return img


def _meta(fid, *, path, filename, w, h, size, camera=None):
    return {
        "file_id": fid,
        "path": path,
        "filename": filename,
        "width": w,
        "height": h,
        "size_bytes": size,
        "camera_model": camera,
        "datetime_original": None,
    }


def _add_file(db, fid, path, filename, *, w=4000, h=3000, size=5_000_000):
    db.conn.execute(
        "INSERT INTO files (file_id, path, filename, extension, file_type, "
        "status, width, height, size_bytes) VALUES (?,?,?,?,?,?,?,?,?)",
        (fid, path, filename, "jpg", "CAMERA_JPEG", "scanned", w, h, size),
    )


def _add_near_pair(db, a, b, hamming=3):
    db.conn.execute(
        "INSERT INTO duplicates (file_id_a, file_id_b, dup_type, hamming_distance, "
        "status) VALUES (?,?,?,?,'pending')",
        (min(a, b), max(a, b), "NEAR", hamming),
    )


# ---------------------------------------------------------------------------
# 1. Sharpness proxy — a blurred image must score lower than a sharp one
# ---------------------------------------------------------------------------

def test_sharpness_blurred_lower_than_sharp():
    sharp = _striped_image()
    blurred = sharp.filter(ImageFilter.GaussianBlur(radius=4))
    assert sharpness(sharp) > sharpness(blurred)


def test_sharpness_returns_float():
    assert isinstance(sharpness(_striped_image()), float)


# ---------------------------------------------------------------------------
# 2. Within-cluster RELATIVE soft-focus flagging
# ---------------------------------------------------------------------------

def test_flag_soft_focus_relative_to_cluster_max():
    # id 3 is far below the sharpest (100); ids 1,2 are in the top band.
    scores = {1: 100.0, 2: 95.0, 3: 20.0}
    assert flag_soft_focus(scores) == {3}


def test_flag_soft_focus_none_when_all_similar():
    scores = {1: 100.0, 2: 98.0, 3: 97.0}
    assert flag_soft_focus(scores) == set()


def test_flag_soft_focus_empty_input():
    assert flag_soft_focus({}) == set()


# ---------------------------------------------------------------------------
# 3. Default selection per cluster type
# ---------------------------------------------------------------------------

def test_default_selection_resized_keeps_highest_resolution():
    # Same filename stem at two resolutions → copy/resized cluster.
    meta = {
        1: _meta(1, path="/a/IMG_001.jpg", filename="IMG_001.jpg",
                 w=4000, h=3000, size=5_000_000),
        2: _meta(2, path="/b/IMG_001.jpg", filename="IMG_001.jpg",
                 w=800, h=600, size=200_000),
    }
    kept, dropped = default_selection([1, 2], meta, {1: 50.0, 2: 50.0}, set())
    assert kept == {1}
    assert dropped == {2}


def test_default_selection_burst_keeps_sharp_drops_soft():
    # Distinct filenames → burst cluster; selection driven by sharpness.
    meta = {
        1: _meta(1, path="/a/DSC_0001.jpg", filename="DSC_0001.jpg",
                 w=4000, h=3000, size=5_000_000),
        2: _meta(2, path="/a/DSC_0002.jpg", filename="DSC_0002.jpg",
                 w=4000, h=3000, size=5_000_000),
        3: _meta(3, path="/a/DSC_0003.jpg", filename="DSC_0003.jpg",
                 w=4000, h=3000, size=5_000_000),
    }
    scores = {1: 100.0, 2: 96.0, 3: 18.0}
    kept, dropped = default_selection([1, 2, 3], meta, scores, set())
    assert kept == {1, 2}
    assert dropped == {3}


def test_default_selection_burst_all_sharp_keeps_all():
    meta = {
        1: _meta(1, path="/a/DSC_0001.jpg", filename="DSC_0001.jpg",
                 w=4000, h=3000, size=5_000_000),
        2: _meta(2, path="/a/DSC_0002.jpg", filename="DSC_0002.jpg",
                 w=4000, h=3000, size=5_000_000),
    }
    scores = {1: 100.0, 2: 98.0}
    kept, dropped = default_selection([1, 2], meta, scores, set())
    assert kept == {1, 2}
    assert dropped == set()


# ---------------------------------------------------------------------------
# 4. Decision endpoint writes status='reviewed' + keep_file_id via _record_decision
# ---------------------------------------------------------------------------

def test_apply_decision_records_single_keeper(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        _add_file(db, 1, "/a/IMG_001.jpg", "IMG_001.jpg", w=4000, h=3000)
        _add_file(db, 2, "/b/IMG_001.jpg", "IMG_001.jpg", w=800, h=600)
        _add_near_pair(db, 1, 2, hamming=4)
        db.commit()

        apply_decision(db, [1, 2], kept={1}, dropped={2}, hamming=4)

        row = db.conn.execute(
            "SELECT status, keep_file_id FROM duplicates "
            "WHERE file_id_a=1 AND file_id_b=2"
        ).fetchone()
        assert row["status"] == "reviewed"
        assert row["keep_file_id"] == 1


def test_apply_decision_keep_all_records_null_keeper(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        _add_file(db, 1, "/a/DSC_0001.jpg", "DSC_0001.jpg")
        _add_file(db, 2, "/a/DSC_0002.jpg", "DSC_0002.jpg")
        _add_near_pair(db, 1, 2)
        db.commit()

        apply_decision(db, [1, 2], kept={1, 2}, dropped=set(), hamming=3)

        row = db.conn.execute(
            "SELECT status, keep_file_id FROM duplicates "
            "WHERE file_id_a=1 AND file_id_b=2"
        ).fetchone()
        assert row["status"] == "reviewed"
        assert row["keep_file_id"] is None


def test_record_selection_multi_keep_stages_only_dropped(tmp_path):
    # Burst cluster {1,2,3}: keep the two sharp frames, drop the soft one.
    # plan derives losers as the non-keep endpoint of every reviewed NEAR pair;
    # only file 3 may ever appear as a loser.
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        _add_file(db, 1, "/a/DSC_0001.jpg", "DSC_0001.jpg")
        _add_file(db, 2, "/a/DSC_0002.jpg", "DSC_0002.jpg")
        _add_file(db, 3, "/a/DSC_0003.jpg", "DSC_0003.jpg")
        _add_near_pair(db, 1, 2)
        _add_near_pair(db, 2, 3)
        _add_near_pair(db, 1, 3)
        db.commit()

        record_selection(db, [1, 2, 3], kept={1, 2}, dropped={3}, hamming=3)
        db.commit()

        losers = set()
        for r in db.conn.execute(
            "SELECT file_id_a, file_id_b, keep_file_id FROM duplicates "
            "WHERE dup_type='NEAR' AND status='reviewed' "
            "AND keep_file_id IS NOT NULL"
        ).fetchall():
            losers.add(r["file_id_a"] if r["keep_file_id"] == r["file_id_b"]
                       else r["file_id_b"])
        assert losers == {3}
        # No pending pairs left — the whole cluster is resolved.
        pend = db.conn.execute(
            "SELECT COUNT(*) FROM duplicates WHERE status='pending'"
        ).fetchone()[0]
        assert pend == 0


# ---------------------------------------------------------------------------
# 5. Thumbnail cache — second request for same file_id does not regenerate
# ---------------------------------------------------------------------------

def test_thumbcache_caches_by_file_id(tmp_path):
    src = tmp_path / "orig.jpg"
    _striped_image(512).save(src)

    cache = ThumbCache(tmp_path / ".thumbs")
    path1, sharp1 = cache.ensure(7, src)
    assert path1.exists()
    assert cache.generations == 1

    path2, sharp2 = cache.ensure(7, src)
    assert path2 == path1
    assert sharp2 == sharp1
    assert cache.generations == 1  # served from cache, not regenerated


def test_thumbcache_thumbnail_is_bounded(tmp_path):
    src = tmp_path / "orig.jpg"
    _striped_image(2000).save(src)
    cache = ThumbCache(tmp_path / ".thumbs", size=256)
    path, _ = cache.ensure(1, src)
    with Image.open(path) as t:
        assert max(t.size) <= 256


# ---------------------------------------------------------------------------
# 6. Live server round-trip — POST /decision drives the real handler to the DB
# ---------------------------------------------------------------------------

def test_serve_decision_endpoint_round_trip(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        _add_file(db, 1, "/a/IMG_001.jpg", "IMG_001.jpg", w=4000, h=3000)
        _add_file(db, 2, "/b/IMG_001.jpg", "IMG_001.jpg", w=800, h=600)
        # hamming must be within _CLUSTER_HAMMING so the pair forms a cluster.
        _add_near_pair(db, 1, 2, hamming=2)
        db.commit()

        server = serve(db, review_all=True, port=0, open_browser=False,
                       background=True)
        try:
            host, port = server.server_address
            url = f"http://127.0.0.1:{port}/decision"
            body = json.dumps(
                {"cluster": 0, "kept": [1], "dropped": [2]}
            ).encode()
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200
        finally:
            server.shutdown()

        row = db.conn.execute(
            "SELECT status, keep_file_id FROM duplicates "
            "WHERE file_id_a=1 AND file_id_b=2"
        ).fetchone()
        assert row["status"] == "reviewed"
        assert row["keep_file_id"] == 1


def test_serve_undecision_reopens_cluster(tmp_path):
    """POST /undecision reverts a saved decision back to pending (un-save)."""
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        _add_file(db, 1, "/a/IMG_001.jpg", "IMG_001.jpg", w=4000, h=3000)
        _add_file(db, 2, "/b/IMG_001.jpg", "IMG_001.jpg", w=800, h=600)
        _add_near_pair(db, 1, 2, hamming=2)
        db.commit()

        server = serve(db, review_all=True, port=0, open_browser=False,
                       background=True)
        try:
            host, port = server.server_address

            def post(path, payload):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                return urllib.request.urlopen(req, timeout=5).status

            assert post("/decision", {"cluster": 0, "kept": [1], "dropped": [2]}) == 200
            row = db.conn.execute(
                "SELECT status FROM duplicates WHERE file_id_a=1 AND file_id_b=2"
            ).fetchone()
            assert row["status"] == "reviewed"

            # Un-save: the pair returns to pending and keep_file_id clears.
            assert post("/undecision", {"cluster": 0}) == 200
            row = db.conn.execute(
                "SELECT status, keep_file_id, resolved_at FROM duplicates "
                "WHERE file_id_a=1 AND file_id_b=2"
            ).fetchone()
            assert row["status"] == "pending"
            assert row["keep_file_id"] is None
            assert row["resolved_at"] is None
        finally:
            server.shutdown()


def test_serve_decision_all_saves_every_cluster(tmp_path):
    """POST /decision-all records every cluster's selection in one request."""
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        # Two independent same-name clusters: {1,2} and {3,4}.
        _add_file(db, 1, "/a/IMG_1.jpg", "IMG_1.jpg", w=4000, h=3000)
        _add_file(db, 2, "/b/IMG_1.jpg", "IMG_1.jpg", w=800, h=600)
        _add_file(db, 3, "/a/IMG_2.jpg", "IMG_2.jpg", w=4000, h=3000)
        _add_file(db, 4, "/b/IMG_2.jpg", "IMG_2.jpg", w=800, h=600)
        _add_near_pair(db, 1, 2, hamming=2)
        _add_near_pair(db, 3, 4, hamming=2)
        db.commit()

        # Mirror the server's cluster ordering to address them by index.
        state = ReviewState(db, review_all=True, cache_dir=tmp_path / ".thumbs")
        decisions = []
        for idx, members in enumerate(state.clusters):
            keeper = max(members)  # the 4000px copy has the higher file_id here
            decisions.append({
                "cluster": idx,
                "kept": [keeper],
                "dropped": [m for m in members if m != keeper],
            })
        assert len(decisions) == 2

        server = serve(db, review_all=True, port=0, open_browser=False,
                       background=True)
        try:
            host, port = server.server_address
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/decision-all",
                data=json.dumps({"decisions": decisions}).encode(),
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200
        finally:
            server.shutdown()

        # Both clusters' pairs are now reviewed; nothing left pending.
        reviewed = db.conn.execute(
            "SELECT COUNT(*) FROM duplicates WHERE status='reviewed'"
        ).fetchone()[0]
        pending = db.conn.execute(
            "SELECT COUNT(*) FROM duplicates WHERE status='pending'"
        ).fetchone()[0]
        assert reviewed == 2
        assert pending == 0
