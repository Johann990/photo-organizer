"""Tests for _folder_merge_loser_ids in planner.py."""
from __future__ import annotations
from photo_organizer.db import Database
from photo_organizer.planner import _folder_merge_loser_ids

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64  # unique to loser


def _add_file(db, path, sha=None, status="scanned", file_type="CAMERA_JPEG"):
    filename = path.split("\\")[-1]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    db.conn.execute(
        "INSERT INTO files (path, filename, extension, sha256, status, file_type, size_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, 1000)",
        (path, filename, ext, sha, status, file_type),
    )
    db.commit()
    return db.conn.execute(
        "SELECT file_id FROM files WHERE path=?", (path,)
    ).fetchone()[0]


def _add_overlap(db, folder_a, folder_b, keeper):
    db.insert_folder_overlap(
        folder_a=folder_a, folder_b=folder_b,
        shared_count=5, a_only_count=0, b_only_count=1,
        coverage_a=1.0, coverage_b=0.833, keeper=keeper,
    )
    db.conn.execute(
        "UPDATE folder_overlaps SET status='reviewed' "
        "WHERE folder_a=? AND folder_b=?", (folder_a, folder_b),
    )
    db.commit()


def test_loser_staged_when_sha_in_keeper(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\img001.jpg", sha=SHA_A)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique = _folder_merge_loser_ids(db)
    assert loser_id in ids
    assert unique == 0


def test_unique_to_loser_not_staged(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\img002.jpg", sha=SHA_C)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique = _folder_merge_loser_ids(db)
    assert loser_id not in ids
    assert unique == 1


def test_both_keeper_skipped(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        _add_file(db, r"D:\B\img001.jpg", sha=SHA_A)
        # keeper=None means "keep both" — insert_folder_overlap allows None keeper
        db.insert_folder_overlap(
            folder_a=r"D:\A", folder_b=r"D:\B",
            shared_count=5, a_only_count=0, b_only_count=0,
            coverage_a=1.0, coverage_b=1.0, keeper=None,
        )
        db.conn.execute(
            "UPDATE folder_overlaps SET status='reviewed' WHERE folder_a=? AND folder_b=?",
            (r"D:\A", r"D:\B"),
        )
        db.commit()
        ids, unique = _folder_merge_loser_ids(db)
    assert ids == set()
    assert unique == 0


def test_null_sha_not_staged(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\img001.jpg", sha=None)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique = _folder_merge_loser_ids(db)
    assert loser_id not in ids
    assert unique == 1


def test_done_status_skipped(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\img001.jpg", sha=SHA_A, status="done")
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique = _folder_merge_loser_ids(db)
    assert loser_id not in ids
    assert unique == 0


def test_multiple_pairs(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A1\img001.jpg", sha=SHA_A)
        loser1 = _add_file(db, r"D:\B1\img001.jpg", sha=SHA_A)
        _add_overlap(db, r"D:\A1", r"D:\B1", keeper="a")

        _add_file(db, r"D:\A2\img001.jpg", sha=SHA_B)
        loser2 = _add_file(db, r"D:\B2\img001.jpg", sha=SHA_B)
        loser3 = _add_file(db, r"D:\B2\img002.jpg", sha=SHA_C)  # unique
        _add_overlap(db, r"D:\A2", r"D:\B2", keeper="a")

        ids, unique = _folder_merge_loser_ids(db)
    assert loser1 in ids
    assert loser2 in ids
    assert loser3 not in ids
    assert unique == 1
