"""
Tests for `sync` — fast, explicit DB resync for manual Explorer edits to
already-organized ('done') files (Pillar 2: relocate's find_stale_rows scans
the WHOLE files table, far too slow for a 3-file edit; this is "tell, don't
detect" — the user states old/new paths explicitly, no library-wide scan).

Two operations:
  relocate_path(db, old, new) — a folder rename or single-file move. Writes
    a 'RENAME' operations row per affected file (status='done', source=old,
    target=new) so the EXISTING executor.undo(op_type='RENAME') reverses it
    for free — no new undo code needed.
  acknowledge_deleted(db, path, yes) — records a 'DELETE' op for files the
    user removed directly in Explorer, bypassing the staging workflow. Does
    NOT touch files.path/status (nothing to restore) — this is a one-way
    acknowledgment, not a reversible move.
"""

from __future__ import annotations

import os

from photo_organizer.db import Database
from photo_organizer.sync import acknowledge_deleted, relocate_path


def _add_file(db, path, *, status="done") -> int:
    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status) "
        "VALUES (?,?,?,?,?)",
        (str(path), path.name, path.suffix.lstrip("."), "CAMERA_JPEG", status),
    )
    return cur.lastrowid


def _add_done_move(db, file_id, source, target):
    db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, target_path, status, executed_at) "
        "VALUES (?, 'MOVE', ?, ?, 'done', '2020-01-01T00:00:00')",
        (file_id, str(source), str(target)),
    )


# ── relocate_path (rename / move) ────────────────────────────────────────────

def test_relocate_path_renames_folder_and_writes_rename_op(tmp_path):
    old_dir = tmp_path / "Masters" / "2020" / "2020-01-12 三貂嶺_小蜂"
    old_dir.mkdir(parents=True)
    f = old_dir / "DSC001.JPG"
    f.write_text("x")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, f)
        _add_done_move(db, fid, f"src/{f.name}", f)
        db.commit()

        new_dir = tmp_path / "Masters" / "2020" / "2020-01-12 三貂嶺"
        os.rename(old_dir, new_dir)  # the user's manual Explorer rename

        result = relocate_path(db, old_dir, new_dir)

        row = db.conn.execute(
            "SELECT path FROM files WHERE file_id=?", (fid,)
        ).fetchone()
        assert row["path"] == str(new_dir / "DSC001.JPG")

        op = db.conn.execute(
            "SELECT op_type, source_path, target_path, status FROM operations "
            "WHERE file_id=? AND op_type='RENAME'", (fid,)
        ).fetchone()
        assert op["status"] == "done"
        assert op["source_path"] == str(f)
        assert op["target_path"] == str(new_dir / "DSC001.JPG")
    assert result["relocated"] == 1


def test_relocate_path_does_not_match_unrelated_sibling(tmp_path):
    # The most common real folder shape in this library has literal '_' —
    # SQLite LIKE treats '_' as "any single char" unless escaped, so an
    # unescaped scope query would also match an unrelated sibling folder
    # that merely has some other character in that position.
    old_dir = tmp_path / "Masters" / "2020" / "三貂嶺_小蜂"
    old_dir.mkdir(parents=True)
    f = old_dir / "a.jpg"
    f.write_text("x")
    # A sibling whose name differs only in what the unescaped '_' wildcard
    # would treat as "any character" — must NEVER be touched by this sync.
    sibling_dir = tmp_path / "Masters" / "2020" / "三貂嶺X小蜂"
    sibling_dir.mkdir(parents=True)
    sibling_f = sibling_dir / "b.jpg"
    sibling_f.write_text("y")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid_a = _add_file(db, f)
        fid_b = _add_file(db, sibling_f)
        _add_done_move(db, fid_a, "src/a.jpg", f)
        _add_done_move(db, fid_b, "src/b.jpg", sibling_f)
        db.commit()

        new_dir = tmp_path / "Masters" / "2020" / "三貂嶺"
        os.rename(old_dir, new_dir)

        relocate_path(db, old_dir, new_dir)

        sibling_row = db.conn.execute(
            "SELECT path FROM files WHERE file_id=?", (fid_b,)
        ).fetchone()
        assert sibling_row["path"] == str(sibling_f)  # untouched


def test_relocate_path_single_file_move(tmp_path):
    src_dir = tmp_path / "Masters" / "2020" / "EventA"
    dst_dir = tmp_path / "Masters" / "2020" / "EventB"
    src_dir.mkdir(parents=True)
    dst_dir.mkdir(parents=True)
    old_file = src_dir / "IMG_0001.JPG"
    old_file.write_text("x")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, old_file)
        _add_done_move(db, fid, "src/IMG_0001.JPG", old_file)
        db.commit()

        new_file = dst_dir / "IMG_0001.JPG"
        os.rename(old_file, new_file)

        result = relocate_path(db, old_file, new_file)

        row = db.conn.execute(
            "SELECT path FROM files WHERE file_id=?", (fid,)
        ).fetchone()
        assert row["path"] == str(new_file)
    assert result["relocated"] == 1


def test_relocate_path_refuses_when_old_path_still_on_disk(tmp_path):
    old_dir = tmp_path / "Masters" / "Event"
    old_dir.mkdir(parents=True)
    (old_dir / "a.jpg").write_text("x")
    new_dir = tmp_path / "Masters" / "EventNew"
    new_dir.mkdir(parents=True)

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, old_dir / "a.jpg")
        _add_done_move(db, fid, "src/a.jpg", old_dir / "a.jpg")
        db.commit()

        # old_dir was never actually moved — refuse rather than guess.
        result = relocate_path(db, old_dir, new_dir)

        row = db.conn.execute(
            "SELECT path FROM files WHERE file_id=?", (fid,)
        ).fetchone()
        assert row["path"] == str(old_dir / "a.jpg")  # untouched
    assert result["relocated"] == 0
    assert result["refused"] == "old_path_still_exists"


def test_relocate_path_refuses_when_new_path_missing_on_disk(tmp_path):
    old_dir = tmp_path / "Masters" / "Event"
    old_dir.mkdir(parents=True)
    f = old_dir / "a.jpg"
    f.write_text("x")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, f)
        _add_done_move(db, fid, "src/a.jpg", f)
        db.commit()

        import shutil
        shutil.rmtree(old_dir)  # deleted, not renamed — no new location at all

        new_dir = tmp_path / "Masters" / "EventNew"  # never created
        result = relocate_path(db, old_dir, new_dir)

        row = db.conn.execute(
            "SELECT path FROM files WHERE file_id=?", (fid,)
        ).fetchone()
        assert row["path"] == str(f)  # untouched
    assert result["relocated"] == 0
    assert result["refused"] == "new_path_missing"


def test_relocate_path_skips_non_done_rows(tmp_path):
    old_dir = tmp_path / "Masters" / "Event"
    old_dir.mkdir(parents=True)
    f = old_dir / "a.jpg"
    f.write_text("x")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, f, status="confirmed")  # not yet executed
        db.commit()

        new_dir = tmp_path / "Masters" / "EventNew"
        os.rename(old_dir, new_dir)

        result = relocate_path(db, old_dir, new_dir)

        row = db.conn.execute(
            "SELECT path, status FROM files WHERE file_id=?", (fid,)
        ).fetchone()
        assert row["path"] == str(f)  # untouched — sync only targets 'done' files
        assert row["status"] == "confirmed"
    assert result["relocated"] == 0
    assert result["skipped_not_done"] == 1


# ── acknowledge_deleted ───────────────────────────────────────────────────────

def test_acknowledge_deleted_writes_delete_op_without_touching_path(tmp_path):
    f = tmp_path / "Masters" / "Event" / "a.jpg"
    f.parent.mkdir(parents=True)
    f.write_text("x")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, f)
        _add_done_move(db, fid, "src/a.jpg", f)
        db.commit()

        f.unlink()  # the user's manual Explorer delete

        result = acknowledge_deleted(db, f, yes=True)

        row = db.conn.execute(
            "SELECT path, status FROM files WHERE file_id=?", (fid,)
        ).fetchone()
        assert row["path"] == str(f)  # left as-is — nothing to point it at
        assert row["status"] == "done"  # unchanged

        op = db.conn.execute(
            "SELECT op_type, source_path, target_path, status FROM operations "
            "WHERE file_id=? AND op_type='DELETE'", (fid,)
        ).fetchone()
        assert op["status"] == "done"
        assert op["source_path"] == str(f)
        assert op["target_path"] is None
    assert result["acknowledged"] == 1


def test_acknowledge_deleted_refuses_when_path_still_on_disk(tmp_path):
    f = tmp_path / "Masters" / "Event" / "a.jpg"
    f.parent.mkdir(parents=True)
    f.write_text("x")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, f)
        _add_done_move(db, fid, "src/a.jpg", f)
        db.commit()

        # f was never actually deleted — refuse rather than acknowledge a lie.
        result = acknowledge_deleted(db, f, yes=True)

        op = db.conn.execute(
            "SELECT COUNT(*) AS n FROM operations WHERE file_id=? AND op_type='DELETE'",
            (fid,),
        ).fetchone()
        assert op["n"] == 0
    assert result["acknowledged"] == 0
    assert result["skipped_still_on_disk"] == 1


def test_acknowledge_deleted_requires_yes(tmp_path):
    f = tmp_path / "Masters" / "Event" / "a.jpg"
    f.parent.mkdir(parents=True)
    f.write_text("x")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, f)
        _add_done_move(db, fid, "src/a.jpg", f)
        db.commit()
        f.unlink()

        result = acknowledge_deleted(db, f, yes=False)

        op = db.conn.execute(
            "SELECT COUNT(*) AS n FROM operations WHERE file_id=? AND op_type='DELETE'",
            (fid,),
        ).fetchone()
        assert op["n"] == 0
    assert result["acknowledged"] == 0
    assert result["refused"] == "not_confirmed"


def test_acknowledge_deleted_folder_scope(tmp_path):
    folder = tmp_path / "Masters" / "Event"
    folder.mkdir(parents=True)
    f1, f2 = folder / "a.jpg", folder / "b.jpg"
    f1.write_text("x")
    f2.write_text("y")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid1 = _add_file(db, f1)
        fid2 = _add_file(db, f2)
        _add_done_move(db, fid1, "src/a.jpg", f1)
        _add_done_move(db, fid2, "src/b.jpg", f2)
        db.commit()

        import shutil
        shutil.rmtree(folder)

        result = acknowledge_deleted(db, folder, yes=True)

        for fid in (fid1, fid2):
            op = db.conn.execute(
                "SELECT status FROM operations WHERE file_id=? AND op_type='DELETE'",
                (fid,),
            ).fetchone()
            assert op["status"] == "done"
    assert result["acknowledged"] == 2


# ── Reversibility: rename/move ride the EXISTING undo() for free ────────────

def test_relocate_path_is_reversible_via_existing_undo(tmp_path, monkeypatch):
    from photo_organizer import executor

    old_dir = tmp_path / "Masters" / "Event"
    old_dir.mkdir(parents=True)
    f = old_dir / "a.jpg"
    f.write_text("x")

    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        fid = _add_file(db, f)
        _add_done_move(db, fid, "src/a.jpg", f)
        db.commit()

        new_dir = tmp_path / "Masters" / "EventRenamed"
        os.rename(old_dir, new_dir)
        new_f = new_dir / "a.jpg"
        relocate_path(db, old_dir, new_dir)

        monkeypatch.setattr(executor, "_same_drive", lambda a, b: True)
        executor.undo(db, force=True, op_type="RENAME")

        row = db.conn.execute(
            "SELECT path FROM files WHERE file_id=?", (fid,)
        ).fetchone()
        assert row["path"] == str(f)  # back to the pre-rename path
        assert f.exists()
        assert not new_f.exists()
