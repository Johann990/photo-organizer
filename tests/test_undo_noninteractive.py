"""
Tests for `executor.undo` non-interactive safety.

`undo` asks "Proceed with undo? [y/N]" via input().  In a background / piped
shell stdin is not a tty and input() blocks forever waiting for a keystroke
that never comes (observed: a 392-file undo "ran" 7 minutes doing nothing,
stuck on the prompt).  When stdin is not interactive and --force was not given,
undo must refuse safely instead of calling input().

Run: python -m pytest tests/test_undo_noninteractive.py
"""

from __future__ import annotations

import builtins

from photo_organizer import executor
from photo_organizer.db import Database


def _add_done_move(db, tmp_path):
    """One completed MOVE so undo gets past the 'nothing to undo' early exit."""
    cur = db.conn.execute(
        "INSERT INTO files (path, filename, extension, file_type, status) "
        "VALUES (?,?,?,?,?)",
        (str(tmp_path / "dst.jpg"), "dst.jpg", "jpg", "CAMERA_JPEG", "done"),
    )
    fid = cur.lastrowid
    db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, target_path, status) "
        "VALUES (?,?,?,?,?)",
        (fid, "MOVE", str(tmp_path / "src.jpg"), str(tmp_path / "dst.jpg"), "done"),
    )
    db.commit()
    return fid


def test_undo_non_tty_without_force_does_not_prompt(tmp_path, monkeypatch):
    with Database(tmp_path / "lib.db") as db:
        _add_done_move(db, tmp_path)

        # Simulate a non-interactive shell.
        monkeypatch.setattr(
            executor.sys.stdin, "isatty", lambda: False, raising=False
        )

        # input() must NOT be called — if it is, the real bug (blocking) occurs.
        def _boom(*_a, **_k):
            raise AssertionError("undo called input() in a non-interactive shell")

        monkeypatch.setattr(builtins, "input", _boom)

        # Must return safely (no prompt, no exception, no hang).
        assert executor.undo(db, force=False) is None

        # Nothing was reverted: the operation is still 'done'.
        status = db.conn.execute(
            "SELECT status FROM operations WHERE op_type='MOVE'"
        ).fetchone()[0]
        assert status == "done"
