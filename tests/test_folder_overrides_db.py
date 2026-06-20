"""Tests for the folder_overrides table + DB helpers (O1)."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest

from photo_organizer.db import Database, SCHEMA_VERSION


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_set_and_get_override(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        now = _now()
        db.set_folder_override(
            "D:\\Photos\\Kyoto",
            event_name="Kyoto",
            date_override="2023-06-15",
            updated_at=now,
        )
        db.commit()
        overrides = db.get_folder_overrides()
        assert "D:\\Photos\\Kyoto" in overrides
        row = overrides["D:\\Photos\\Kyoto"]
        assert row["event_name"] == "Kyoto"
        assert row["date_override"] == "2023-06-15"
        assert row["updated_at"] == now


def test_upsert_overwrites(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        db.set_folder_override(
            "D:\\Photos\\Kyoto", event_name="Kyoto", date_override="2023-06-15",
            updated_at=_now(),
        )
        db.commit()
        db.set_folder_override(
            "D:\\Photos\\Kyoto", event_name="Kyoto Trip", date_override="2023-06-16",
            updated_at=_now(),
        )
        db.commit()
        overrides = db.get_folder_overrides()
        assert len(overrides) == 1
        row = overrides["D:\\Photos\\Kyoto"]
        assert row["event_name"] == "Kyoto Trip"
        assert row["date_override"] == "2023-06-16"


def test_upsert_clears_omitted_date(tmp_path):
    # O2 relies on this: a second set_folder_override that omits date_override
    # must null it out (full-row replace via excluded.*), not keep the old value.
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        db.set_folder_override(
            "D:\\Photos\\Kyoto", event_name="Kyoto", date_override="2023-06-15",
            updated_at=_now(),
        )
        db.commit()
        db.set_folder_override(
            "D:\\Photos\\Kyoto", event_name="Kyoto", updated_at=_now(),
        )
        db.commit()
        row = db.get_folder_overrides()["D:\\Photos\\Kyoto"]
        assert row["event_name"] == "Kyoto"
        assert row["date_override"] is None


def test_set_event_only(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        db.set_folder_override(
            "D:\\Photos\\Osaka", event_name="Osaka", updated_at=_now(),
        )
        db.commit()
        row = db.get_folder_overrides()["D:\\Photos\\Osaka"]
        assert row["event_name"] == "Osaka"
        assert row["date_override"] is None


def test_clear_override(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        db.set_folder_override(
            "D:\\Photos\\Kyoto", event_name="Kyoto", updated_at=_now(),
        )
        db.commit()
        db.clear_folder_override("D:\\Photos\\Kyoto")
        db.commit()
        assert db.get_folder_overrides() == {}


def test_clear_unknown_raises(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        with pytest.raises(KeyError):
            db.clear_folder_override("D:\\nope")


def test_get_empty_returns_empty_dict(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        assert db.get_folder_overrides() == {}


def test_schema_version_is_5():
    assert SCHEMA_VERSION == 5
