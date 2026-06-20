"""
folderorganize.py — HTML review UI for no-event / low-confidence-date folders

Local web UI (`review --organize`) for assigning an event name and/or a date
to source folders that need attention: folders whose name carries no usable
event label, or folders containing LOW/unknown-confidence dates. Decisions
are written to `folder_overrides` (keyed by the file's IMMEDIATE PARENT
folder — same key `plan` looks up) and never auto-applied to files here;
`plan` (O2) is what actually consults them.

O3b adds a contact-sheet thumbnail row per candidate folder, reusing
`webreview.ThumbCache` (same lazy-generate-and-cache-by-file_id scheme as the
near-dupe review UI) keyed off `self.meta` already populated below.

Decision seam: POST /folder-override → db.set_folder_override().
"""

from __future__ import annotations

import html
import json
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PureWindowsPath

from .db import Database
from .planner import _is_unorganised_folder_name, _parse_exif_dt, _sanitize_event
from .progress import console, print_phase_header, print_success
from .webreview import ThumbCache

_MAX_POST_BYTES = 64 * 1024
_MAX_BATCH_BYTES = 16 * 1024 * 1024
_SAMPLE_LIMIT = 8  # max filenames shown per candidate folder
_THUMB_SAMPLE_LIMIT = 6  # max thumbnails shown per candidate folder
# Pillow-openable image types (RAW/.CR2/.ARW and VIDEO are not reliably openable).
_THUMBNAILABLE_TYPES = frozenset(("CAMERA_JPEG", "DEV_JPEG", "HEIC"))

_LOW_CONFIDENCE = ("LOW", None)
_CONFIDENT = ("HIGH", "MEDIUM")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------------------------------------------------
# Inline CSS
# -----------------------------------------------------------------------

_PAGE_CSS = """
:root { color-scheme: dark; }
body { font-family: system-ui, sans-serif; background: #16181d; color: #e6e6e6;
       margin: 0; padding: 1.2rem 1.4rem 6rem; }
h1 { font-size: 1.25rem; margin: 0 0 .2rem; }
.sub { color: #8b93a1; font-size: .85rem; margin: 0 0 1.4rem; }
.card { border: 1px solid #2b2f38; border-radius: 10px; padding: .8rem .9rem;
        margin: 0 0 1.1rem; background: #1c1f26; }
.card.done { opacity: .6; }
.card.done:hover { opacity: 1; }
.chead { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
         margin-bottom: .5rem; font-size: .9rem; color: #aab2c0; }
.chead .path { font-family: monospace; font-size: .85rem; color: #c9d1d9;
               word-break: break-all; }
.chead .tag { background: #2b2f38; border-radius: 6px; padding: .1rem .5rem;
              font-size: .76rem; }
.chead .flag { background: #3d2a14; color: #e3a85b; border-radius: 6px;
               padding: .1rem .5rem; font-size: .76rem; }
.stats { font-size: .78rem; color: #8b93a1; margin-bottom: .5rem; }
.thumbs { display: flex; flex-wrap: wrap; gap: .35rem; margin: .3rem 0 .5rem; }
.thumb { height: 96px; width: auto; border-radius: 6px; border: 1px solid #2b2f38;
         object-fit: cover; }
.samples { margin: .3rem 0 .6rem 0; padding: 0 0 0 1rem; font-size: .72rem; color: #7d8694; }
.samples li { list-style: disc; margin: .1rem 0; }
.inputs { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; margin-top: .4rem; }
.inputs label { font-size: .78rem; color: #8b93a1; }
.inputs input[type=text] { background: #11141a; border: 1px solid #3a404b; color: #e6e6e6;
       border-radius: 6px; padding: .3rem .5rem; font-size: .85rem; }
.inputs input.ev { width: 14rem; }
.inputs input.dt { width: 9rem; font-family: monospace; }
.inputs button { background: #2b2f38; color: #e6e6e6; border: 1px solid #3a404b;
       border-radius: 6px; padding: .3rem .8rem; cursor: pointer; font-size: .8rem; }
.inputs button:hover { background: #353b46; }
.inputs button.savebtn { background: #238636; border-color: #2ea043; color: #fff; }
.inputs button.savebtn:hover { background: #2ea043; }
.saved { color: #3fb950; font-size: .78rem; }
.saved.dirty { color: #d29922; }
.actionbar { position: sticky; top: 0; z-index: 10; background: #16181dee;
             backdrop-filter: blur(4px); padding: .6rem 0 .7rem; margin: 0 0 1rem;
             border-bottom: 1px solid #2b2f38; display: flex; align-items: center; gap: .8rem; }
.actionbar button.primary { background: #238636; color: #fff; border: 1px solid #2ea043;
             border-radius: 6px; padding: .4rem 1rem; cursor: pointer; font-size: .85rem; }
.actionbar button.primary:hover { background: #2ea043; }
.actionbar button.ghostbtn { background: #2b2f38; color: #e6e6e6; border: 1px solid #3a404b;
             border-radius: 6px; padding: .4rem .8rem; cursor: pointer; font-size: .8rem; }
.actionbar button.ghostbtn:hover { background: #353b46; }
.allmsg { color: #8b93a1; font-size: .82rem; }
details.group { margin: 0 0 1.1rem; }
summary.ghead { background: #21262d; border: 1px solid #2b2f38; border-radius: 8px;
                padding: .55rem .8rem; cursor: pointer; list-style: none; }
summary.ghead::-webkit-details-marker { display: none; }
summary.ghead .gicon { margin-right: .4rem; }
summary.ghead .gpaths { font-family: monospace; font-size: .82rem; color: #c9d1d9;
                         word-break: break-all; }
summary.ghead .gcount { color: #8b93a1; font-size: .78rem; margin-top: .25rem; }
details.group[open] summary.ghead { border-radius: 8px 8px 0 0; margin-bottom: .6rem; }
"""

# -----------------------------------------------------------------------
# Inline JS
# -----------------------------------------------------------------------

_PAGE_JS = """
function cardEl(folder) {
  return document.querySelector('.card[data-folder="' + CSS.escape(folder) + '"]');
}
function setSaved(folder, saved) {
  const el = cardEl(folder);
  el.classList.toggle('done', saved);
  el.dataset.saved = saved ? '1' : '';
  const s = el.querySelector('.saved');
  s.textContent = saved ? '\\u2713 saved' : '';
  s.className = 'saved';
}
function setDirty(folder) {
  const el = cardEl(folder);
  if (el.dataset.saved) {
    el.classList.remove('done');
    el.dataset.saved = '';
  }
  const s = el.querySelector('.saved');
  s.textContent = '\\u25cf unsaved changes';
  s.className = 'saved dirty';
}
function readCard(folder) {
  const el = cardEl(folder);
  return {
    source_folder: folder,
    event_name: el.querySelector('.ev').value.trim(),
    date_override: el.querySelector('.dt').value.trim(),
  };
}
function saveFolder(folder) {
  const payload = readCard(folder);
  fetch('/folder-override', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(r => { if (r.ok) setSaved(folder, true); });
}
function clearFolder(folder) {
  const el = cardEl(folder);
  el.querySelector('.ev').value = '';
  el.querySelector('.dt').value = '';
  fetch('/folder-override-clear', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({source_folder: folder})
  }).then(r => { if (r.ok) setSaved(folder, false); });
}
function saveAll() {
  const cards = document.querySelectorAll('.card');
  const overrides = [];
  cards.forEach(el => overrides.push(readCard(el.dataset.folder)));
  const msg = document.getElementById('allmsg');
  msg.textContent = 'saving ' + overrides.length + ' folder(s)\\u2026';
  fetch('/folder-override-all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({overrides: overrides})
  }).then(r => r.json()).then(j => {
    if (j.ok) {
      cards.forEach(el => setSaved(el.dataset.folder, true));
      msg.textContent = '\\u2713 saved all ' + j.saved + ' folder(s)';
    } else {
      msg.textContent = 'error: ' + (j.error || 'failed');
    }
  }).catch(() => { msg.textContent = 'error saving'; });
}
function expandAll() { document.querySelectorAll('details.group').forEach(d => d.open = true); }
function collapseAll() { document.querySelectorAll('details.group').forEach(d => d.open = false); }
"""


# -----------------------------------------------------------------------
# Server state
# -----------------------------------------------------------------------

class FolderOrganizeState:
    """Loaded once at server start: candidate folders needing attention."""

    def __init__(self, db: Database) -> None:
        staged = {
            row[0] for row in
            db.conn.execute("SELECT file_id FROM operations WHERE op_type='STAGE_DELETE'")
        }

        by_folder: dict[str, list[dict]] = defaultdict(list)
        for r in db.conn.execute(
            "SELECT file_id, path, filename, date_confidence, datetime_original, "
            "file_type FROM files WHERE status != 'error'"
        ):
            if r["file_id"] in staged:
                continue
            folder = str(PureWindowsPath(r["path"]).parent)
            by_folder[folder].append(dict(r))

        overrides = db.get_folder_overrides()

        meta: dict[int, dict] = {}
        folders: list[dict] = []
        for folder, rows in by_folder.items():
            name = PureWindowsPath(folder).name
            is_no_event = _is_unorganised_folder_name(name) or not _sanitize_event(name)
            low_date_count = sum(
                1 for r in rows if r["date_confidence"] in _LOW_CONFIDENCE
            )

            if not (is_no_event or low_date_count > 0):
                continue

            confident_dates = []
            for r in rows:
                if r["date_confidence"] in _CONFIDENT:
                    dt = _parse_exif_dt(r["datetime_original"])
                    if dt is not None:
                        confident_dates.append(dt)
            date_lo = min(confident_dates).strftime("%Y-%m-%d") if confident_dates else None
            date_hi = max(confident_dates).strftime("%Y-%m-%d") if confident_dates else None

            samples = [r["filename"] for r in rows[:_SAMPLE_LIMIT]]

            # Sample file_ids of Pillow-openable images for thumbnail rendering.
            # RAW/.CR2/.ARW and VIDEO stay out of sample_fids (not reliably
            # openable) but remain in the filename `samples` list above.
            sample_fids = [
                r["file_id"] for r in rows
                if r["file_type"] in _THUMBNAILABLE_TYPES
            ][:_THUMB_SAMPLE_LIMIT]

            for r in rows:
                meta[r["file_id"]] = {"path": r["path"]}

            ov = overrides.get(folder)
            folders.append({
                "folder": folder,
                "name": name,
                "count": len(rows),
                "low_date_count": low_date_count,
                "is_no_event": is_no_event,
                "date_lo": date_lo,
                "date_hi": date_hi,
                "samples": samples,
                "sample_fids": sample_fids,
                "override": {
                    "event_name": ov["event_name"] if ov else None,
                    "date_override": ov["date_override"] if ov else None,
                } if ov else None,
            })

        self.db = db
        self.meta = meta
        self.folders = folders
        self.thumbs = ThumbCache(db.path.parent / "_staging" / ".thumbs")
        self.lock = threading.Lock()
        self.groups: list[dict] = self._build_groups()

    def _build_groups(self) -> list[dict]:
        """Group candidate folders by their parent ("mother") folder so runs
        of sibling candidate subfolders collapse into one section."""
        by_parent: dict[str, list[dict]] = defaultdict(list)
        for row in self.folders:
            parent = str(PureWindowsPath(row["folder"]).parent)
            by_parent[parent].append(row)

        groups = []
        for parent, rows in by_parent.items():
            rows = sorted(rows, key=lambda r: r["folder"])
            groups.append({
                "parent": parent,
                "rows": rows,
                "pending": sum(1 for r in rows if not r["override"]),
            })
        groups.sort(key=lambda g: g["parent"])
        return groups

    def total(self) -> int:
        return len(self.folders)


# -----------------------------------------------------------------------
# HTML rendering
# -----------------------------------------------------------------------

def _card_html(row: dict) -> str:
    folder = row["folder"]
    ov = row["override"]
    has_override = ov is not None
    ev_val = html.escape(ov["event_name"] or "", quote=True) if ov else ""
    dt_val = html.escape(ov["date_override"] or "", quote=True) if ov else ""

    flags = []
    if row["is_no_event"]:
        flags.append('<span class="flag">&#9888; no event name</span>')
    if row["low_date_count"]:
        flags.append(f'<span class="flag">&#9888; {row["low_date_count"]:,} low-date</span>')
    flags_html = " ".join(flags)

    if row["date_lo"] and row["date_hi"]:
        date_range = f'{row["date_lo"]} &hellip; {row["date_hi"]}' if row["date_lo"] != row["date_hi"] else row["date_lo"]
    else:
        date_range = "no confident date"

    thumb_items = "".join(
        f'<img class="thumb" loading="lazy" src="/thumb/{fid}" alt="">'
        for fid in row["sample_fids"]
    )
    thumbs_html = f'<div class="thumbs">{thumb_items}</div>' if thumb_items else ""

    items = "".join(f"<li>{html.escape(f)}</li>" for f in row["samples"])
    sample_html = f'<ul class="samples">{items}</ul>' if items else ""

    done_cls = " done" if has_override else ""
    saved_mark = ' data-saved="1"' if has_override else ""
    saved_txt = "&#10003; saved" if has_override else ""

    folder_attr = html.escape(folder, quote=True)

    return (
        f'<div class="card{done_cls}" data-folder="{folder_attr}"{saved_mark}>'
        f'<div class="chead">'
        f'<span class="path">{html.escape(folder)}</span>'
        f'<span class="tag">{row["count"]:,} files</span>'
        f'{flags_html}'
        f'</div>'
        f'<div class="stats">date range: {date_range}</div>'
        f'{thumbs_html}'
        f'{sample_html}'
        f'<div class="inputs">'
        f'<label>Event name</label>'
        f'<input type="text" class="ev" value="{ev_val}" placeholder="e.g. Kyoto">'
        f'<label>Date</label>'
        f'<input type="text" class="dt" value="{dt_val}" placeholder="YYYY-MM-DD">'
        f'<button class="savebtn" onclick="saveFolder(cardEl0(this))">save</button>'
        f'<button onclick="clearFolder(cardEl0(this))">clear</button>'
        f'<span class="saved">{saved_txt}</span>'
        f'</div></div>'
    )


def _group_html(group: dict) -> str:
    pending = group["pending"]
    count_txt = (
        f'{len(group["rows"]):,} folder(s) &middot; '
        + ("all set" if pending == 0 else f"{pending:,} need attention")
    )
    cards_html = "".join(_card_html(row) for row in group["rows"])
    open_attr = " open" if pending > 0 else ""
    return (
        f'<details class="group"{open_attr}>'
        f'<summary class="ghead">'
        f'<span class="gicon">&#128193;</span>'
        f'<span class="gpaths">{html.escape(group["parent"])}</span>'
        f'<div class="gcount">{count_txt}</div>'
        f'</summary>{cards_html}</details>'
    )


def _render_page(state: FolderOrganizeState) -> bytes:
    total = state.total()
    if total == 0:
        body = '<p class="sub">No folders need attention. Run plan to see the result.</p>'
        actionbar = ""
    else:
        body = "".join(_group_html(group) for group in state.groups)
        actionbar = (
            f'<div class="actionbar">'
            f'<button class="primary" onclick="saveAll()">Save all {total} folder(s)</button>'
            '<button class="ghostbtn" onclick="expandAll()">Expand all</button>'
            '<button class="ghostbtn" onclick="collapseAll()">Collapse all</button>'
            '<span id="allmsg" class="allmsg"></span></div>'
        )
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Folder organize review</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<h1>No-event / low-confidence-date folder review</h1>"
        '<p class="sub">Assign an event name and/or a date to folders below. '
        'Nothing is moved here &mdash; <b>plan</b> consults these overrides when '
        "it builds the move plan.</p>"
        f"{actionbar}{body}"
        "<script>"
        "function cardEl0(btn){return btn.closest('.card').dataset.folder;}"
        f"{_PAGE_JS}"
        "</script></body></html>"
    )
    return doc.encode("utf-8")


# -----------------------------------------------------------------------
# HTTP handler
# -----------------------------------------------------------------------

_VALID_PATHS = frozenset(
    ("/folder-override", "/folder-override-clear", "/folder-override-all")
)


def _make_handler(state: FolderOrganizeState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, _render_page(state))
                return
            if self.path.startswith("/thumb/"):
                try:
                    fid = int(self.path.rsplit("/", 1)[-1].split(".")[0])
                    tp, _ = state.thumbs.ensure(fid, state.meta[fid]["path"])
                    self._send(200, tp.read_bytes(), "image/jpeg")
                except (KeyError, ValueError, OSError):
                    self._send(404, b"not found", "text/plain")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path not in _VALID_PATHS:
                self._send(404, b"not found", "text/plain")
                return
            host = self.headers.get("Host", "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, b"forbidden", "text/plain")
                return
            try:
                cap = _MAX_BATCH_BYTES if self.path == "/folder-override-all" else _MAX_POST_BYTES
                length = min(int(self.headers.get("Content-Length", 0)), cap)
                payload = json.loads(self.rfile.read(length) or b"{}")

                if self.path == "/folder-override-clear":
                    source_folder = str(payload["source_folder"])
                    with state.lock:
                        try:
                            state.db.clear_folder_override(source_folder)
                        except KeyError:
                            pass
                        state.db.commit()
                    self._send(200, b'{"ok":true}', "application/json")
                    return

                if self.path == "/folder-override-all":
                    saved = 0
                    now = _now()
                    with state.lock:
                        for d in payload.get("overrides", []):
                            source_folder = str(d["source_folder"])
                            ev = (d.get("event_name") or "").strip() or None
                            dt = (d.get("date_override") or "").strip() or None
                            state.db.set_folder_override(
                                source_folder, event_name=ev, date_override=dt,
                                note=None, updated_at=now,
                            )
                            saved += 1
                        state.db.commit()
                    self._send(
                        200,
                        json.dumps({"ok": True, "saved": saved}).encode(),
                        "application/json",
                    )
                    return

                # /folder-override
                source_folder = str(payload["source_folder"])
                ev = (payload.get("event_name") or "").strip() or None
                dt = (payload.get("date_override") or "").strip() or None
                now = _now()
                with state.lock:
                    state.db.set_folder_override(
                        source_folder, event_name=ev, date_override=dt,
                        note=None, updated_at=now,
                    )
                    state.db.commit()
                self._send(200, b'{"ok":true}', "application/json")

            except (KeyError, ValueError, TypeError) as exc:
                self._send(
                    400,
                    json.dumps({"error": str(exc)}).encode(),
                    "application/json",
                )

    return Handler


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

def serve(
    db: Database,
    port: int = 0,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    background: bool = False,
) -> ThreadingHTTPServer:
    """Start the local folder-organize review server.

    Binds 127.0.0.1 only (single local user, no auth). With background=False
    (the CLI default) this blocks until Ctrl-C. With background=True (tests)
    it returns the running server immediately; call .shutdown() to stop it.
    """
    state = FolderOrganizeState(db)
    httpd = ThreadingHTTPServer((host, port), _make_handler(state))
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"

    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    print_phase_header("folder organize", "No-Event / Low-Confidence-Date Folder Review")
    print_success(
        f"Serving {state.total():,} folder(s) at [bold]{url}[/bold]\n"
        "  Assign event names / dates in your browser; decisions save live "
        "to the DB. Press Ctrl-C here when done."
    )
    if open_browser:
        try:
            webbrowser.open(url)
        except OSError:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped — decisions saved.[/yellow]")
    finally:
        httpd.shutdown()
    return httpd
