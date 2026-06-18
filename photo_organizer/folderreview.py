"""
folderreview.py — HTML side-by-side folder-overlap review

Local web UI for reviewing twin-folder pairs stored in `folder_overlaps`
(produced by `folder-merge`). Each pair is shown as two columns; the user
picks keep-A / keep-B / keep-both and saves the decision. Nothing is ever
auto-deleted — decisions are written to `folder_overlaps.status='reviewed'`
and acted on later by `plan` (S2c).

Decision seam: POST /folder-decision → db.record_folder_overlap_decision().
Decision model: keeper='a'/'b' means fold the other side; keeper=NULL means
keep both (no deletions). `plan` reads these decisions in S2c.
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
from .progress import console, print_phase_header, print_success

_MAX_POST_BYTES = 64 * 1024
_MAX_BATCH_BYTES = 16 * 1024 * 1024
_SAMPLE_LIMIT = 8  # max filenames shown per folder side


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
.pair { border: 1px solid #2b2f38; border-radius: 10px; padding: .8rem .9rem;
        margin: 0 0 1.1rem; background: #1c1f26; }
.pair.done { opacity: .55; }
.pair.done:hover { opacity: 1; }
.phead { display: flex; align-items: center; gap: .8rem; margin-bottom: .7rem;
         font-size: .9rem; color: #aab2c0; }
.phead .tag { background: #2b2f38; border-radius: 6px; padding: .1rem .5rem; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin-bottom: .6rem; }
.col { border: 2px solid #333a45; border-radius: 8px; padding: .65rem .8rem;
       cursor: pointer; transition: border-color .12s, opacity .12s; }
.col.selected { border-color: #3fb950; background: #112218; }
.col.both { border-color: #58a6ff; background: #0d2038; }
.col.dimmed { opacity: .42; }
.col .path { font-size: .8rem; word-break: break-all; font-family: monospace;
             margin-bottom: .4rem; color: #c9d1d9; }
.col .stats { font-size: .78rem; color: #8b93a1; margin-bottom: .35rem; }
.badge-keeper { display: inline-block; background: #143d1d; color: #6fdc8c;
                font-size: .7rem; border-radius: 4px; padding: 0 .4rem; margin-bottom: .3rem; }
.samples { margin: .3rem 0 0 0; padding: 0 0 0 1rem; font-size: .72rem; color: #7d8694; }
.samples li { list-style: disc; margin: .1rem 0; }
.controls { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; margin-top: .5rem; }
.controls button { background: #2b2f38; color: #e6e6e6; border: 1px solid #3a404b;
                   border-radius: 6px; padding: .25rem .7rem; cursor: pointer; font-size: .8rem; }
.controls button:hover { background: #353b46; }
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
summary.ghead .gpaths .arrow { color: #8b93a1; margin: 0 .3rem; }
summary.ghead .gcount { color: #8b93a1; font-size: .78rem; margin-top: .25rem; }
details.group[open] summary.ghead { border-radius: 8px 8px 0 0; margin-bottom: .6rem; }
"""

# -----------------------------------------------------------------------
# Inline JS
# -----------------------------------------------------------------------

_PAGE_JS = """
function setSaved(oid, saved) {
  const el = document.getElementById('p' + oid);
  el.classList.toggle('done', saved);
  el.dataset.saved = saved ? '1' : '';
  const s = el.querySelector('.saved');
  s.textContent = saved ? '\\u2713 saved' : '';
  s.className = 'saved';
  el.querySelector('.savebtn').textContent = saved ? '\\u21a9 un-save' : 'save decision';
}
function setDirty(oid) {
  const el = document.getElementById('p' + oid);
  if (el.dataset.saved) {
    el.classList.remove('done');
    el.dataset.saved = '';
    const s = el.querySelector('.saved');
    s.textContent = '\\u25cf unsaved changes';
    s.className = 'saved dirty';
    el.querySelector('.savebtn').textContent = 'save decision';
  }
}
function selectSide(oid, side) {
  const colA = document.getElementById('col_a_' + oid);
  const colB = document.getElementById('col_b_' + oid);
  [colA, colB].forEach(c => c.classList.remove('selected', 'both', 'dimmed'));
  if (side === 'both') {
    colA.classList.add('both');
    colB.classList.add('both');
  } else {
    const kept = side === 'a' ? colA : colB;
    const drop = side === 'a' ? colB : colA;
    kept.classList.add('selected');
    drop.classList.add('dimmed');
  }
  document.getElementById('p' + oid).dataset.choice = side;
  setDirty(oid);
}
function save(oid) {
  const el = document.getElementById('p' + oid);
  const decision = el.dataset.choice || 'both';
  fetch('/folder-decision', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({overlap_id: oid, decision: decision})
  }).then(r => { if (r.ok) setSaved(oid, true); });
}
function undo(oid) {
  fetch('/folder-undecision', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({overlap_id: oid})
  }).then(r => { if (r.ok) setSaved(oid, false); });
}
function saveOrUndo(oid) {
  const el = document.getElementById('p' + oid);
  if (el.dataset.saved) { undo(oid); } else { save(oid); }
}
function saveAll() {
  const pairs = document.querySelectorAll('.pair');
  const decisions = [];
  pairs.forEach(el => {
    decisions.push({
      overlap_id: parseInt(el.dataset.oid, 10),
      decision: el.dataset.choice || 'both'
    });
  });
  const msg = document.getElementById('allmsg');
  msg.textContent = 'saving ' + decisions.length + ' pair(s)\\u2026';
  fetch('/folder-decision-all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({decisions: decisions})
  }).then(r => r.json()).then(j => {
    if (j.ok) {
      pairs.forEach(el => setSaved(parseInt(el.dataset.oid, 10), true));
      msg.textContent = '\\u2713 saved all ' + j.saved + ' pair(s)';
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

class FolderReviewState:
    """Loaded once at server start: all overlap rows + sample filenames."""

    def __init__(self, db: Database) -> None:
        # Load all pairs (pending + reviewed) so done pairs are shown dimmed.
        self.overlaps = [dict(r) for r in db.iter_folder_overlaps(pending_only=False)]

        # Build folder → up-to-8 sample filenames in one pass over files.
        needed = set()
        for row in self.overlaps:
            needed.add(row["folder_a"])
            needed.add(row["folder_b"])

        samples: dict[str, list[str]] = defaultdict(list)
        if needed:
            for r in db.conn.execute(
                "SELECT path, filename FROM files "
                "WHERE status != 'error' ORDER BY path"
            ):
                # Walk from direct parent up to drive root so that rolled-up
                # ancestor overlap pairs (where files live in subfolders) also
                # get sample filenames, not just direct-child pairs.
                cur = str(PureWindowsPath(r["path"]).parent)
                while True:
                    if cur in needed and len(samples[cur]) < _SAMPLE_LIMIT:
                        samples[cur].append(r["filename"])
                    up = str(PureWindowsPath(cur).parent)
                    if up == cur:
                        break
                    cur = up

        self.db = db
        self.samples: dict[str, list[str]] = dict(samples)
        self.lock = threading.Lock()
        self.groups: list[dict] = self._build_groups()

    def _build_groups(self) -> list[dict]:
        """Group overlap rows by their parent-folder pair so the UI can
        collapse twin-subfolder runs (e.g. .../Album/2003, 2004, 2005) into
        one section."""
        by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in self.overlaps:
            key = (
                str(PureWindowsPath(row["folder_a"]).parent),
                str(PureWindowsPath(row["folder_b"]).parent),
            )
            by_key[key].append(row)

        groups = []
        for (ga, gb), rows in by_key.items():
            rows = sorted(rows, key=lambda r: r["folder_a"])
            groups.append({
                "parent_a": ga,
                "parent_b": gb,
                "rows": rows,
                "shared_total": sum(r["shared_count"] for r in rows),
                "pending": sum(1 for r in rows if r["status"] != "reviewed"),
            })
        groups.sort(key=lambda g: (g["parent_a"], g["parent_b"]))
        return groups

    def total(self) -> int:
        return len(self.overlaps)


# -----------------------------------------------------------------------
# HTML rendering
# -----------------------------------------------------------------------

def _col_html(oid: int, side: str, folder: str, total_files: int,
              only_count: int, coverage: float, is_suggested_keeper: bool,
              choice: str, samples: list[str]) -> str:
    """Render one folder column. `choice` is 'a', 'b', or 'both'."""
    if choice == "both":
        cls = "both"
    elif choice == side:
        cls = "selected"
    else:
        cls = "dimmed"

    badge = ('<span class="badge-keeper">&#9733; suggested keeper</span><br>'
             if is_suggested_keeper else "")
    items = "".join(f"<li>{html.escape(f)}</li>" for f in samples)
    sample_html = f'<ul class="samples">{items}</ul>' if items else ""

    return (
        f'<div class="col {cls}" id="col_{side}_{oid}" '
        f'onclick="selectSide({oid}, \'{side}\')">'
        f'<div class="path">{html.escape(folder)}</div>'
        f'{badge}'
        f'<div class="stats">'
        f'{total_files:,} files &middot; {only_count:,} unique'
        f' &middot; coverage {coverage:.0%}'
        f'</div>'
        f'{sample_html}'
        f'</div>'
    )


def _pair_html(state: FolderReviewState, i: int, row: dict) -> str:
    oid = row["overlap_id"]
    is_done = row["status"] == "reviewed"

    # Initial/current choice: use DB keeper if present, else 'both'.
    k = row.get("keeper")
    choice = k if k in ("a", "b") else "both"

    total_a = row["shared_count"] + row["a_only_count"]
    total_b = row["shared_count"] + row["b_only_count"]

    col_a = _col_html(
        oid, "a", row["folder_a"], total_a, row["a_only_count"],
        row["coverage_a"], row.get("keeper") == "a", choice,
        state.samples.get(row["folder_a"], []),
    )
    col_b = _col_html(
        oid, "b", row["folder_b"], total_b, row["b_only_count"],
        row["coverage_b"], row.get("keeper") == "b", choice,
        state.samples.get(row["folder_b"], []),
    )

    done_cls = " done" if is_done else ""
    saved_mark = ' data-saved="1"' if is_done else ""
    saved_txt = "&#10003; saved" if is_done else ""
    savebtn_txt = "&#8617; un-save" if is_done else "save decision"

    return (
        f'<div class="pair{done_cls}" id="p{oid}" '
        f'data-oid="{oid}" data-choice="{choice}"{saved_mark}>'
        f'<div class="phead">'
        f'<span class="tag">#{i + 1}</span>'
        f'<span>{row["shared_count"]:,} shared</span>'
        f'<span class="tag">'
        f'A:{row["a_only_count"]:,} unique | B:{row["b_only_count"]:,} unique'
        f'</span>'
        f'</div>'
        f'<div class="cols">{col_a}{col_b}</div>'
        f'<div class="controls">'
        f'<button onclick="selectSide({oid},\'a\')">Keep A</button> '
        f'<button onclick="selectSide({oid},\'b\')">Keep B</button> '
        f'<button onclick="selectSide({oid},\'both\')">Keep Both</button> '
        f'<button class="savebtn" onclick="saveOrUndo({oid})">{savebtn_txt}</button>'
        f'<span class="saved">{saved_txt}</span>'
        f'</div></div>'
    )


def _group_html(state: FolderReviewState, group: dict, start_index: int) -> str:
    pending = group["pending"]
    count_txt = (
        f'{len(group["rows"]):,} twin subfolders &middot; '
        f'{group["shared_total"]:,} shared &middot; '
        + ("all reviewed" if pending == 0 else f"{pending:,} pending")
    )
    pairs_html = "".join(
        _pair_html(state, start_index + j, row) for j, row in enumerate(group["rows"])
    )
    open_attr = " open" if pending > 0 else ""
    return (
        f'<details class="group"{open_attr}>'
        f'<summary class="ghead">'
        f'<span class="gicon">&#128193;</span>'
        f'<span class="gpaths">{html.escape(group["parent_a"])}'
        f'<span class="arrow">&harr;</span>{html.escape(group["parent_b"])}</span>'
        f'<div class="gcount">{count_txt}</div>'
        f'</summary>{pairs_html}</details>'
    )


def _render_page(state: FolderReviewState) -> bytes:
    total = state.total()
    if total == 0:
        body = '<p class="sub">No twin-folder pairs found. Run folder-merge first.</p>'
        actionbar = ""
    else:
        chunks = []
        idx = 0
        for group in state.groups:
            chunks.append(_group_html(state, group, idx))
            idx += len(group["rows"])
        body = "".join(chunks)
        actionbar = (
            f'<div class="actionbar">'
            f'<button class="primary" onclick="saveAll()">Save all {total} decisions</button>'
            '<button class="ghostbtn" onclick="expandAll()">Expand all</button>'
            '<button class="ghostbtn" onclick="collapseAll()">Collapse all</button>'
            '<span id="allmsg" class="allmsg"></span></div>'
        )
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Folder overlap review</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<h1>Twin-folder overlap review</h1>"
        '<p class="sub">Click a column to select the keeper, then '
        '"save decision". Green = keep, dim = fold. '
        "Nothing is deleted here &mdash; <b>plan</b> stages the drops you confirm.</p>"
        f"{actionbar}{body}"
        f"<script>{_PAGE_JS}</script></body></html>"
    )
    return doc.encode("utf-8")


# -----------------------------------------------------------------------
# HTTP handler
# -----------------------------------------------------------------------

_VALID_PATHS = frozenset(
    ("/folder-decision", "/folder-undecision", "/folder-decision-all")
)


def _make_handler(state: FolderReviewState):
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
            else:
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
                cap = _MAX_BATCH_BYTES if self.path == "/folder-decision-all" else _MAX_POST_BYTES
                length = min(int(self.headers.get("Content-Length", 0)), cap)
                payload = json.loads(self.rfile.read(length) or b"{}")

                if self.path == "/folder-undecision":
                    oid = int(payload["overlap_id"])
                    with state.lock:
                        state.db.reopen_folder_overlap(oid)
                        state.db.commit()
                    self._send(200, b'{"ok":true}', "application/json")
                    return

                if self.path == "/folder-decision-all":
                    saved = 0
                    now = _now()
                    with state.lock:
                        for d in payload.get("decisions", []):
                            oid = int(d["overlap_id"])
                            dec = str(d.get("decision", "both"))
                            keeper = dec if dec in ("a", "b") else None
                            state.db.record_folder_overlap_decision(oid, keeper, now)
                            saved += 1
                        state.db.commit()
                    self._send(
                        200,
                        json.dumps({"ok": True, "saved": saved}).encode(),
                        "application/json",
                    )
                    return

                # /folder-decision
                oid = int(payload["overlap_id"])
                dec = str(payload.get("decision", "both"))
                keeper = dec if dec in ("a", "b") else None
                now = _now()
                with state.lock:
                    state.db.record_folder_overlap_decision(oid, keeper, now)
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
    """Start the local folder-overlap review server.

    Binds 127.0.0.1 only (single local user, no auth). With background=False
    (the CLI default) this blocks until Ctrl-C. With background=True (tests)
    it returns the running server immediately; call .shutdown() to stop it.
    """
    state = FolderReviewState(db)
    httpd = ThreadingHTTPServer((host, port), _make_handler(state))
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"

    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    print_phase_header("folder review", "Twin-Folder Overlap Review")
    print_success(
        f"Serving {state.total():,} pair(s) at [bold]{url}[/bold]\n"
        "  Click columns in your browser to pick a direction; decisions save "
        "live to the DB. Press Ctrl-C here when done."
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
