"""
Tests for command-timing history (the `timings` command's data layer).

Covers:
  * record_command_run appends rows (never overwrites);
  * command_timings aggregates runs/avg/min/max and reports the LAST run's
    duration and status, ordered most-recent-first.
"""

from __future__ import annotations

from photo_organizer.db import Database


def _db(tmp_path):
    return Database(tmp_path / "t.db")


def test_record_and_aggregate(tmp_path):
    with _db(tmp_path) as db:
        db.record_command_run("dedup", "2026-01-01T00:00:00+00:00",
                              "2026-01-01T00:10:00+00:00", 600.0)
        db.record_command_run("dedup", "2026-01-02T00:00:00+00:00",
                              "2026-01-02T00:05:00+00:00", 300.0)

        rows = db.command_timings()
        assert len(rows) == 1
        r = rows[0]
        assert r["command"] == "dedup"
        assert r["runs"] == 2
        assert r["avg_s"] == 450.0
        assert r["min_s"] == 300.0
        assert r["max_s"] == 600.0
        # last run is the 2026-01-02 one (300s)
        assert r["last_s"] == 300.0
        assert r["last_status"] == "ok"


def test_multiple_commands_ordered_by_recency(tmp_path):
    with _db(tmp_path) as db:
        db.record_command_run("scan", "2026-01-01T00:00:00+00:00",
                              "2026-01-01T00:01:00+00:00", 60.0)
        db.record_command_run("plan", "2026-01-03T00:00:00+00:00",
                              "2026-01-03T00:00:30+00:00", 30.0)
        db.record_command_run("dedup", "2026-01-02T00:00:00+00:00",
                              "2026-01-02T00:02:00+00:00", 120.0)

        rows = db.command_timings()
        # Ordered by last_at DESC: plan (Jan 3), dedup (Jan 2), scan (Jan 1)
        assert [r["command"] for r in rows] == ["plan", "dedup", "scan"]


def test_last_status_reflects_most_recent_run(tmp_path):
    with _db(tmp_path) as db:
        db.record_command_run("execute", "2026-01-01T00:00:00+00:00",
                              "2026-01-01T00:01:00+00:00", 60.0, status="ok")
        db.record_command_run("execute", "2026-01-02T00:00:00+00:00",
                              "2026-01-02T00:00:10+00:00", 10.0,
                              status="interrupted")

        rows = db.command_timings()
        assert rows[0]["last_status"] == "interrupted"
        assert rows[0]["runs"] == 2


def test_empty_history(tmp_path):
    with _db(tmp_path) as db:
        assert db.command_timings() == []
