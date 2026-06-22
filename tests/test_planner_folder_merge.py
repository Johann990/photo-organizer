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
        keeper_id = _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\img001.jpg", sha=SHA_A)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique, keeper_of = _folder_merge_loser_ids(db)
    assert loser_id in ids
    assert unique == 0
    assert keeper_of[loser_id] == keeper_id


def test_unique_to_loser_not_staged(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\img002.jpg", sha=SHA_C)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique, keeper_of = _folder_merge_loser_ids(db)
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
        ids, unique, keeper_of = _folder_merge_loser_ids(db)
    assert ids == set()
    assert unique == 0


def test_null_sha_not_staged(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\img001.jpg", sha=None)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique, keeper_of = _folder_merge_loser_ids(db)
    assert loser_id not in ids
    assert unique == 1


def test_done_status_skipped(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\img001.jpg", sha=SHA_A, status="done")
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique, keeper_of = _folder_merge_loser_ids(db)
    assert loser_id not in ids
    assert unique == 0


def test_multiple_pairs(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        keeper1 = _add_file(db, r"D:\A1\img001.jpg", sha=SHA_A)
        loser1 = _add_file(db, r"D:\B1\img001.jpg", sha=SHA_A)
        _add_overlap(db, r"D:\A1", r"D:\B1", keeper="a")

        keeper2 = _add_file(db, r"D:\A2\img001.jpg", sha=SHA_B)
        loser2 = _add_file(db, r"D:\B2\img001.jpg", sha=SHA_B)
        loser3 = _add_file(db, r"D:\B2\img002.jpg", sha=SHA_C)  # unique
        _add_overlap(db, r"D:\A2", r"D:\B2", keeper="a")

        ids, unique, keeper_of = _folder_merge_loser_ids(db)
    assert loser1 in ids
    assert loser2 in ids
    assert loser3 not in ids
    assert unique == 1
    assert keeper_of[loser1] == keeper1
    assert keeper_of[loser2] == keeper2


def test_nested_subtree_match(tmp_path):
    # folder_overlaps rows are rolled-up ancestors: the files live in SUBfolders.
    # The keeper SHA and the loser file are both nested below the recorded folder,
    # so the ancestor-walk must reach the rolled-up folder name from each path.
    # Also guards the sibling-prefix case (D:\B vs D:\B2 must not collide).
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_file(db, r"D:\A\Album\2003\img001.jpg", sha=SHA_A)
        loser_id = _add_file(db, r"D:\B\Album\2003\img001.jpg", sha=SHA_A)
        # Sibling folder whose name is a prefix of the loser folder — must NOT match.
        unrelated = _add_file(db, r"D:\B2\img999.jpg", sha=SHA_A)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")
        ids, unique, keeper_of = _folder_merge_loser_ids(db)
    assert loser_id in ids
    assert unrelated not in ids
    assert unique == 0


def test_plan_creates_stage_delete_for_loser(tmp_path):
    from photo_organizer.planner import plan

    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    target.mkdir()

    with Database(db_path) as db:
        # Keeper folder: one file
        keeper_id = _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        # Loser folder: one duplicate (same SHA) + one unique (different SHA)
        loser_dup_id = _add_file(db, r"D:\B\img001.jpg", sha=SHA_A)
        loser_uniq_id = _add_file(db, r"D:\B\img002.jpg", sha=SHA_C)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")

        plan(db, target, assume_yes=True)

        ops = {
            r["file_id"]: r
            for r in db.conn.execute(
                "SELECT file_id, op_type, target_path, stage_reason, dupe_of_file_id "
                "FROM operations"
            )
        }
        assert ops[loser_dup_id]["op_type"] == "STAGE_DELETE"        # duplicate staged
        assert ops.get(loser_uniq_id) is None or ops[loser_uniq_id]["op_type"] != "STAGE_DELETE"
        assert ops[keeper_id]["op_type"] == "MOVE"                   # keeper moved normally
        assert ops[loser_dup_id]["stage_reason"] == "folder_merge_loser"
        assert ops[loser_dup_id]["dupe_of_file_id"] == keeper_id
        assert "folder_merge" in ops[loser_dup_id]["target_path"]
