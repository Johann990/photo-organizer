"""
folder_merge.py — detect "twin" folders: two folders that hold essentially the
SAME set of files (by exact SHA-256), i.e. one is a wholesale copy of the other.

DB-only, read-only over `files`: builds subtree-union SHA sets per folder, an
inverted sha→folders index, finds folder pairs whose MUTUAL coverage ≥ threshold
(Twin semantics — both sides are ~the same set), rolls them up to the highest
twin ancestor, and suggests a keeper (the side with fewer backup/container
markers). Validated against the real 280k-file library via
scripts/spike_folder_overlaps.py. Results are recorded in `folder_overlaps`;
deciding/merging is done later (review UI + plan/execute).
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from pathlib import PureWindowsPath

from .db import Database
from .progress import PhaseProgress

# Lowercased substrings marking the LESS canonical side (fold away from these).
_NONCANONICAL = (
    "all in lightroom", "depository", "rawtank", "jpegtank", "raw.old",
    "raw_old", ".out", "完整備份", "待整理", "archive", "backup",
    "- copy", "_copy", " copy", "copy of", "temp", "tmp", ".old", ".bak",
)


def _parent(path: str) -> str:
    return str(PureWindowsPath(path).parent)


def _is_root(path: str) -> bool:
    p = PureWindowsPath(path)
    return p.parent == p


def _noncanonical_score(folder: str) -> int:
    low = folder.lower()
    return sum(1 for tok in _NONCANONICAL if tok in low)


def _pick_keeper(a: str, b: str) -> str:
    """'a' or 'b' — the more canonical side (fewer backup markers, then shorter
    path, then lexicographically smaller)."""
    sa, sb = _noncanonical_score(a), _noncanonical_score(b)
    if sa != sb:
        return "a" if sa < sb else "b"
    if len(a) != len(b):
        return "a" if len(a) < len(b) else "b"
    return "a" if a <= b else "b"


def compute_folder_overlaps(
    db: Database, scan_roots: list, *, coverage: float = 0.95,
    min_shared: int = 5, ubiquitous_cap: int = 30, show_progress: bool = False,
) -> list[dict]:
    """Return rolled-up twin-folder pairs (see module docstring)."""
    roots = {str(PureWindowsPath(r)) for r in scan_roots}

    rows = db.conn.execute(
        "SELECT path, sha256 FROM files "
        "WHERE sha256 IS NOT NULL AND status != 'error'"
    ).fetchall()

    folder_shas: dict[str, set] = defaultdict(set)

    def _accumulate(path: str, sha: str) -> None:
        fld = _parent(path)
        folder_shas[fld].add(sha)
        cur = fld
        while cur not in roots and not _is_root(cur):
            cur = _parent(cur)
            folder_shas[cur].add(sha)
            if cur in roots:
                break

    if show_progress:
        with PhaseProgress(
            "Indexing folders", total=len(rows), phase="folder-merge"
        ) as prog:
            for r in rows:
                _accumulate(r["path"], r["sha256"])
                prog.advance(1)
    else:
        for r in rows:
            _accumulate(r["path"], r["sha256"])

    # Inverted index over the subtree-union folder sets.
    sha_folders: dict[str, set] = defaultdict(set)
    for fld, shas in folder_shas.items():
        for sha in shas:
            sha_folders[sha].add(fld)

    pair_shared: dict[tuple, int] = defaultdict(int)
    for folders in sha_folders.values():
        if 2 <= len(folders) <= ubiquitous_cap:
            for a, b in combinations(sorted(folders), 2):
                # Skip same-tree ancestor/descendant pairs (containment in ONE
                # tree, not a duplicate).
                if a.startswith(b + "\\") or b.startswith(a + "\\"):
                    continue
                pair_shared[(a, b)] += 1

    flagged: dict[tuple, dict] = {}
    for (a, b), shared in pair_shared.items():
        if shared < min_shared:
            continue
        na, nb = len(folder_shas[a]), len(folder_shas[b])
        cov_a, cov_b = shared / na, shared / nb
        if min(cov_a, cov_b) >= coverage:  # Twin: both sides ~same set
            flagged[(a, b)] = {
                "folder_a": a, "folder_b": b, "shared_count": shared,
                "a_only_count": na - shared, "b_only_count": nb - shared,
                "coverage_a": cov_a, "coverage_b": cov_b,
                "keeper": _pick_keeper(a, b),
            }

    # Rollup: drop a pair if ANY ancestor pair (walked in lockstep) is a twin.
    flagged_keys = set(flagged)
    out: list[dict] = []
    for (a, b), rec in flagged.items():
        pa, pb, suppressed = a, b, False
        while True:
            pa, pb = _parent(pa), _parent(pb)
            if pa == pb or _is_root(pa) or _is_root(pb):
                break
            if tuple(sorted((pa, pb))) in flagged_keys:
                suppressed = True
                break
        if not suppressed:
            out.append(rec)

    out.sort(key=lambda r: -r["shared_count"])
    return out


def detect_and_store(db: Database, scan_roots: list, *, coverage: float = 0.95,
                     min_shared: int = 5, show_progress: bool = False) -> int:
    """Compute twin-folder overlaps and replace the folder_overlaps table with
    them (clears prior pending detection first). Returns the number stored."""
    overlaps = compute_folder_overlaps(
        db, scan_roots, coverage=coverage, min_shared=min_shared,
        show_progress=show_progress,
    )
    db.clear_folder_overlaps()
    for o in overlaps:
        db.insert_folder_overlap(
            folder_a=o["folder_a"], folder_b=o["folder_b"],
            shared_count=o["shared_count"], a_only_count=o["a_only_count"],
            b_only_count=o["b_only_count"], coverage_a=o["coverage_a"],
            coverage_b=o["coverage_b"], keeper=o["keeper"],
        )
    db.commit()
    return len(overlaps)
