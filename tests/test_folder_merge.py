from __future__ import annotations

from photo_organizer.db import Database


def test_folder_overlaps_table_and_helpers(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        db.insert_folder_overlap(
            folder_a="D:\\A", folder_b="D:\\B",
            shared_count=10, a_only_count=0, b_only_count=2,
            coverage_a=1.0, coverage_b=0.83, keeper="b",
        )
        db.commit()
        rows = list(db.iter_folder_overlaps())
        assert len(rows) == 1
        r = rows[0]
        assert r["folder_a"] == "D:\\A" and r["folder_b"] == "D:\\B"
        assert r["shared_count"] == 10 and r["keeper"] == "b"
        assert r["status"] == "pending"

        db.clear_folder_overlaps()
        db.commit()
        assert list(db.iter_folder_overlaps()) == []


def test_compute_folder_overlaps_twin_and_keeper(tmp_path):
    from photo_organizer.folder_merge import compute_folder_overlaps
    from photo_organizer.db import Database

    db_path = tmp_path / ".photo_organizer" / "library.db"
    # Two SIBLING leaf folders under one scan root with the SAME 6 shas → one
    # twin pair (mirrors the real library, where copies are deep siblings, not
    # nested directly under the scan root). The 'backup' side is folded.
    def add(db, path, sha):
        db.conn.execute(
            "INSERT INTO files (path, filename, extension, file_type, status, sha256) "
            "VALUES (?,?,?,?, 'hashed', ?)",
            (path, path.split("\\")[-1], "jpg", "CAMERA_JPEG", sha),
        )
    with Database(db_path) as db:
        for i in range(6):
            add(db, f"D:\\Lib\\Trip\\IMG_{i}.jpg", f"sha{i}")
            add(db, f"D:\\Lib\\Backup_Trip\\IMG_{i}.jpg", f"sha{i}")
        db.commit()

        overlaps = compute_folder_overlaps(db, ["D:\\Lib"], coverage=0.95, min_shared=3)

        assert len(overlaps) == 1
        o = overlaps[0]
        assert {o["folder_a"], o["folder_b"]} == {"D:\\Lib\\Trip", "D:\\Lib\\Backup_Trip"}
        assert o["shared_count"] == 6
        assert o["coverage_a"] == 1.0 and o["coverage_b"] == 1.0
        # keeper is the side NOT marked as a backup copy.
        keeper_folder = o["folder_a"] if o["keeper"] == "a" else o["folder_b"]
        assert keeper_folder == "D:\\Lib\\Trip"


def test_compute_folder_overlaps_non_twin_not_flagged(tmp_path):
    from photo_organizer.folder_merge import compute_folder_overlaps
    from photo_organizer.db import Database

    db_path = tmp_path / ".photo_organizer" / "library.db"
    def add(db, path, sha):
        db.conn.execute(
            "INSERT INTO files (path, filename, extension, file_type, status, sha256) "
            "VALUES (?,?,?,?, 'hashed', ?)",
            (path, path.split("\\")[-1], "jpg", "CAMERA_JPEG", sha),
        )
    with Database(db_path) as db:
        # A has 10 unique, B shares only 2 of them → min-coverage low → not a twin.
        for i in range(10):
            add(db, f"D:\\Lib\\A\\a{i}.jpg", f"s{i}")
        add(db, "D:\\Lib\\B\\b0.jpg", "s0")
        add(db, "D:\\Lib\\B\\b1.jpg", "s1")
        db.commit()
        overlaps = compute_folder_overlaps(db, ["D:\\Lib"], coverage=0.95, min_shared=2)
        assert overlaps == []


def test_detect_and_store_persists_overlaps(tmp_path):
    from photo_organizer.folder_merge import detect_and_store
    from photo_organizer.db import Database

    db_path = tmp_path / ".photo_organizer" / "library.db"
    def add(db, path, sha):
        db.conn.execute(
            "INSERT INTO files (path, filename, extension, file_type, status, sha256) "
            "VALUES (?,?,?,?, 'hashed', ?)",
            (path, path.split("\\")[-1], "jpg", "CAMERA_JPEG", sha),
        )
    with Database(db_path) as db:
        for i in range(6):
            add(db, f"D:\\Lib\\Trip\\IMG_{i}.jpg", f"sha{i}")
            add(db, f"D:\\Lib\\Backup_Trip\\IMG_{i}.jpg", f"sha{i}")
        db.commit()

        n = detect_and_store(db, ["D:\\Lib"], coverage=0.95, min_shared=3)
        assert n == 1
        rows = list(db.iter_folder_overlaps())
        assert len(rows) == 1 and rows[0]["shared_count"] == 6

        # Idempotent: a second run clears then re-inserts, same count.
        n2 = detect_and_store(db, ["D:\\Lib"], coverage=0.95, min_shared=3)
        assert n2 == 1 and len(list(db.iter_folder_overlaps())) == 1


def test_cli_folder_merge_wired():
    import photo_organizer.__main__ as m
    parser = m.build_parser()
    args = parser.parse_args(["folder-merge", "--db", "x.db"])
    assert args.func is m.cmd_folder_merge
