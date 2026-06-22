# Folder-Merge S2b: Web Review UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `review --web --folders` — a local HTML side-by-side UI for reviewing twin-folder pairs stored in `folder_overlaps`, letting the user pick a merge direction (keep A / keep B / keep both) and recording the decision back to the DB.

**Architecture:** A new `folderreview.py` module (parallel to `webreview.py`) serves a single-page HTML contact-sheet of twin-folder pairs. Each pair is shown as two columns (folder A | folder B) with path, file counts, coverage %, and sample filenames. The user clicks a column (or "Keep Both") to pre-select a direction, then presses "Save Decision" to POST the choice to the local server. The server writes `status='reviewed'` + `keeper` into `folder_overlaps`; `plan` (Plan 2c) later derives the actual STAGE_DELETE operations. Nothing is deleted here — decisions are reversible via "un-save".

**Tech Stack:** Python stdlib `http.server.ThreadingHTTPServer`, `threading`, `json`; Pillow NOT needed (no thumbnails); existing `db.py` helpers; `progress.py` console output; inline CSS/JS (dark theme, mirrors `webreview.py`).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `photo_organizer/db.py` | Add `record_folder_overlap_decision()`, `reopen_folder_overlap()` |
| Create | `photo_organizer/folderreview.py` | State, HTML rendering, HTTP server |
| Modify | `photo_organizer/__main__.py` | `--folders` flag on `review`; dispatch to `folderreview.serve()` |
| Create | `tests/test_folder_review.py` | 6 tests (DB methods, GET, POST decision/undecision/all, CLI wiring) |

---

## Task 1: DB helper methods

**Files:**
- Modify: `photo_organizer/db.py` (after `iter_folder_overlaps`, ~line 731)
- Test: `tests/test_folder_review.py` (create)

- [ ] **Step 1: Write the two failing tests**

Create `tests/test_folder_review.py`:

```python
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
```

- [ ] **Step 2: Run to confirm FAIL**

```
python -m pytest tests/test_folder_review.py::test_record_folder_overlap_decision tests/test_folder_review.py::test_reopen_folder_overlap -v
```
Expected: `AttributeError: 'Database' object has no attribute 'record_folder_overlap_decision'`

- [ ] **Step 3: Add methods to `db.py`**

Open `photo_organizer/db.py`. After the `iter_folder_overlaps` method (around line 731), add:

```python
    def record_folder_overlap_decision(self, overlap_id: int, keeper, reviewed_at: str) -> None:
        self.conn.execute(
            "UPDATE folder_overlaps SET status='reviewed', keeper=?, reviewed_at=? "
            "WHERE overlap_id=?",
            (keeper, reviewed_at, overlap_id),
        )

    def reopen_folder_overlap(self, overlap_id: int) -> None:
        self.conn.execute(
            "UPDATE folder_overlaps SET status='pending', reviewed_at=NULL "
            "WHERE overlap_id=?",
            (overlap_id,),
        )
```

- [ ] **Step 4: Run to confirm PASS**

```
python -m pytest tests/test_folder_review.py::test_record_folder_overlap_decision tests/test_folder_review.py::test_reopen_folder_overlap -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```
git add photo_organizer/db.py tests/test_folder_review.py
git commit -m "feat(folder-merge): DB helpers for folder overlap decisions (S2b step 1)"
```

---

## Task 2: `folderreview.py` — State + HTML rendering

**Files:**
- Create: `photo_organizer/folderreview.py`
- Test: `tests/test_folder_review.py` (add `test_serve_get_renders_pair`)

- [ ] **Step 1: Write the failing GET test**

Append to `tests/test_folder_review.py`:

```python
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
```

- [ ] **Step 2: Run to confirm FAIL**

```
python -m pytest tests/test_folder_review.py::test_serve_get_renders_pair -v
```
Expected: `ModuleNotFoundError: No module named 'photo_organizer.folderreview'`

- [ ] **Step 3: Create `photo_organizer/folderreview.py`**

```python
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
.allmsg { color: #8b93a1; font-size: .82rem; }
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
                parent = str(PureWindowsPath(r["path"]).parent)
                if parent in needed and len(samples[parent]) < _SAMPLE_LIMIT:
                    samples[parent].append(r["filename"])

        self.db = db
        self.samples: dict[str, list[str]] = dict(samples)
        self.lock = threading.Lock()

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

    badge = ('<span class="badge-keeper">★ suggested keeper</span><br>'
             if is_suggested_keeper else "")
    items = "".join(f"<li>{html.escape(f)}</li>" for f in samples)
    sample_html = f'<ul class="samples">{items}</ul>' if items else ""

    return (
        f'<div class="col {cls}" id="col_{side}_{oid}" '
        f'onclick="selectSide({oid}, \'{side}\')">'
        f'<div class="path">{html.escape(folder)}</div>'
        f'{badge}'
        f'<div class="stats">'
        f'{total_files:,} files · {only_count:,} unique'
        f' · coverage {coverage:.0%}'
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
    saved_txt = "✓ saved" if is_done else ""
    savebtn_txt = "↩ un-save" if is_done else "save decision"

    return (
        f'<div class="pair{done_cls}" id="p{oid}" '
        f'data-oid="{oid}" data-choice="{choice}"{saved_mark}>'
        f'<div class="phead">'
        f'<span class="tag">#{i + 1}</span>'
        f'<span>{row["shared_count"]:,} shared</span>'
        f'<span class="tag">'
        f'A:{row["a_only_count"]:,} unique | B:{row["b_only_count"]:,} unique'
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


def _render_page(state: FolderReviewState) -> bytes:
    total = state.total()
    if total == 0:
        body = '<p class="sub">No twin-folder pairs found. Run folder-merge first.</p>'
        actionbar = ""
    else:
        body = "".join(_pair_html(state, i, row) for i, row in enumerate(state.overlaps))
        actionbar = (
            f'<div class="actionbar">'
            f'<button class="primary" onclick="saveAll()">Save all {total} decisions</button>'
            '<span id="allmsg" class="allmsg"></span></div>'
        )
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Folder overlap review</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<h1>Twin-folder overlap review</h1>"
        '<p class="sub">Click a column to select the keeper, then '
        '"save decision". Green = keep, dim = fold. '
        "Nothing is deleted here — <b>plan</b> stages the drops you confirm.</p>"
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
                length = min(int(self.headers.get("Content-Length", 0)),
                             _MAX_POST_BYTES)
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
```

- [ ] **Step 4: Run to confirm PASS**

```
python -m pytest tests/test_folder_review.py::test_serve_get_renders_pair -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```
git add photo_organizer/folderreview.py tests/test_folder_review.py
git commit -m "feat(folder-merge): folderreview module — state + HTML rendering + server (S2b step 2)"
```

---

## Task 3: Remaining HTTP tests + CLI wiring

**Files:**
- Test: `tests/test_folder_review.py` (append 4 more tests)
- Modify: `photo_organizer/__main__.py` (add `--folders` flag + dispatch)

- [ ] **Step 1: Write the four failing tests**

Append to `tests/test_folder_review.py`:

```python
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
                "SELECT status, keeper FROM folder_overlaps WHERE overlap_id=?",
                (oid,),
            ).fetchone()
            assert row["status"] == "reviewed"
            assert row["keeper"] == "b"
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
    assert args.func is m.cmd_review
    assert args.folders is True
```

- [ ] **Step 2: Run to confirm FAILs**

```
python -m pytest tests/test_folder_review.py::test_serve_post_decision tests/test_folder_review.py::test_serve_post_undecision tests/test_folder_review.py::test_serve_decision_all tests/test_folder_review.py::test_cli_review_folders_wired -v
```
Expected: 4 failures (3 pass because folderreview module exists; the CLI test fails with `unrecognized arguments: --folders`).

- [ ] **Step 3: Wire `--folders` into `__main__.py`**

In `photo_organizer/__main__.py`, find `cmd_review` (around line 379) and add the `--folders` branch:

```python
def cmd_review(args):
    cfg = _load_cfg(args)
    with Database(_db_path(args, cfg)) as db:
        if getattr(args, "auto", False):
            from .reviewer import auto_resolve_near_dupes
            auto_resolve_near_dupes(db, commit=getattr(args, "commit", False))
        elif getattr(args, "folders", False):
            from .folderreview import serve as folder_serve
            folder_serve(db, port=getattr(args, "port", 0))
        elif getattr(args, "web", False):
            from .webreview import serve
            serve(db, review_all=getattr(args, "all", False),
                  port=getattr(args, "port", 0))
        else:
            from .reviewer import review_near_dupes
            review_near_dupes(db, review_all=getattr(args, "all", False))
```

Then find the `review` subparser argument block (around line 720-735) and add `--folders` before `p_review.set_defaults(...)`:

```python
    p_review.add_argument(
        "--folders", action="store_true",
        help="Review twin-folder pairs (from folder-merge) instead of near-duplicate images",
    )
    p_review.set_defaults(func=cmd_review)
```

- [ ] **Step 4: Run all 6 folder-review tests to confirm PASS**

```
python -m pytest tests/test_folder_review.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Full suite regression check**

```
python -m pytest -q
```
Expected: 184 passed (178 prior + 6 new), exit code 0.

- [ ] **Step 6: Commit**

```
git add tests/test_folder_review.py photo_organizer/__main__.py
git commit -m "feat(folder-merge): review --web --folders wired; 6 tests green (S2b complete)"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] `review --web --folders` dispatches to new `folderreview.serve()` — Task 3, `__main__.py`
- [x] Folder pair shown side-by-side with path / count / coverage / sample files — `_pair_html`, `_col_html`
- [x] Pre-selects suggested keeper from DB — `choice = k if k in ("a","b") else "both"`
- [x] User can pick Keep A / Keep B / Keep Both — `selectSide()` JS + three buttons
- [x] Decision recorded via `record_folder_overlap_decision` — POST handler
- [x] Decision reversible via "un-save" — `reopen_folder_overlap` via POST `/folder-undecision`
- [x] "Save all" bulk endpoint — POST `/folder-decision-all`
- [x] Already-reviewed pairs shown dimmed (`.done` class), not hidden — `FolderReviewState` loads all
- [x] keeper=NULL for "both" — `keeper = dec if dec in ("a","b") else None`
- [x] Nothing deleted — decisions only recorded; `plan` acts on them in S2c
- [x] Binds 127.0.0.1 only, localhost/127.0.0.1 host check — `_make_handler`

**Placeholder scan:** None found.

**Type consistency:** `record_folder_overlap_decision(overlap_id: int, keeper, reviewed_at: str)` matches all call sites in `folderreview.py` handler. `reopen_folder_overlap(overlap_id: int)` matches call site. `_col_html` signature is consistent between definition and both call sites in `_pair_html`.
