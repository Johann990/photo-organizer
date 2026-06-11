"""
Tests for Layer 1 — the durable, versioned, library-adjacent ledger.

Covers:
  * default DB-location resolution (_resolve_db_path): explicit --db wins, then
    config db, then the library-adjacent default {target}/.photo_organizer/...;
  * the schema_version guard: a fresh DB is stamped, an older DB migrates and is
    bumped, and a fabricated NEWER version is refused.
"""

from __future__ import annotations


import pytest

from photo_organizer.__main__ import _resolve_db_path
from photo_organizer.db import (
    SCHEMA_VERSION,
    Database,
    SchemaVersionError,
    default_db_path,
)


# ---------------------------------------------------------------------------
# Default DB-location resolution
# ---------------------------------------------------------------------------

def test_explicit_db_arg_always_wins(tmp_path):
    explicit = tmp_path / "explicit.db"
    target = tmp_path / "Library"
    # Even with a config db AND a target present, --db wins.
    resolved = _resolve_db_path(str(explicit), str(tmp_path / "cfg.db"), target)
    assert resolved == explicit


def test_config_db_used_when_no_arg(tmp_path):
    cfg_db = tmp_path / "cfg.db"
    target = tmp_path / "Library"
    resolved = _resolve_db_path(None, str(cfg_db), target)
    assert resolved == cfg_db


def test_defaults_under_target_when_no_db(tmp_path):
    target = tmp_path / "Library"
    resolved = _resolve_db_path(None, None, target)
    assert resolved == target / ".photo_organizer" / "library.db"
    assert resolved == default_db_path(target)


def test_none_when_nothing_resolvable():
    assert _resolve_db_path(None, None, None) is None


def test_default_db_path_parent_is_created_on_connect(tmp_path):
    target = tmp_path / "Library"  # does not exist yet
    db_path = default_db_path(target)
    assert not db_path.parent.exists()
    with Database(db_path):
        pass
    assert db_path.exists()
    assert db_path.parent.name == ".photo_organizer"


# ---------------------------------------------------------------------------
# schema_version guard
# ---------------------------------------------------------------------------

def test_fresh_db_is_stamped_with_current_version(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        row = db.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None
        assert int(row["value"]) == SCHEMA_VERSION


def test_older_db_migrates_and_is_bumped(tmp_path):
    db_path = tmp_path / "photos.db"
    # Simulate an older DB: connect once, then force the marker backwards.
    with Database(db_path) as db:
        db.conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        db.commit()
    # Re-opening runs idempotent migrations and bumps the marker forward.
    with Database(db_path) as db:
        row = db.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert int(row["value"]) == SCHEMA_VERSION


def test_newer_db_is_refused(tmp_path):
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        db.conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION + 5),),
        )
        db.commit()
    with pytest.raises(SchemaVersionError):
        with Database(db_path):
            pass


def test_premarker_db_is_recognised_and_stamped(tmp_path):
    """A DB created before the meta table existed must still open and migrate."""
    db_path = tmp_path / "photos.db"
    with Database(db_path) as db:
        db.conn.execute("DELETE FROM meta WHERE key='schema_version'")
        db.commit()
    with Database(db_path) as db:
        row = db.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert int(row["value"]) == SCHEMA_VERSION
