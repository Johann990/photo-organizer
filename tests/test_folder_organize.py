"""Tests for folderorganize.py — no-event / low-confidence-date folder review UI."""
from __future__ import annotations

import json
from pathlib import Path

from photo_organizer.db import Database


def _write_jpeg(path):
    from PIL import Image
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (120, 60, 30)).save(p, "JPEG")
    return str(p)


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


def test_video_low_date_does_not_trigger_candidate(tmp_path):
    # A well-named folder whose ONLY low-date file is a VIDEO must NOT appear:
    # video mtime-only dates are a systemic planner concern, not a per-folder fix.
    from photo_organizer.folderorganize import FolderOrganizeState

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\Photos\Kyoto"
        _add_file(
            db, folder + r"\MVI_0001.mov",
            file_type="VIDEO", datetime_original=None, date_confidence="LOW",
        )
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
        )
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        assert folder not in folders  # good name + only a video is LOW → skip


def test_photo_low_date_still_triggers_despite_video(tmp_path):
    # A non-video LOW file still flags the folder; the video LOW is not counted.
    from photo_organizer.folderorganize import FolderOrganizeState

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\Photos\Kyoto"
        _add_file(
            db, folder + r"\MVI_0001.mov",
            file_type="VIDEO", datetime_original=None, date_confidence="LOW",
        )
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original=None, date_confidence="LOW",
        )
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        assert folder in folders
        assert folders[folder]["low_date_count"] == 1  # video LOW not counted


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
            # KEY CONTRACT (load-bearing with plan): the saved override key must
            # equal the file's IMMEDIATE parent dir — not a resolved ancestor.
            from pathlib import PureWindowsPath
            saved_key = next(iter(overrides))
            assert saved_key == str(
                PureWindowsPath(folder + r"\IMG_0001.jpg").parent
            )
        finally:
            httpd.shutdown()


def test_post_override_special_chars_roundtrip(tmp_path):
    # A folder path with characters that need HTML/JS escaping must round-trip
    # byte-exact from the data-attribute → POST → folder_overrides key, or the
    # plan lookup (keyed on the exact parent string) silently misses.
    import urllib.request
    from photo_organizer.folderorganize import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = r"D:\DCIM\O'Brien & <co>"
        _add_file(
            db, folder + r"\IMG_0001.jpg",
            datetime_original="2012:09:08 10:00:00", date_confidence="LOW",
        )
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({
                "source_folder": folder, "event_name": "派對",
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
            assert overrides[folder]["event_name"] == "派對"
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


def test_per_day_candidate_listed(tmp_path):
    from photo_organizer.folderorganize import FolderOrganizeState
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        _add_file(db, root + r"\0808\a.jpg",
                  datetime_original="2005:08:08 10:00:00", date_confidence="HIGH")
        _add_file(db, root + r"\0809\b.jpg",
                  datetime_original="2005:08:09 10:00:00", date_confidence="HIGH")
        st = FolderOrganizeState(db)
        roots = {c["event_folder"] for c in st.per_day_candidates}
        assert root in roots


def test_post_per_day_split_saves(tmp_path):
    import urllib.request
    from photo_organizer.folderorganize import serve
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        _add_file(db, root + r"\0808\a.jpg",
                  datetime_original="2005:08:08 10:00:00", date_confidence="HIGH")
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({"source_folder": root, "per_day_split": 1}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-override",
                data=payload, method="POST",
                headers={"Content-Type": "application/json", "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            urllib.request.urlopen(req)
            assert db.get_folder_overrides()[root]["per_day_split"] == 1
        finally:
            httpd.shutdown()


def test_per_day_toggle_does_not_clobber_event_name(tmp_path):
    # Setting per_day_split via POST must preserve an existing event_name.
    import urllib.request
    from photo_organizer.folderorganize import serve
    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        root = r"D:\Raw\20050814 蒙古"
        _add_file(db, root + r"\0808\a.jpg",
                  datetime_original="2005:08:08 10:00:00", date_confidence="HIGH")
        db.set_folder_override(root, event_name="蒙古", updated_at="2026-06-21T00:00:00+00:00")
        db.commit()
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            payload = json.dumps({"source_folder": root, "per_day_split": 1}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/folder-override",
                data=payload, method="POST",
                headers={"Content-Type": "application/json", "Host": "127.0.0.1",
                         "Content-Length": str(len(payload))},
            )
            urllib.request.urlopen(req)
            ov = db.get_folder_overrides()[root]
            assert ov["per_day_split"] == 1
            assert ov["event_name"] == "蒙古"   # NOT clobbered
        finally:
            httpd.shutdown()


def test_cli_review_organize_wired():
    import photo_organizer.__main__ as m
    parser = m.build_parser()
    args = parser.parse_args(["review", "--organize", "--db", "x.db"])
    assert args.func is m.cmd_review and args.organize is True


def test_sample_fids_for_image_folder(tmp_path):
    from photo_organizer.folderorganize import FolderOrganizeState

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = str(tmp_path / "DCIM" / "100EOS5D")
        jpg1 = _write_jpeg(folder + r"\IMG_0001.jpg")
        jpg2 = _write_jpeg(folder + r"\IMG_0002.jpg")
        _add_file(
            db, jpg1,
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            file_type="CAMERA_JPEG",
        )
        _add_file(
            db, jpg2,
            datetime_original="2012:09:08 10:01:00", date_confidence="HIGH",
            file_type="CAMERA_JPEG",
        )
        # A RAW and a VIDEO file in the same folder — must NOT show up as thumbs.
        _add_file(
            db, folder + r"\IMG_0003.CR2",
            datetime_original="2012:09:08 10:02:00", date_confidence="HIGH",
            file_type="RAW",
        )
        _add_file(
            db, folder + r"\MVI_0004.mov",
            datetime_original="2012:09:08 10:03:00", date_confidence="HIGH",
            file_type="VIDEO",
        )
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        assert folder in folders
        sample_fids = folders[folder]["sample_fids"]
        assert len(sample_fids) == 2
        thumbnailable_paths = {jpg1, jpg2}
        for fid in sample_fids:
            assert state.meta[fid]["path"] in thumbnailable_paths


def test_thumb_endpoint_serves_image(tmp_path):
    import urllib.request
    from photo_organizer.folderorganize import FolderOrganizeState, serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = str(tmp_path / "DCIM" / "100EOS5D")
        jpg = _write_jpeg(folder + r"\IMG_0001.jpg")
        _add_file(
            db, jpg,
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            file_type="CAMERA_JPEG",
        )
        state = FolderOrganizeState(db)
        folders = {f["folder"]: f for f in state.folders}
        fid = folders[folder]["sample_fids"][0]

        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            port = httpd.server_address[1]
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/thumb/{fid}")
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "image/jpeg"
            body = resp.read()
            assert body[:2] == b"\xff\xd8"
        finally:
            httpd.shutdown()


def test_thumb_unknown_fid_404(tmp_path):
    import urllib.error
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
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/thumb/999999")
                assert False, "expected HTTPError"
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
        finally:
            httpd.shutdown()


def test_get_page_has_thumb_imgs(tmp_path):
    import urllib.request
    from photo_organizer.folderorganize import serve

    db_path = tmp_path / ".photo_organizer" / "library.db"
    with Database(db_path) as db:
        folder = str(tmp_path / "DCIM" / "100EOS5D")
        jpg = _write_jpeg(folder + r"\IMG_0001.jpg")
        _add_file(
            db, jpg,
            datetime_original="2012:09:08 10:00:00", date_confidence="HIGH",
            file_type="CAMERA_JPEG",
        )
        httpd = serve(db, port=0, open_browser=False, background=True)
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/"
            body = urllib.request.urlopen(url).read().decode()
            assert 'src="/thumb/' in body
        finally:
            httpd.shutdown()
