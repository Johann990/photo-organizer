"""Tests for folderorganize.py — no-event / low-confidence-date folder review UI."""
from __future__ import annotations

import json

from photo_organizer.db import Database


def _add_file(
    db,
    path,
    *,
    sha=None,
    status="scanned",
    file_type="CAMERA_JPEG",
    datetime_original=None,
    date_confidence=None,
    camera_model=None,
    mtime=None,
):
    filename = path.split("\\")[-1]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    db.conn.execute(
        "INSERT INTO files "
        "(path, filename, extension, sha256, status, file_type, size_bytes, "
        " datetime_original, date_confidence, camera_model, mtime) "
        "VALUES (?, ?, ?, ?, ?, ?, 1000, ?, ?, ?, ?)",
        (
            path, filename, ext, sha, status, file_type,
            datetime_original, date_confidence, camera_model, mtime,
        ),
    )
    db.commit()
    return db.conn.execute(
        "SELECT file_id FROM files WHERE path=?", (path,)
    ).fetchone()[0]


def _stage_delete(db, file_id, source_path):
    db.conn.execute(
        "INSERT INTO operations (file_id, op_type, source_path, status, planned_at) "
        "VALUES (?, 'STAGE_DELETE', ?, 'planned', '2026-06-20T00:00:00+00:00')",
        (file_id, source_path),
    )
    db.commit()


def test_candidate_includes_no_event_folder(tmp_path):
    from photo_organizer.folderorganize import FolderOrganizeState

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            camera_model="Canon EOS 5D",
        )
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        assert folder in folders
        assert folders[folder]["is_no_event"] is True


def test_candidate_includes_low_date_folder(tmp_path):
    from photo_organizer.folderorganize import FolderOrganizeState

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\Photos\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original=None, date_confidence="LOW",
        )
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        assert folder in folders
        assert folders[folder]["low_date_count"] == 1
        assert folders[folder]["is_no_event"] is False


def test_normal_folder_excluded(tmp_path):
    from photo_organizer.folderorganize import FolderOrganizeState

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\Photos\Kyoto"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        assert folder not in folders


def test_staged_files_excluded(tmp_path):
    from photo_organizer.folderorganize import FolderOrganizeState

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        path = folder + r"\IMG_0001.jpg"
        fid = _add_file(
            db, path,
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        _stage_delete(db, fid, path)
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        assert folder not in folders


def test_date_range_computed(tmp_path):
    from photo_organizer.folderorganize import FolderOrganizeState

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:01 10:00:00", date_confidence="HIGH",
        )
        _add_file(
            db, folder + r"\IMG_0002.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        assert folders[folder]["date_lo"] == "2012-09-01"
        assert folders[folder]["date_hi"] == "2012-09-08"


def test_get_renders_candidate(tmp_path):
    import urllib.request
    from photo_organizer.folderorganize import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/"
            body = urllib.request.urlopen(url).read().decode()
            assert folder in body
            assert "<input" in body
        finally:
            httpd.shutdown()


def test_post_override_saves(tmp_path):
    import urllib.request
    from photo_organizer.folderorganize import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({
                "source_folder": folder, "event_name": "上海",
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-override",
                data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            urllib.request.urlopen(req)
            overrides = db.get_folder_overrides()
            assert folder in overrides
            assert overrides[folder]["event_name"] == "上海"
        finally:
            httpd.shutdown()


def test_post_override_clear(tmp_path):
    import urllib.request
    from photo_organizer.folderorganize import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\DCIM\100EOS5D"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({
                "source_folder": folder, "event_name": "上海",
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-override",
                data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            urllib.request.urlopen(req)
            assert folder in db.get_folder_overrides()

            clear_payload = json.dumps({"source_folder": folder}).encode()
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-override-clear",
                data=clear_payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1",
                         "Content-Length": str(len(clear_payload))},
            )
            urllib.request.urlopen(req2)
            assert folder not in db.get_folder_overrides()

            # Clearing again (nothing saved) should still return 200, no error.
            req3 = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-override-clear",
                data=clear_payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1",
                         "Content-Length": str(len(clear_payload))},
            )
            resp = urllib.request.urlopen(req3)
            assert resp.status == 200
        finally:
            httpd.shutdown()


def test_post_override_all(tmp_path):
    import urllib.request
    from photo_organizer.folderorganize import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder1 = r"D:\DCIM\100EOS5D"
        folder2 = r"D:\DCIM\101EOS5D"
        _add_file(
            db, folder1 + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        _add_file(
            db, folder2 + r"\IMG_0002.jpg",
            datetime_original="2012:09:09 10:00:00", date_confidence="HIGH",
        )
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({
                "overrides": [
                    {"source_folder": folder1, "event_name": "Tokyo"},
                    {"source_folder": folder2, "event_name": "Osaka"},
                ]
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-override-all",
                data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            resp = urllib.request.urlopen(req)
            j = json.loads(resp.read())
            assert j["ok"] is True and j["saved"] == 2
            overrides = db.get_folder_overrides()
            assert overrides[folder1]["event_name"] == "Tokyo"
            assert overrides[folder2]["event_name"] == "Osaka"
        finally:
            httpd.shutdown()


def test_cli_review_organize_wired():
    import photo_organizer.__main__ as m
    parser = m.build_parser()
    args = parser.parse_args(["review", "--organize", "--db", "x.db"])
    assert args.func is m.cmd_review and args.organize is True
