"""
READ-ONLY spike — folder-level duplicate detection preview (S1 validation).

Does NOT create tables, touch files, or modify the DB. Pure SELECT + in-memory
analysis to show which source folders look like wholesale copies of each other,
so we can validate the S1 design against the real library before building it.

v2: ancestor rollup (collapse to topmost duplicated subtree) + smarter keeper
hint (penalise backup/container/copy/temp ancestors) + self-contained HTML report.

Algorithm (mirrors the S1 spec, leaf granularity + highest-common-ancestor rollup):
  1. folder (= parent dir of each file) -> set of sha256
  2. inverted index sha256 -> {folders}; skip ubiquitous shas (in > CAP folders)
  3. candidate pairs = folders co-occurring on shared shas; count shared
  4. coverage_a = shared/|A|, coverage_b = shared/|B|
  5. flag when max(cov_a, cov_b) >= COVERAGE and shared >= MIN_SHARED
  6. rollup: drop (A,B) if ANY ancestor pair (parent^k A, parent^k B) is flagged
  7. keeper hint = the side with fewer backup/container/copy markers in its path

Usage: python scripts/spike_folder_overlaps.py [db_path]
"""

from __future__ import annotations

import html
import sqlite3
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path, PureWindowsPath

DB = sys.argv[1] if len(sys.argv) > 1 else "C:/PhotoTestZone/photos.db"
COVERAGE = 0.95
MIN_SHARED = 5
UBIQUITOUS_CAP = 30
OUT_HTML = "C:/PhotoTestZone/_staging/folder_overlaps.html"

# Lowercased substrings that mark a folder (or any ancestor) as a backup / copy /
# scratch location — the LESS canonical side, i.e. the one to fold AWAY from.
NONCANONICAL = (
    "all in lightroom", "depository", "rawtank", "jpegtank", "raw.old",
    "raw_old", ".out", "完整備份", "待整理", "archive", "backup",
    "- copy", "_copy", " copy", "copy of", "temp", "tmp", ".old", ".bak",
)


def parent(path: str) -> str:
    return str(PureWindowsPath(path).parent)


def is_root(path: str) -> bool:
    p = PureWindowsPath(path)
    return p.parent == p


def noncanonical_score(folder: str) -> int:
    low = folder.lower()
    return sum(1 for tok in NONCANONICAL if tok in low)


def pick_keeper(a: str, b: str) -> str:
    """Return 'a' or 'b' — the more canonical side (fewer backup markers,
    then shorter path, then lexicographically smaller)."""
    sa, sb = noncanonical_score(a), noncanonical_score(b)
    if sa != sb:
        return "a" if sa < sb else "b"
    if len(a) != len(b):
        return "a" if len(a) < len(b) else "b"
    return "a" if a <= b else "b"


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # Scan roots: never climb above these when building subtree-union sets,
    # so we don't merge unrelated trees up at the drive root.
    scan_roots = {
        r"D:\Albums", r"D:\DCIM", r"D:\DCIM_Working", r"D:\DCIM_Storage",
    }

    folder_shas: dict[str, set[str]] = defaultdict(set)

    n = 0
    for row in con.execute(
        "SELECT path, sha256 FROM files "
        "WHERE sha256 IS NOT NULL AND status != 'error'"
    ):
        sha = row["sha256"]
        # D3: every level — add this file's sha to its leaf folder AND every
        # ancestor folder up to (and including) the scan root (subtree = union).
        fld = parent(row["path"])
        folder_shas[fld].add(sha)
        cur = fld
        while cur not in scan_roots and not is_root(cur):
            cur = parent(cur)
            folder_shas[cur].add(sha)
            if cur in scan_roots:
                break
        n += 1

    n_leaf = sum(1 for f in folder_shas)
    print(f"Loaded {n:,} hashed files; {n_leaf:,} folders (leaf + intermediate).")

    # Inverted index over the subtree-union folder sets.
    sha_folders: dict[str, set[str]] = defaultdict(set)
    for fld, shas in folder_shas.items():
        for sha in shas:
            sha_folders[sha].add(fld)

    pair_shared: dict[tuple[str, str], int] = defaultdict(int)
    for folders in sha_folders.values():
        if 2 <= len(folders) <= UBIQUITOUS_CAP:
            for a, b in combinations(sorted(folders), 2):
                # Skip same-tree ancestor/descendant pairs (a contains b or vice
                # versa) — that's containment within ONE tree, not a duplicate.
                if a.startswith(b + "\\") or b.startswith(a + "\\"):
                    continue
                pair_shared[(a, b)] += 1

    flagged: dict[tuple[str, str], dict] = {}
    for (a, b), shared in pair_shared.items():
        if shared < MIN_SHARED:
            continue
        na, nb = len(folder_shas[a]), len(folder_shas[b])
        cov_a, cov_b = shared / na, shared / nb
        if min(cov_a, cov_b) >= COVERAGE:  # twin: both sides ~same set
            keeper = pick_keeper(a, b)
            flagged[(a, b)] = {
                "shared": shared, "na": na, "nb": nb,
                "cov_a": cov_a, "cov_b": cov_b, "keeper": keeper,
            }

    # Rollup: suppress a pair if ANY ancestor pair (walked in lockstep) is flagged.
    flagged_keys = set(flagged)
    suppressed: set[tuple[str, str]] = set()
    for (a, b) in flagged:
        pa, pb = a, b
        while True:
            pa, pb = parent(pa), parent(pb)
            if pa == pb or is_root(pa) or is_root(pb):
                break
            if tuple(sorted((pa, pb))) in flagged_keys:
                suppressed.add((a, b))
                break

    top = [(k, v) for k, v in flagged.items() if k not in suppressed]
    top.sort(key=lambda kv: -kv[1]["shared"])

    print(f"Flagged pairs (cov >= {COVERAGE:.0%}, shared >= {MIN_SHARED}): {len(flagged):,}")
    print(f"After ancestor rollup: {len(top):,} (suppressed {len(suppressed):,})\n")

    for (a, b), v in top[:40]:
        ka, kb = ("KEEP", "fold") if v["keeper"] == "a" else ("fold", "KEEP")
        print(f"shared={v['shared']:,}  cov A={v['cov_a']:.0%} B={v['cov_b']:.0%}")
        print(f"  [{ka}] {a}")
        print(f"  [{kb}] {b}")
    if len(top) > 40:
        print(f"... and {len(top) - 40:,} more (see HTML).")

    _write_html(top, n, len(folder_shas))
    print(f"\nHTML report: {OUT_HTML}")


def _write_html(top, n_files, n_folders) -> None:
    rows = []
    for i, ((a, b), v) in enumerate(top):
        keep, fold = (a, b) if v["keeper"] == "a" else (b, a)
        keep_cov = v["cov_a"] if v["keeper"] == "a" else v["cov_b"]
        fold_cov = v["cov_b"] if v["keeper"] == "a" else v["cov_a"]
        keep_n = v["na"] if v["keeper"] == "a" else v["nb"]
        fold_n = v["nb"] if v["keeper"] == "a" else v["na"]
        rows.append(
            f'<details class="g" data-shared="{v["shared"]}" data-idx="{i}">'
            f"<summary>"
            f'<span class="sh">{v["shared"]:,}</span> shared &middot; '
            f'<span class="cov">cov {max(v["cov_a"], v["cov_b"]):.0%}</span>'
            f'<span class="lbl">{html.escape(PureWindowsPath(keep).name)}</span>'
            f"</summary>"
            f'<div class="pair">'
            f'<div class="keep"><span class="tag k">KEEP</span> '
            f'{keep_n:,} files, {keep_cov:.0%} covered<br><code>{html.escape(keep)}</code></div>'
            f'<div class="fold"><span class="tag f">fold &amp; dedupe</span> '
            f'{fold_n:,} files, {fold_cov:.0%} covered<br><code>{html.escape(fold)}</code></div>'
            f"</div></details>"
        )
    doc = _TEMPLATE.format(
        n_pairs=f"{len(top):,}", n_files=f"{n_files:,}",
        n_folders=f"{n_folders:,}", body="\n".join(rows),
    )
    out = Path(OUT_HTML)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")


_TEMPLATE = """<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Folder overlaps (S1 preview)</title><style>
:root {{ --bg:#1a1a1a; --fg:#e8e8e8; --dim:#9a9a9a; --keep:#5ec27e; --fold:#e0894a;
        --row:#242424; --row2:#2c2c2c; --border:#383838; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font-family:"Segoe UI",system-ui,sans-serif; font-size:14px; }}
h1 {{ font-size:18px; margin:0 0 4px; }}
.sub {{ color:var(--dim); margin-bottom:16px; }}
.controls {{ display:flex; gap:8px; margin-bottom:14px; }}
button {{ background:var(--row2); color:var(--fg); border:1px solid var(--border);
         border-radius:6px; padding:6px 12px; cursor:pointer; }}
button:hover {{ border-color:var(--keep); }}
#tree {{ border:1px solid var(--border); border-radius:8px; overflow:hidden; }}
details.g {{ border-bottom:1px solid var(--border); }}
summary {{ display:flex; align-items:center; gap:10px; padding:10px 14px;
          cursor:pointer; background:var(--row); list-style:none; }}
summary::-webkit-details-marker {{ display:none; }}
summary:hover {{ background:var(--row2); }}
.sh {{ font-weight:600; color:var(--fg); min-width:70px; }}
.cov {{ color:var(--dim); min-width:80px; }}
.lbl {{ color:var(--keep); font-weight:600; }}
.pair {{ background:#1e1e1e; padding:8px 14px; display:grid; gap:8px; }}
.keep, .fold {{ padding:8px 10px; border-radius:6px; }}
.keep {{ background:rgba(94,194,126,.08); border-left:3px solid var(--keep); }}
.fold {{ background:rgba(224,137,74,.08); border-left:3px solid var(--fold); }}
.tag {{ font-size:11px; padding:1px 7px; border-radius:9px; margin-right:6px; }}
.tag.k {{ background:var(--keep); color:#08240f; }}
.tag.f {{ background:var(--fold); color:#2a1606; }}
code {{ color:var(--dim); font-size:12px; word-break:break-all; }}
</style></head><body>
<h1>Folder overlaps — S1 detection preview</h1>
<div class="sub">{n_pairs} flagged folder pairs (after ancestor rollup) &middot;
 {n_files} hashed files &middot; {n_folders} folders &middot;
 KEEP = suggested canonical side, fold = redundant copy to merge away.
 This is a PREVIEW only — nothing has been moved or deleted.</div>
<div class="controls"><button id="ex">Expand all</button>
<button id="co">Collapse all</button></div>
<div id="tree">
{body}
</div><script>
const t=document.getElementById('tree');
const gs=()=>Array.from(t.querySelectorAll('details.g'));
document.getElementById('ex').onclick=()=>gs().forEach(g=>g.open=true);
document.getElementById('co').onclick=()=>gs().forEach(g=>g.open=false);
</script></body></html>
"""


if __name__ == "__main__":
    main()
