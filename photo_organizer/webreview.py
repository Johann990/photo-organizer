"""
webreview.py — HTML contact-sheet near-duplicate review (an ADDITIONAL front-end)

The terminal review (`reviewer.review_near_dupes`) opens each candidate in a
separate OS viewer window — unworkable at thousands of clusters.  This module
serves the SAME clusters as a single scrollable contact sheet: every candidate
rendered as a cached ~256px thumbnail with its resolution / size / camera / path
and the cluster's Hamming distance, with smart per-cluster defaults already
pre-selected.

It is ONLY a new human front-end.  The decision/recording model is untouched:
every decision flows through `reviewer._record_decision` exactly as the TUI's
does — write `status='reviewed'` + `keep_file_id` into `duplicates`; `plan`
later derives and stages the losers.  Nothing is ever auto-deleted here; the
page only PRE-SELECTS a suggestion the human confirms.

Cluster set, ranking and the burst/distinct-look-alike skip all reuse the TUI's
helpers (`_build_near_clusters`, `keep_score`, `_has_samename_dupes`).

Two kinds of default selection:
  • copy / resized clusters (same filename stem at different resolution) →
    keep the highest-resolution copy (keep_score), drop the rest.
  • burst clusters (distinct frames of one moment) → keep every in-focus frame,
    pre-mark only the soft-focus frames (sharpness far below the cluster's
    sharpest) as drop candidates.

Sharpness is a no-new-dependency Pillow proxy (variance of an edge-filtered
grayscale) computed by PIGGYBACKING on the thumbnail decode — one read per file.
"""

from __future__ import annotations

import html
import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

from .db import Database
from .planner import keep_score
from .progress import console, print_phase_header, print_success
from .reviewer import (
    _build_near_clusters,
    _has_samename_dupes,
    _pick_keeper,
    _record_decision,
)

# A frame is "soft focus" when its sharpness falls below this fraction of the
# cluster's sharpest frame.  RELATIVE (within-cluster) — burst frames share
# content/exposure, so a global absolute blur threshold is unreliable.
SOFT_FOCUS_RATIO = 0.55

# Thumbnail box (longest edge, px).  Big enough to judge focus, small enough
# that the whole review subset fits on disk and re-renders instantly.
THUMB_SIZE = 256


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Sharpness proxy (Pillow only — no cv2 dependency)
# ---------------------------------------------------------------------------

def sharpness(img: Image.Image) -> float:
    """A focus/sharpness score for a PIL image (higher = sharper).

    Variance of an edge-filtered grayscale — a Laplacian-like proxy.  A blurred
    frame has weaker, smoother edges, so the edge image has lower variance.
    Absolute values are meaningless across scenes; only meaningful WITHIN a
    cluster of near-identical frames (see `flag_soft_focus`).
    """
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(edges).var[0])


def flag_soft_focus(scores: dict[int, float],
                    ratio: float = SOFT_FOCUS_RATIO) -> set[int]:
    """Return the file_ids that are soft-focus RELATIVE to the cluster's best.

    A frame is flagged when its sharpness is below `ratio` × the sharpest
    frame's sharpness.  Empty / all-similar clusters flag nothing.
    """
    if not scores:
        return set()
    top = max(scores.values())
    if top <= 0:
        return set()
    threshold = top * ratio
    return {fid for fid, s in scores.items() if s < threshold}


# ---------------------------------------------------------------------------
# Default selection per cluster type
# ---------------------------------------------------------------------------

def default_selection(
    members: list[int],
    meta: dict[int, dict],
    scores: dict[int, float],
    known: set[str],
) -> tuple[set[int], set[int]]:
    """Pre-select keepers/drops for a cluster (a suggestion, never an action).

    Returns (kept_ids, dropped_ids), a partition of `members`.

      • copy / resized cluster (shares a filename stem) → keep the single
        highest-ranked copy (keep_score: resolution first), drop the rest.
      • burst cluster (distinct frames) → keep every in-focus frame; pre-drop
        only the soft-focus frames.  If sharpness is unavailable or every frame
        is sharp, keep all.
    """
    member_set = set(members)
    if _has_samename_dupes(members, meta):
        keeper = _pick_keeper(members, meta, known)
        return {keeper}, member_set - {keeper}

    soft = flag_soft_focus({f: scores[f] for f in members if f in scores})
    kept = member_set - soft
    if not kept:  # degenerate — never drop everything
        return member_set, set()
    return kept, soft


# ---------------------------------------------------------------------------
# Thumbnail + sharpness cache (keyed by file_id)
# ---------------------------------------------------------------------------

class ThumbCache:
    """Lazily generate and cache ~256px thumbnails keyed by file_id.

    One decode of the original yields BOTH the thumbnail and the sharpness
    score; both are cached so re-runs (and the page's default-selection pass)
    never re-read the original.  `generations` counts real (cache-miss) builds.
    """

    def __init__(self, cache_dir: str | Path, size: int = THUMB_SIZE) -> None:
        self.dir = Path(cache_dir)
        self.size = size
        self.generations = 0
        self._sharp: dict[int, float] = {}
        self._lock = threading.Lock()

    def thumb_path(self, file_id: int) -> Path:
        return self.dir / f"{file_id}.jpg"

    def ensure(self, file_id: int, orig_path: str | Path) -> tuple[Path, float]:
        """Return (thumbnail_path, sharpness), generating on first request."""
        tp = self.thumb_path(file_id)
        with self._lock:
            if file_id in self._sharp and tp.exists():
                return tp, self._sharp[file_id]

            self.dir.mkdir(parents=True, exist_ok=True)
            if tp.exists():
                # Thumbnail survived from a previous run but sharpness wasn't in
                # memory — recompute from the small cached thumb (no big read).
                with Image.open(tp) as t:
                    s = sharpness(t)
                self._sharp[file_id] = s
                return tp, s

            # Cache miss — single decode of the original → thumbnail + sharpness.
            with Image.open(orig_path) as img:
                img = img.convert("RGB")
                img.thumbnail((self.size, self.size))
                s = sharpness(img)
                img.save(tp, "JPEG", quality=80)
            self.generations += 1
            self._sharp[file_id] = s
            return tp, s


# ---------------------------------------------------------------------------
# Decision recording — reuses reviewer._record_decision unchanged
# ---------------------------------------------------------------------------

def record_selection(
    db: Database,
    members: list[int],
    kept: set[int],
    dropped: set[int],
    hamming: int,
) -> None:
    """Record a web review selection through the existing `_record_decision`.

    Three shapes, all expressed via `_record_decision` (never auto-delete):
      • nothing dropped            → keep all  (keep_file_id = NULL)
      • exactly one kept           → single keeper, stage the rest
      • several kept, some dropped → stage each dropped against a fixed keeper,
        then close any remaining intra-cluster pending pairs as keep-all so no
        kept frame is ever derivable as a loser by `plan`.
    """
    if not dropped:
        _record_decision(db, members, None, hamming)
        return

    if len(kept) == 1:
        _record_decision(db, members, next(iter(kept)), hamming)
        return

    # Multi-keep: every dropped frame loses to one stable keeper.
    keeper = max(kept, key=lambda f: f)  # deterministic; identity of keeper
    for loser in dropped:                # only matters as "a survivor", any kept
        _record_decision(db, [keeper, loser], keeper, hamming)

    # Close the rest of the cluster (kept↔kept, kept↔other) as keep-all so plan
    # stages nothing more and the cluster no longer shows as pending.
    ids = ",".join("?" * len(members))
    db.conn.execute(
        f"UPDATE duplicates SET status='reviewed', keep_file_id=NULL, "
        f"resolved_at=? WHERE dup_type='NEAR' AND status='pending' "
        f"AND file_id_a IN ({ids}) AND file_id_b IN ({ids})",
        [_now(), *members, *members],
    )


def apply_decision(
    db: Database,
    members: list[int],
    kept: set[int],
    dropped: set[int],
    hamming: int,
) -> None:
    """Record one cluster decision and commit (the POST /decision target)."""
    record_selection(db, members, kept, dropped, hamming)
    db.commit()


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

class ReviewState:
    """Everything the HTTP handler needs, built once per server."""

    def __init__(self, db: Database, review_all: bool, cache_dir: Path) -> None:
        clusters, meta, cluster_h = _build_near_clusters(db)
        if not review_all:
            clusters = [m for m in clusters if _has_samename_dupes(m, meta)]
        # Most-similar (lowest hamming) first, larger groups first.
        clusters.sort(key=lambda m: (cluster_h.get(m[0], 0), -len(m)))

        self.db = db
        self.meta = meta
        self.clusters = clusters
        self.cluster_h = cluster_h
        self.known = db.get_known_camera_models()
        self.thumbs = ThumbCache(cache_dir)
        self.lock = threading.Lock()

    def hamming(self, members: list[int]) -> int:
        return self.cluster_h.get(members[0], 0)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_PAGE_CSS = """
:root { color-scheme: dark; }
body { font-family: system-ui, sans-serif; background:#16181d; color:#e6e6e6;
       margin:0; padding:1.2rem 1.4rem 6rem; }
h1 { font-size:1.25rem; margin:0 0 .2rem; }
.sub { color:#8b93a1; font-size:.85rem; margin:0 0 1.4rem; }
.cluster { border:1px solid #2b2f38; border-radius:10px; padding:.8rem .9rem;
           margin:0 0 1.1rem; background:#1c1f26; }
.chead { display:flex; align-items:center; gap:.8rem; margin-bottom:.6rem;
         font-size:.9rem; color:#aab2c0; }
.chead .tag { background:#2b2f38; border-radius:6px; padding:.1rem .5rem; }
.grid { display:flex; flex-wrap:wrap; gap:.7rem; }
.cand { width:200px; border:2px solid #333a45; border-radius:8px; padding:.4rem;
        cursor:pointer; background:#21252e; transition:border-color .1s; }
.cand.keep { border-color:#3fb950; box-shadow:0 0 0 1px #3fb95066; }
.cand.drop { border-color:#6e7681; opacity:.6; }
.cand img { width:100%; height:150px; object-fit:contain; background:#0d0f13;
            border-radius:4px; display:block; }
.cand .m { font-size:.72rem; line-height:1.35; margin-top:.35rem; color:#c4ccd8; }
.cand .m .p { color:#7d8694; word-break:break-all; }
.badge { display:inline-block; font-size:.68rem; border-radius:4px;
         padding:0 .35rem; margin-top:.25rem; }
.badge.soft { background:#5a3a00; color:#ffcf70; }
.badge.kept { background:#143d1d; color:#6fdc8c; }
.controls { margin-top:.5rem; }
.controls button { background:#2b2f38; color:#e6e6e6; border:1px solid #3a404b;
                   border-radius:6px; padding:.25rem .7rem; cursor:pointer;
                   font-size:.8rem; }
.controls button:hover { background:#353b46; }
.saved { color:#3fb950; font-size:.78rem; margin-left:.6rem; }
.topbar { margin-bottom:1.2rem; font-size:.85rem; }
.topbar a { color:#58a6ff; }
"""

_PAGE_JS = """
function post(idx){
  const el = document.getElementById('c'+idx);
  const kept=[], dropped=[];
  el.querySelectorAll('.cand').forEach(c=>{
    const id=parseInt(c.dataset.id,10);
    (c.classList.contains('drop')?dropped:kept).push(id);
  });
  fetch('/decision',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cluster:idx,kept:kept,dropped:dropped})})
   .then(r=>{ if(r.ok){ el.querySelector('.saved').textContent='✓ saved';
                        el.style.opacity=.5; } });
}
function toggle(idx,id){
  const el=document.getElementById('c'+idx);
  const c=el.querySelector('.cand[data-id="'+id+'"]');
  if(c.classList.contains('keep')){ c.classList.remove('keep'); c.classList.add('drop'); }
  else { c.classList.remove('drop'); c.classList.add('keep'); }
}
function keepAll(idx){
  document.querySelectorAll('#c'+idx+' .cand').forEach(c=>{
    c.classList.remove('drop'); c.classList.add('keep'); });
}
"""


def _candidate_html(state: ReviewState, idx: int, fid: int,
                    kept: set[int], soft: set[int], score: float) -> str:
    m = state.meta[fid]
    w, h = m.get("width"), m.get("height")
    dims = f"{w}×{h}" if (w and h) else "?"
    mb = (m.get("size_bytes") or 0) / 1_048_576
    cam = html.escape(str(m.get("camera_model") or "—"))
    path = html.escape(str(m.get("path") or ""))
    cls = "keep" if fid in kept else "drop"
    badge = ('<span class="badge soft">⚠ soft focus</span>' if fid in soft
             else '<span class="badge kept">in focus</span>')
    return (
        f'<div class="cand {cls}" data-id="{fid}" '
        f'onclick="toggle({idx},{fid})">'
        f'<img loading="lazy" src="/thumb/{fid}" alt="">'
        f'<div class="m"><b>{dims}</b> · {mb:.1f} MB<br>'
        f'{cam}<br>sharp {score:.0f} {badge}<br>'
        f'<span class="p">{path}</span></div></div>'
    )


def _cluster_html(state: ReviewState, idx: int, members: list[int]) -> str:
    scores: dict[int, float] = {}
    for f in members:
        try:
            _, s = state.thumbs.ensure(f, state.meta[f]["path"])
        except (OSError, ValueError):
            s = 0.0
        scores[f] = s

    kept, _dropped = default_selection(members, state.meta, scores, state.known)
    soft = flag_soft_focus(scores)
    ranked = sorted(members, key=lambda f: keep_score(state.meta[f], state.known),
                    reverse=True)
    cands = "".join(
        _candidate_html(state, idx, f, kept, soft, scores.get(f, 0.0))
        for f in ranked
    )
    ham = state.hamming(members)
    return (
        f'<div class="cluster" id="c{idx}">'
        f'<div class="chead"><span class="tag">#{idx + 1}</span>'
        f'<span>{len(members)} candidates</span>'
        f'<span class="tag">Hamming≈{ham}</span></div>'
        f'<div class="grid">{cands}</div>'
        f'<div class="controls">'
        f'<button onclick="keepAll({idx})">keep all</button> '
        f'<button onclick="post({idx})">save decision</button>'
        f'<span class="saved"></span></div></div>'
    )


def _render_page(state: ReviewState) -> bytes:
    total = len(state.clusters)
    if total == 0:
        body = '<p class="sub">No near-duplicate clusters pending review. 🎉</p>'
    else:
        body = "".join(
            _cluster_html(state, i, m) for i, m in enumerate(state.clusters)
        )
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Near-duplicate review</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<h1>Near-duplicate contact sheet</h1>"
        f'<p class="sub">{total} cluster(s). Green = keep, dim = drop. '
        "Click a thumbnail to toggle, then “save decision”. "
        "Nothing is deleted here — <b>plan</b> stages the drops you confirm.</p>"
        f"{body}"
        f"<script>{_PAGE_JS}</script></body></html>"
    )
    return doc.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _make_handler(state: ReviewState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr spam
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
            if self.path != "/decision":
                self._send(404, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                idx = int(payload["cluster"])
                members = state.clusters[idx]
                kept = set(payload.get("kept", []))
                dropped = set(payload.get("dropped", []))
                with state.lock:
                    apply_decision(state.db, members, kept, dropped,
                                   state.hamming(members))
                self._send(200, b'{"ok":true}', "application/json")
            except (KeyError, ValueError, IndexError, TypeError) as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(),
                           "application/json")

    return Handler


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve(
    db: Database,
    review_all: bool = False,
    port: int = 0,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    background: bool = False,
) -> ThreadingHTTPServer:
    """Start the local contact-sheet review server.

    Binds 127.0.0.1 only (single local user, no auth).  With background=False
    (the CLI default) this blocks, serving until Ctrl-C.  With background=True
    (tests) it returns the running server immediately; call `.shutdown()`.
    """
    cache_dir = db.path.parent / "_staging" / ".thumbs"
    state = ReviewState(db, review_all, cache_dir)
    httpd = ThreadingHTTPServer((host, port), _make_handler(state))
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"

    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    print_phase_header("3B web", "Near-Duplicate Contact-Sheet Review")
    print_success(
        f"Serving {len(state.clusters):,} cluster(s) at [bold]{url}[/bold]\n"
        "  Review in your browser; decisions save live to the DB. "
        "Press Ctrl-C here when done."
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
