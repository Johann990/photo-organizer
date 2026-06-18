"""
Tests for the `relocate` command (photo_organizer.relocate).

A moved file keeps its bytes, so its SHA-256 is unchanged. `relocate` finds DB
rows whose path no longer exists on disk, discovers on-disk files not yet in the
DB, hashes only those, and re-points stale rows to their new location by
identical sha256 — updating only files.path/mtime so file_id and every
organizing decision stay intact. Rows with no match are logged LOST.
"""

from __future__ import annotations

from pathlib import Path

from photo_organizer.db import Database
from photo_organizer.relocate import find_stale_rows, relocate


def _add(db, path, *, sha256, status="hashed", filename=None, mtime=None):
    p = Path(path)
    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status, "
        "sha256, mtime) VALUES (?,?,?,?,?,?,?)",
        (str(p), filename or p.name, p.suffix.lstrip(".").lower(),
         "CAMERA_JPEG", status, sha256, mtime),
    )
    return cur.lastrowid


def test_find_stale_rows_flags_only_missing_paths(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    present = tmp_path / "here.jpg"
    present.write_bytes(b"x")
    with Database(db_path) as db:
        gone = _add(db, tmp_path / "gone.jpg", sha256="aaaa")
        _add(db, present, sha256="bbbb")
        db.commit()

        stale = find_stale_rows(db)
        assert [r["file_id"] for r in stale] == [gone]


def test_match_rows_to_paths_prefers_filename_then_order():
    from photo_organizer.relocate import _match_rows_to_paths
    rows = [{"file_id": 1, "filename": "IMG_1.jpg"},
            {"file_id": 2, "filename": "IMG_2.jpg"}]
    cands = [Path("D:/new/IMG_2.jpg"), Path("D:/new/IMG_1.jpg")]
    matched = _match_rows_to_paths(rows, cands)
    by_id = {row["file_id"]: p.name for row, p in matched}
    assert by_id == {1: "IMG_1.jpg", 2: "IMG_2.jpg"}


def test_match_rows_to_paths_falls_back_to_order_when_names_differ():
    from photo_organizer.relocate import _match_rows_to_paths

    rows = [{"file_id": 1, "filename": "old_a.jpg"},
            {"file_id": 2, "filename": "old_b.jpg"}]
    cands = [Path("D:/new/z.jpg"), Path("D:/new/a.jpg")]
    matched = _match_rows_to_paths(rows, cands)
    # 2 rows, 2 candidates → both matched (order-based), none dropped
    assert len(matched) == 2
    assert {str(p) for _, p in matched} == {str(c) for c in cands}


def test_relocate_repoints_moved_file_and_preserves_decisions(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    scan_root = tmp_path / "lib"
    old_dir = scan_root / "OldFolder"
    new_dir = scan_root / "MovedFolder"
    new_dir.mkdir(parents=True)

    # The file physically lives at its NEW location already (user moved it),
    # but the DB still records the OLD path.
    content = b"hello-photo-bytes"
    import hashlib
    sha = hashlib.sha256(content).hexdigest()
    (new_dir / "IMG_1.jpg").write_bytes(content)

    with Database(db_path) as db:
        fid = _add(db, old_dir / "IMG_1.jpg", sha256=sha)
        # A decision attached to this file_id (must survive relocate).
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, status) "
            "VALUES (?, 'MOVE', ?, 'planned')",
            (fid, str(old_dir / "IMG_1.jpg")),
        )
        db.commit()

        summary = relocate(db, [scan_root])

        assert summary == {"stale": 1, "relocated": 1, "lost": 0, "pruned": 0}
        row = db.conn.execute(
            "SELECT path FROM files WHERE file_id = ?", (fid,)
        ).fetchone()
        assert row["path"] == str(new_dir / "IMG_1.jpg")
        # The decision row is still attached to the SAME file_id.
        op = db.conn.execute(
            "SELECT op_type FROM operations WHERE file_id = ?", (fid,)
        ).fetchone()
        assert op["op_type"] == "MOVE"


def test_relocate_logs_lost_when_no_sha_match(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    scan_root = tmp_path / "lib"
    scan_root.mkdir(parents=True)
    with Database(db_path) as db:
        fid = _add(db, scan_root / "deleted.jpg", sha256="deadbeef")
        db.commit()

        summary = relocate(db, [scan_root])
        assert summary == {"stale": 1, "relocated": 0, "lost": 1, "pruned": 0}
        log = db.conn.execute(
            "SELECT message FROM run_log WHERE phase='relocate' "
            "AND file_id = ?", (fid,)
        ).fetchone()
        assert log is not None and "LOST" in log["message"]


def test_cli_relocate_command_is_wired():
    import photo_organizer.__main__ as m

    parser = m.build_parser() if hasattr(m, "build_parser") else m._build_parser()
    args = parser.parse_args(["relocate", "--db", "x.db"])
    assert args.func is m.cmd_relocate


def test_prune_rows_deletes_orphan_and_dependents_keeps_done(tmp_path):
    from photo_organizer.relocate import _prune_rows

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        # Orphan source row (status hashed) + a survivor row.
        orphan = _add(db, tmp_path / "gone" / "a.jpg", sha256="aaaa")
        survivor = _add(db, tmp_path / "keep" / "b.jpg", sha256="bbbb")
        # A done row whose path is also gone — must NOT be pruned.
        done = _add(db, tmp_path / "gone" / "c.jpg", sha256="cccc", status="done")
        # Dependents referencing the orphan.
        db.conn.execute(
            "INSERT INTO duplicates (file_id_a, file_id_b, dup_type) VALUES (?,?,'EXACT')",
            (orphan, survivor),
        )
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, status) "
            "VALUES (?, 'MOVE', ?, 'planned')",
            (orphan, str(tmp_path / "gone" / "a.jpg")),
        )
        db.conn.execute(
            "INSERT INTO run_log (level, phase, file_id, path, message, logged_at) "
            "VALUES ('WARN','relocate',?,?,'LOST x','2026-01-01T00:00:00+00:00')",
            (orphan, str(tmp_path / "gone" / "a.jpg")),
        )
        db.commit()

        orphan_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (orphan,)
        ).fetchone()
        done_row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (done,)
        ).fetchone()

        pruned, kept_done = _prune_rows(db, [orphan_row, done_row], tmp_path / "report.txt")

        assert pruned == 1 and kept_done == 1
        # orphan + its dependents gone
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (orphan,)).fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM duplicates WHERE file_id_a=? OR file_id_b=?", (orphan, orphan)).fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM operations WHERE file_id=?", (orphan,)).fetchone()[0] == 0
        # survivor + done row untouched
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (survivor,)).fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (done,)).fetchone()[0] == 1
        # audit file written with the pruned path
        assert (tmp_path / "report.txt").read_text(encoding="utf-8").strip().endswith("a.jpg")


def test_relocate_prune_removes_lost_default_off(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    scan_root = tmp_path / "lib"
    scan_root.mkdir(parents=True)
    with Database(db_path) as db:
        gone = _add(db, scan_root / "deleted.jpg", sha256="deadbeef")
        db.commit()

        # Default (prune off): row remains.
        summary = relocate(db, [scan_root])
        assert summary["lost"] == 1 and summary.get("pruned", 0) == 0
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (gone,)).fetchone()[0] == 1

        # prune=True: orphan row removed.
        summary2 = relocate(db, [scan_root], prune=True)
        assert summary2["pruned"] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (gone,)).fetchone()[0] == 0


def test_cli_relocate_prune_flag():
    import photo_organizer.__main__ as m
    parser = m.build_parser()
    args = parser.parse_args(["relocate", "--db", "x.db", "--prune"])
    assert args.func is m.cmd_relocate and args.prune is True
    args2 = parser.parse_args(["relocate", "--db", "x.db"])
    assert args2.prune is False


def test_prune_missing_deletes_stale_keeps_present(tmp_path):
    from photo_organizer.relocate import prune_missing

    db_path = tmp_path / ".photo_organizer" / "library.db"
    present = tmp_path / "present.jpg"
    present.write_bytes(b"x")
    with Database(db_path) as db:
        stale = _add(db, tmp_path / "gone" / "a.jpg", sha256="aaaa")
        survivor = _add(db, present, sha256="bbbb")
        # Dependents referencing the stale row.
        db.conn.execute(
            "INSERT INTO duplicates (file_id_a, file_id_b, dup_type) VALUES (?,?,'EXACT')",
            (stale, survivor),
        )
        db.conn.execute(
            "INSERT INTO operations (file_id, op_type, source_path, status) "
            "VALUES (?, 'MOVE', ?, 'planned')",
            (stale, str(tmp_path / "gone" / "a.jpg")),
        )
        db.commit()

        summary = prune_missing(db)

        assert summary["pruned"] >= 1
        # stale row + dependents gone
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (stale,)).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM duplicates WHERE file_id_a=? OR file_id_b=?", (stale, stale)
        ).fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM operations WHERE file_id=?", (stale,)).fetchone()[0] == 0
        # present row kept
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (survivor,)).fetchone()[0] == 1


def test_prune_missing_keeps_done(tmp_path):
    from photo_organizer.relocate import prune_missing

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        done = _add(db, tmp_path / "gone" / "c.jpg", sha256="cccc", status="done")
        db.commit()

        summary = prune_missing(db)

        assert summary["kept_done"] == 1
        assert summary["pruned"] == 0
        # 'done' row with a missing file is NOT pruned.
        assert db.conn.execute("SELECT COUNT(*) FROM files WHERE file_id=?", (done,)).fetchone()[0] == 1


def test_cli_relocate_prune_only_flag():
    import photo_organizer.__main__ as m
    parser = m.build_parser()
    args = parser.parse_args(["relocate", "--db", "x.db", "--prune-only"])
    assert args.prune_only is True and args.func is m.cmd_relocate


def test_prune_creates_fk_indexes_to_avoid_fulltable_scan(tmp_path):
    # Regression: with foreign_keys=ON, deleting a files row makes SQLite verify
    # nothing still references it. files has self-referential FKs raw_pair_id /
    # jpeg_pair_id; unindexed, they force a FULL files-table scan per deleted row
    # (caused a >1h hang on the real 280k-row library). _prune_rows must create
    # indexes on every column referencing files(file_id) so the prune stays fast.
    from photo_organizer.relocate import _prune_rows

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        fid = _add(db, tmp_path / "gone" / "a.jpg", sha256="aaaa")
        row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (fid,)).fetchone()
        _prune_rows(db, [row], tmp_path / "report.txt")
        idx = {
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            "idx_files_raw_pair", "idx_files_jpeg_pair",
            "idx_dup_file_b", "idx_dup_keep", "idx_runlog_file",
        } <= idx
