"""
Tests for the `clone` command (photo_organizer.cloner) — a non-destructive,
DB-aware incremental backup of the already-organized library to another volume.

Design under test:
  * incremental skip (matching size+mtime → no re-copy, no re-hash)
  * copy + verify newly-copied files against the DB's known-good sha256
  * a corrupted copy is detected, retried once, then flagged (source untouched)
  * --verify-all re-hashes existing dest files and flags a tampered one
  * resumability: a leftover .tmp is cleaned and the file lands intact
  * --prune is OFF by default; ON it warns before removing stale dest files
  * the ledger DB is copied into the destination so the replica is self-describing
"""

from __future__ import annotations

import hashlib
import shutil

import photo_organizer.cloner as cloner
from photo_organizer.cloner import clone
from photo_organizer.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add_done_file(db, path, content: bytes) -> int:
    """Materialize a 'done' library file on disk and record it in the DB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, size_bytes, "
        "sha256, status) VALUES (?,?,?,?,?,?, 'done')",
        (str(path), path.name, path.suffix.lstrip("."), "CAMERA_JPEG",
         len(content), _sha256(content)),
    )
    db.conn.commit()
    return cur.lastrowid


def _mk_library(tmp_path):
    """Create a tiny organized library; return (db_path, target_root, dest_root)."""
    db_path = tmp_path / "photos.db"
    target_root = tmp_path / "lib"
    dest_root = tmp_path / "backup"
    with Database(db_path) as db:
        _add_done_file(db, target_root / "Masters" / "2023" / "a.jpg", b"alpha-bytes")
        _add_done_file(db, target_root / "Others" / "2024" / "b.jpg", b"beta-bytes!!")
    return db_path, target_root, dest_root


# ---------------------------------------------------------------------------
# 1. Incremental skip
# ---------------------------------------------------------------------------

def test_skip_when_dest_already_current(tmp_path, monkeypatch):
    db_path, target_root, dest_root = _mk_library(tmp_path)

    # Pre-seed the destination with an identical copy (same size+mtime) so the
    # incremental path must skip it without re-copying.
    src = target_root / "Masters" / "2023" / "a.jpg"
    dst = dest_root / "Masters" / "2023" / "a.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)  # copy2 preserves mtime → looks "already current"

    calls: list = []
    real_copy = cloner._copy_bytes
    monkeypatch.setattr(cloner, "_copy_bytes",
                        lambda s, d: (calls.append(str(s)), real_copy(s, d))[1])

    with Database(db_path) as db:
        stats = clone(db, dest_root, target_root=target_root)

    # a.jpg was already current → not re-copied; b.jpg is new → copied.
    assert str(src) not in calls
    assert stats.skipped >= 1
    assert stats.copied >= 1


# ---------------------------------------------------------------------------
# 2. Copy + verify a new file against the DB sha256
# ---------------------------------------------------------------------------

def test_new_file_copied_and_verified(tmp_path):
    db_path, target_root, dest_root = _mk_library(tmp_path)

    with Database(db_path) as db:
        stats = clone(db, dest_root, target_root=target_root)

    dst_a = dest_root / "Masters" / "2023" / "a.jpg"
    dst_b = dest_root / "Others" / "2024" / "b.jpg"
    assert dst_a.read_bytes() == b"alpha-bytes"
    assert dst_b.read_bytes() == b"beta-bytes!!"
    assert stats.copied == 2
    assert stats.verified == 2
    assert stats.errors == 0
    assert stats.bytes_copied == len(b"alpha-bytes") + len(b"beta-bytes!!")


# ---------------------------------------------------------------------------
# 3. Corrupted copy → detected, retried, flagged; source never deleted
# ---------------------------------------------------------------------------

def test_corrupted_copy_is_detected_and_flagged(tmp_path, monkeypatch):
    db_path, target_root, dest_root = _mk_library(tmp_path)

    # Stub the byte-copy to write WRONG bytes — every attempt corrupts.
    def _bad_copy(src, dst_tmp):
        dst_tmp.write_bytes(b"CORRUPTED-WRONG-BYTES")

    monkeypatch.setattr(cloner, "_copy_bytes", _bad_copy)

    with Database(db_path) as db:
        stats = clone(db, dest_root, target_root=target_root)

        assert stats.errors == 2          # both files fail to verify
        assert stats.verified == 0
        assert len(stats.error_paths) == 2

        # Loudly logged as backup errors.
        logged = db.conn.execute(
            "SELECT message FROM run_log WHERE phase='clone' AND level='ERROR'"
        ).fetchall()
        assert any("verif" in r["message"].lower() for r in logged)

    # The SOURCE files are completely untouched — nothing was deleted.
    assert (target_root / "Masters" / "2023" / "a.jpg").read_bytes() == b"alpha-bytes"
    assert (target_root / "Others" / "2024" / "b.jpg").read_bytes() == b"beta-bytes!!"


# ---------------------------------------------------------------------------
# 4. --verify-all re-hashes existing dest files and flags a tampered one
# ---------------------------------------------------------------------------

def test_verify_all_flags_tampered_existing_file(tmp_path):
    db_path, target_root, dest_root = _mk_library(tmp_path)

    with Database(db_path) as db:
        clone(db, dest_root, target_root=target_root)  # first, good backup

        # Tamper a dest file IN PLACE: same byte length, and restore mtime so the
        # cheap size+mtime check would NOT notice. Only a full re-hash catches it.
        dst_a = dest_root / "Masters" / "2023" / "a.jpg"
        src_a = target_root / "Masters" / "2023" / "a.jpg"
        good_stat = dst_a.stat()
        dst_a.write_bytes(b"XXXXX-bytes")  # same length as b"alpha-bytes"
        import os
        os.utime(dst_a, (good_stat.st_atime, good_stat.st_mtime))

        stats = clone(db, dest_root, target_root=target_root, verify_all=True)

        # The tamper is flagged loudly...
        logged = db.conn.execute(
            "SELECT message FROM run_log WHERE phase='clone'"
        ).fetchall()
        assert any("verify-all" in r["message"].lower()
                   or "mismatch" in r["message"].lower() for r in logged)
        assert stats.tampered >= 1

        # ...and the destination is healed back to the known-good bytes.
        assert dst_a.read_bytes() == b"alpha-bytes"
        # Source unchanged.
        assert src_a.read_bytes() == b"alpha-bytes"


# ---------------------------------------------------------------------------
# 5. Resumability — a leftover .tmp is re-copied cleanly, no .tmp left behind
# ---------------------------------------------------------------------------

def test_resume_cleans_leftover_tmp(tmp_path):
    db_path, target_root, dest_root = _mk_library(tmp_path)

    # Simulate an interrupted copy: a truncated .tmp exists but the final file
    # was never renamed into place.
    dst_a = dest_root / "Masters" / "2023" / "a.jpg"
    dst_a.parent.mkdir(parents=True, exist_ok=True)
    leftover = dst_a.parent / (dst_a.name + ".tmp")
    leftover.write_bytes(b"trunc")  # partial, garbage

    with Database(db_path) as db:
        clone(db, dest_root, target_root=target_root)

    # Final file present and correct; the .tmp is gone.
    assert dst_a.read_bytes() == b"alpha-bytes"
    assert not leftover.exists()
    assert list(dest_root.rglob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 6. --prune is OFF by default; ON it warns before removing stale dest files
# ---------------------------------------------------------------------------

def test_prune_off_by_default_keeps_stale(tmp_path):
    db_path, target_root, dest_root = _mk_library(tmp_path)

    with Database(db_path) as db:
        clone(db, dest_root, target_root=target_root)

    # A file at the destination that is NOT in the library.
    stale = dest_root / "Masters" / "2023" / "stale_old.jpg"
    stale.write_bytes(b"no-longer-in-library")

    with Database(db_path) as db:
        stats = clone(db, dest_root, target_root=target_root)  # prune defaults OFF

    assert stale.exists()       # additive mirror leaves it alone
    assert stats.pruned == 0


def test_prune_warns_then_removes_stale(tmp_path):
    db_path, target_root, dest_root = _mk_library(tmp_path)

    with Database(db_path) as db:
        clone(db, dest_root, target_root=target_root)

    stale = dest_root / "Masters" / "2023" / "stale_old.jpg"
    stale.write_bytes(b"no-longer-in-library")

    with Database(db_path) as db:
        stats = clone(db, dest_root, target_root=target_root, prune=True)

        # Warned (logged) before removing.
        logged = db.conn.execute(
            "SELECT message FROM run_log WHERE phase='clone' AND message LIKE 'Prune%'"
        ).fetchall()
        assert len(logged) >= 1

    assert not stale.exists()
    assert stats.pruned == 1


# ---------------------------------------------------------------------------
# 7. The ledger DB is included in the destination (self-describing replica)
# ---------------------------------------------------------------------------

def test_ledger_copied_to_destination(tmp_path):
    db_path, target_root, dest_root = _mk_library(tmp_path)

    with Database(db_path) as db:
        clone(db, dest_root, target_root=target_root)

    replica_db = dest_root / ".photo_organizer" / db_path.name
    assert replica_db.exists()
    # It is a usable SQLite DB describing the same library.
    with Database(replica_db) as rdb:
        n = rdb.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert n == 2
