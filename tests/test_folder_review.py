"""Tests for folderreview.py — folder twin-pair review web UI."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from photo_organizer.db import Database


def _add_overlap(db, folder_a, folder_b, shared=10, a_only=0, b_only=2, keeper="a"):
    db.insert_folder_overlap(
        folder_a=folder_a, folder_b=folder_b,
        shared_count=shared, a_only_count=a_only, b_only_count=b_only,
        coverage_a=round(shared / (shared + a_only), 4),
        coverage_b=round(shared / (shared + b_only), 4),
        keeper=keeper,
    )
    db.commit()
    return db.conn.execute(
        "SELECT overlap_id FROM folder_overlaps ORDER BY overlap_id DESC LIMIT 1"
    ).fetchone()[0]


def test_record_folder_overlap_decision(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        oid = _add_overlap(db, "D:\\A", "D:\\B")
        now = datetime.now(timezone.utc).isoformat()
        db.record_folder_overlap_decision(oid, "b", now)
        db.commit()
        row = db.conn.execute(
            "SELECT status, keeper, reviewed_at FROM folder_overlaps WHERE overlap_id=?",
            (oid,),
        ).fetchone()
        assert row["status"] == "reviewed"
        assert row["keeper"] == "b"
        assert row["reviewed_at"] == now


def test_reopen_folder_overlap(tmp_path):
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        oid = _add_overlap(db, "D:\\A", "D:\\B")
        db.record_folder_overlap_decision(oid, "a", datetime.now(timezone.utc).isoformat())
        db.commit()
        db.reopen_folder_overlap(oid)
        db.commit()
        row = db.conn.execute(
            "SELECT status, reviewed_at FROM folder_overlaps WHERE overlap_id=?",
            (oid,),
        ).fetchone()
        assert row["status"] == "pending"
        assert row["reviewed_at"] is None


def test_record_decision_raises_on_unknown_id(tmp_path):
    import pytest
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        with pytest.raises(KeyError, match="999"):
            db.record_folder_overlap_decision(999, "a", "2026-01-01T00:00:00+00:00")


def test_serve_get_renders_pair(tmp_path):
    import urllib.request
    from photo_organizer.folderreview import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        _add_overlap(db, "D:\\Canon\\Trip", "D:\\Backup\\Trip")
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/"
            body = urllib.request.urlopen(url).read().decode()
            assert "D:\\Canon\\Trip" in body
            assert "D:\\Backup\\Trip" in body
            assert "shared" in body.lower()
        finally:
            httpd.shutdown()




def test_serve_post_decision(tmp_path):
    import urllib.request
    from photo_organizer.folderreview import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        oid = _add_overlap(db, "D:\\A", "D:\\B", keeper="a")
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({"overlap_id": oid, "decision": "b"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-decision",
                data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            urllib.request.urlopen(req)
            row = db.conn.execute(
                "SELECT status, keeper FROM folder_overlaps WHERE overlap_id=?", (oid,),
            ).fetchone()
            assert row["status"] == "reviewed" and row["keeper"] == "b"
        finally:
            httpd.shutdown()


def test_serve_post_undecision(tmp_path):
    import urllib.request
    from photo_organizer.folderreview import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        oid = _add_overlap(db, "D:\\A", "D:\\B")
        db.record_folder_overlap_decision(oid, "a", datetime.now(timezone.utc).isoformat())
        db.commit()
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({"overlap_id": oid}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-undecision",
                data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            urllib.request.urlopen(req)
            row = db.conn.execute(
                "SELECT status FROM folder_overlaps WHERE overlap_id=?", (oid,)
            ).fetchone()
            assert row["status"] == "pending"
        finally:
            httpd.shutdown()


def test_serve_decision_all(tmp_path):
    import urllib.request
    from photo_organizer.folderreview import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        oid1 = _add_overlap(db, "D:\\A1", "D:\\B1")
        oid2 = _add_overlap(db, "D:\\A2", "D:\\B2")
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({
                "decisions": [
                    {"overlap_id": oid1, "decision": "a"},
                    {"overlap_id": oid2, "decision": "both"},
                ]
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-decision-all",
                data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            resp = urllib.request.urlopen(req)
            j = json.loads(resp.read())
            assert j["ok"] is True and j["saved"] == 2
            r1 = db.conn.execute(
                "SELECT keeper FROM folder_overlaps WHERE overlap_id=?", (oid1,)
            ).fetchone()
            r2 = db.conn.execute(
                "SELECT keeper FROM folder_overlaps WHERE overlap_id=?", (oid2,)
            ).fetchone()
            assert r1["keeper"] == "a"
            assert r2["keeper"] is None  # "both" → keeper NULL
        finally:
            httpd.shutdown()


def test_cli_review_folders_wired():
    import photo_organizer.__main__ as m
    parser = m.build_parser()
    args = parser.parse_args(["review", "--folders", "--db", "x.db"])
    assert args.func is m.cmd_review and args.folders is True
