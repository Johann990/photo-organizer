"""
Tests for Layer 2 — the incremental `add` command (photo_organizer.adder).

Seeds a temp library with 'done'-state organized files in an existing event
folder, then plans additions for NEW files and asserts the critical invariant:
  * a byte-identical duplicate of an existing file is recorded + staged, NOT
    placed a second time;
  * a brand-new photo whose source folder / date matches an existing event is
    planned INTO that event folder using its EXISTING name;
  * a brand-new photo on a new date gets a NEW event folder;
  * NO operation moves or renames any pre-existing 'done' file, and existing
    event-folder names are unchanged;
  * the near-dupe step finds a near-dup of a new file against the existing index.
"""

from __future__ import annotations

from pathlib import Path

from photo_organizer.adder import plan_additions
from photo_organizer.db import Database

KNOWN_CAMERA = "TestCam"
EVENT_DIRNAME = "2023-06-15_3d_Kyoto"  # multi-day → range 2023-06-15 … 2023-06-17


def _add_file(
    db, path, *, file_type="CAMERA_JPEG", status="hashed",
    sha256=None, phash=None, camera_model=KNOWN_CAMERA,
    datetime_original=None, filename=None, width=4000, height=3000,
    size_bytes=1000,
) -> int:
    p = Path(path)
    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status, "
        "sha256, phash, camera_model, datetime_original, width, height, size_bytes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(p), filename or p.name, p.suffix.lstrip(".").lower(), file_type,
         status, sha256, phash, camera_model, datetime_original,
         width, height, size_bytes),
    )
    return cur.lastrowid


def _add_done_move(db, file_id, source_path):
    """Mark a file as already organized: a 'done' MOVE op + status='done'."""
    db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, target_path, status) "
        "VALUES (?, 'MOVE', ?, ?, 'done')",
        (file_id, str(source_path), None),
    )
    db.conn.execute("UPDATE files SET status='done' WHERE file_id=?", (file_id,))


def _seed_library(tmp_path):
    """A library with one materialized multi-day event (two done files)."""
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Library"
    src = tmp_path / "src"          # original (already-organized) source
    event_dir = target / "Masters" / "2023" / EVENT_DIRNAME

    with Database(db_path) as db:
        db.add_known_camera("Test", KNOWN_CAMERA)
        # Two done files originally from src/Kyoto, now living in the event dir.
        a = _add_file(db, event_dir / "IMG_001.jpg", status="done",
                      sha256="aaaa", datetime_original="2023:06:15 10:00:00")
        _add_done_move(db, a, src / "Kyoto" / "IMG_001.jpg")
        b = _add_file(db, event_dir / "IMG_002.jpg", status="done",
                      sha256="bbbb", datetime_original="2023:06:17 10:00:00")
        _add_done_move(db, b, src / "Kyoto" / "IMG_002.jpg")
        db.commit()
    return db_path, target, src, event_dir, {"a": a, "b": b}


def _ops_for(db, file_id):
    return db.conn.execute(
        "SELECT op_type, target_path, status FROM operations WHERE file_id=?",
        (file_id,),
    ).fetchall()


def test_add_places_new_and_freezes_existing(tmp_path):
    db_path, target, src, event_dir, done = _seed_library(tmp_path)
    newsrc = tmp_path / "newsrc"

    with Database(db_path) as db:
        # (a) byte-identical duplicate of done file "a" (same sha256).
        dup = _add_file(db, newsrc / "Kyoto2" / "dup.jpg", sha256="aaaa",
                        datetime_original="2023:06:16 09:00:00")
        # (b) brand-new photo dated WITHIN the event range, different folder.
        within = _add_file(db, newsrc / "Kyoto2" / "new_within.jpg", sha256="cccc",
                           datetime_original="2023:06:16 12:00:00")
        # (c) brand-new photo on a NEW date → must get a new event folder.
        newdate = _add_file(db, newsrc / "Kyoto2" / "new_sep.jpg", sha256="dddd",
                            datetime_original="2023:09:01 12:00:00")
        # (d) brand-new photo in the SAME source folder as the event (lineage).
        lineage = _add_file(db, src / "Kyoto" / "IMG_999.jpg", sha256="eeee",
                            datetime_original="2024:01:01 12:00:00")
        db.commit()

        summary = plan_additions(db, target)

        # (a) duplicate: staged, recorded, NOT moved.
        a_ops = _ops_for(db, dup)
        assert [o["op_type"] for o in a_ops] == ["STAGE_DELETE"]
        assert summary["exact_dup"] == 1
        dup_rows = db.conn.execute(
            "SELECT COUNT(*) FROM duplicates WHERE dup_type='EXACT' "
            "AND (file_id_a=? OR file_id_b=?)", (dup, dup),
        ).fetchone()[0]
        assert dup_rows == 1

        # (b) within-range → into the EXISTING event folder, frozen name.
        b_ops = _ops_for(db, within)
        assert len(b_ops) == 1 and b_ops[0]["op_type"] == "MOVE"
        assert Path(b_ops[0]["target_path"]).parent == event_dir

        # (c) new date → a NEW event folder (not the existing one).
        c_ops = _ops_for(db, newdate)
        assert len(c_ops) == 1 and c_ops[0]["op_type"] == "MOVE"
        c_parent = Path(c_ops[0]["target_path"]).parent
        assert c_parent != event_dir
        assert c_parent.name.startswith("2023-09-01")

        # (d) same source lineage → into the EXISTING event folder.
        d_ops = _ops_for(db, lineage)
        assert len(d_ops) == 1 and d_ops[0]["op_type"] == "MOVE"
        assert Path(d_ops[0]["target_path"]).parent == event_dir

        # ── Invariant: no op touches a pre-existing 'done' file ───────────────
        for fid in done.values():
            ops = db.conn.execute(
                "SELECT status FROM operations WHERE file_id=?", (fid,)
            ).fetchall()
            # Only the original 'done' MOVE op, still done — never re-planned.
            assert [o["status"] for o in ops] == ["done"]

        # No new op may target a location currently occupied by a done file.
        done_paths = {
            db.conn.execute("SELECT path FROM files WHERE file_id=?", (fid,)).fetchone()["path"]
            for fid in done.values()
        }
        planned_targets = {
            r["target_path"]
            for r in db.conn.execute(
                "SELECT target_path FROM operations WHERE status='planned'"
            ).fetchall()
        }
        assert planned_targets.isdisjoint(done_paths)

        # Existing event-folder name unchanged (done files never re-pathed).
        for fid in done.values():
            path = db.conn.execute(
                "SELECT path FROM files WHERE file_id=?", (fid,)
            ).fetchone()["path"]
            assert Path(path).parent == event_dir


def test_add_near_dupe_against_existing_index(tmp_path):
    """A new image near-duplicate of an existing 'done' image is recorded
    (via the BKTree-preferred index) and still placed (not staged)."""
    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Library"
    event_dir = target / "Masters" / "2023" / EVENT_DIRNAME

    with Database(db_path) as db:
        db.add_known_camera("Test", KNOWN_CAMERA)
        # Existing organized image with a known pHash.
        existing = _add_file(db, event_dir / "IMG_001.jpg", status="done",
                             sha256="aaaa", phash="0000000000000000",
                             datetime_original="2023:06:15 10:00:00")
        _add_done_move(db, existing, tmp_path / "src" / "Kyoto" / "IMG_001.jpg")
        db.commit()

        # New image, pHash differs by ONE bit (Hamming 1) → near-dup, unique sha.
        newimg = _add_file(db, tmp_path / "newsrc" / "near.jpg", sha256="ffff",
                           phash="0000000000000001",
                           datetime_original="2023:08:01 12:00:00")
        db.commit()

        summary = plan_additions(db, target, hamming_threshold=8)

        assert summary["near_dup"] >= 1
        near_rows = db.conn.execute(
            "SELECT COUNT(*) FROM duplicates WHERE dup_type='NEAR' "
            "AND (file_id_a=? OR file_id_b=?)", (newimg, newimg),
        ).fetchone()[0]
        assert near_rows == 1

        # Near-dupes are NOT auto-staged — the new file is still placed (MOVE).
        ops = _ops_for(db, newimg)
        assert [o["op_type"] for o in ops] == ["MOVE"]


def test_near_index_fallback_without_bktree(monkeypatch):
    """The near-dupe query must work even if photo_organizer.bktree is absent
    (the BKTree task may not have merged). Force the import to fail and confirm
    the brute-force fallback returns the same neighbours."""
    import sys

    from photo_organizer.adder import _query_near_index

    index = {0x0000000000000000: [1], 0x0000000000000001: [2],
             0xFFFFFFFFFFFFFFFF: [3]}
    # With BKTree present, the query finds the two close keys (Hamming ≤ 8).
    near = {k for k, _ in _query_near_index(index, 0x0, 8)}
    assert near == {0x0, 0x1}

    # Now make `from .bktree import BKTree` raise ImportError → fallback path.
    monkeypatch.setitem(sys.modules, "photo_organizer.bktree", None)
    near_fb = {k for k, _ in _query_near_index(index, 0x0, 8)}
    assert near_fb == {0x0, 0x1}


def test_add_no_new_files_is_noop(tmp_path):
    db_path, target, *_ = _seed_library(tmp_path)
    with Database(db_path) as db:
        summary = plan_additions(db, target)
        assert summary["new"] == 0
        assert summary["moved"] == 0
        planned = db.conn.execute(
            "SELECT COUNT(*) FROM operations WHERE status='planned'"
        ).fetchone()[0]
        assert planned == 0
