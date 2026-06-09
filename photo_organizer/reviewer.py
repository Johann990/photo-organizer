"""
reviewer.py — Near-duplicate review (cluster-based)

Near-duplicate pairs are first collapsed into CLUSTERS (connected components):
if A~B and B~C, then {A, B, C} is one group.  53k raw pairs typically collapse
into a few thousand clusters, most of which need no human judgement at all.

Two entry points:

  auto_resolve_near_dupes(db, commit)
      Cluster every pending NEAR pair and AUTO-resolve the unambiguous groups:
        • a group with one clearly-highest-resolution file  → keep it
        • a group of same-resolution near-identical copies   → keep the best-
          located copy (known-camera/Masters folder > real event-name folder >
          shortest path), per the user's chosen keep policy
        • a group containing a degenerate image (tiny/blank/0-byte — pHash junk)
          → keep ALL, never delete
      Genuinely ambiguous groups (same resolution, very different file size —
      possibly different edits/crops) are LEFT pending for the human.
      Dry-run by default; pass commit=True to write the decisions.

  review_near_dupes(db)
      Interactive, one CLUSTER at a time (not one pair), for whatever stays
      pending.  Press 'v' to open every image in the group in the OS viewer.

Both only RECORD decisions in the `duplicates` table (status='reviewed',
keep_file_id).  The actual STAGE_DELETE for each loser is created later by
`plan`.  Run `review` (and/or `review --auto --commit`) BEFORE `plan`.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table

from .db import Database
from .planner import keep_score
from .progress import console, print_phase_header, print_success, print_warning

# A pHash computed from one of these is meaningless and collides with unrelated
# images, so such files must never drive a deletion decision.
_MIN_DIM = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Cluster construction (union-find over pending NEAR pairs)
# ---------------------------------------------------------------------------

def _build_near_clusters(
    db: Database,
) -> tuple[list[list[int]], dict[int, dict], dict[int, int]]:
    """Return (clusters, meta, cluster_hamming).

    clusters         — list of file_id lists (each a connected component)
    meta             — file_id → row dict (path/dims/size/camera/…)
    cluster_hamming  — root file_id → representative (min) Hamming distance
    """
    pairs = db.conn.execute(
        "SELECT file_id_a, file_id_b, hamming_distance FROM duplicates "
        "WHERE dup_type='NEAR' AND status='pending'"
    ).fetchall()

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    min_h: dict[int, int] = {}
    for p in pairs:
        union(p["file_id_a"], p["file_id_b"])

    groups: dict[int, list[int]] = defaultdict(list)
    for fid in list(parent.keys()):
        groups[find(fid)].append(fid)

    # representative hamming per cluster (smallest = most similar pair)
    for p in pairs:
        root = find(p["file_id_a"])
        h = p["hamming_distance"] if p["hamming_distance"] is not None else 0
        if root not in min_h or h < min_h[root]:
            min_h[root] = h
    cluster_hamming = {root: min_h.get(root, 0) for root in groups}

    # Load metadata for all involved files
    meta: dict[int, dict] = {}
    ids = list(parent.keys())
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        q = (
            "SELECT file_id, path, filename, width, height, size_bytes, "
            "camera_model, datetime_original FROM files WHERE file_id IN (%s)"
            % ",".join("?" * len(chunk))
        )
        for r in db.conn.execute(q, chunk):
            meta[r["file_id"]] = dict(r)

    clusters = [groups[root] for root in groups]
    # remap cluster_hamming to first-member key for easy lookup
    ch_by_first = {members[0]: cluster_hamming[find(members[0])]
                   for members in clusters}
    return clusters, meta, ch_by_first


# ---------------------------------------------------------------------------
# Keep policy
# ---------------------------------------------------------------------------

def _is_degenerate(m: dict) -> bool:
    w, h = m.get("width"), m.get("height")
    return (not w or not h or w < _MIN_DIM or h < _MIN_DIM
            or not m.get("size_bytes"))


def _pick_keeper(members: list[int], meta: dict, known: set[str]) -> int:
    # keep_score (shared with planner) ranks copies; highest is kept.
    return max(members, key=lambda f: keep_score(meta[f], known))


def _norm_stem(filename: str) -> str:
    """Normalised filename stem (no extension, lowercased).

    Copies of one original keep the same camera filename, so a shared stem is a
    strong 'same image' signal.  Two consecutive shots (DSC_0139 vs DSC_0140)
    have DIFFERENT stems — they must never be auto-deleted against each other.
    """
    return Path(filename).stem.strip().lower()


def _resolve_cluster(
    members: list[int], meta: dict, known: set[str]
) -> tuple[list[tuple[list[int], int | None]], bool]:
    """Decide a cluster by SAME-STEM sub-groups (copies of one image).

    Returns (decisions, has_distinct_remainder):
      decisions — list of (group, keeper).  group = same-stem copies (≥2);
                  keeper=None means the group is degenerate → keep ALL.
      has_distinct_remainder — True when more than one distinct stem is present,
                  i.e. visually-similar but differently-named images survive and
                  are worth an OPTIONAL human pass (default: keep both).

    A file is only ever staged when another copy with the SAME stem survives, so
    a unique image can never be deleted.
    """
    partitions: dict[str, list[int]] = defaultdict(list)
    for f in members:
        partitions[_norm_stem(meta[f]["filename"])].append(f)

    decisions: list[tuple[list[int], int | None]] = []
    for group in partitions.values():
        if len(group) < 2:
            continue  # unique image in this cluster — keep it
        if any(_is_degenerate(meta[f]) for f in group):
            decisions.append((group, None))           # degenerate copies → keep all
        else:
            decisions.append((group, _pick_keeper(group, meta, known)))
    return decisions, len(partitions) > 1


# ---------------------------------------------------------------------------
# Decision recording
# ---------------------------------------------------------------------------

def _record_decision(db: Database, members: list[int], keeper: int | None,
                     hamming: int) -> None:
    """Mark every pending NEAR pair in the cluster reviewed, and ensure plan
    can stage every loser.

    For a real keeper, plan derives the loser of each pair as 'the member that
    isn't keep_file_id'.  Because a loser may only be connected to the keeper
    transitively, we additionally UPSERT a direct (keeper, loser) reviewed pair
    for every loser — guaranteeing each loser is staged and the keeper never is.
    keeper=None (degenerate 'keep all') records keep_file_id=NULL so plan stages
    nothing.
    """
    now = _now()
    ids = ",".join("?" * len(members))

    # Mark all existing pending intra-cluster NEAR pairs as reviewed.
    db.conn.execute(
        f"UPDATE duplicates SET status='reviewed', keep_file_id=?, resolved_at=? "
        f"WHERE dup_type='NEAR' AND status='pending' "
        f"AND file_id_a IN ({ids}) AND file_id_b IN ({ids})",
        [keeper, now, *members, *members],
    )

    if keeper is None:
        return

    # Ensure a direct keeper↔loser reviewed pair exists for every loser.
    for loser in members:
        if loser == keeper:
            continue
        a, b = sorted((keeper, loser))
        db.conn.execute(
            "INSERT OR IGNORE INTO duplicates "
            "(file_id_a, file_id_b, dup_type, hamming_distance) VALUES (?,?,?,?)",
            (a, b, "NEAR", hamming),
        )
        db.conn.execute(
            "UPDATE duplicates SET status='reviewed', keep_file_id=?, resolved_at=? "
            "WHERE dup_type='NEAR' AND file_id_a=? AND file_id_b=?",
            (keeper, now, a, b),
        )


# ---------------------------------------------------------------------------
# Auto-resolve
# ---------------------------------------------------------------------------

def auto_resolve_near_dupes(db: Database, commit: bool = False) -> None:
    print_phase_header("3B auto", "Near-Duplicate Auto-Resolve")

    clusters, meta, cluster_h = _build_near_clusters(db)
    if not clusters:
        print_success("No pending near-duplicate pairs to resolve.")
        return

    known = db.get_known_camera_models()

    copy_groups = degen_groups = 0       # same-stem groups: dedupe / keep-all
    files_staged = 0
    remainder_clusters = 0               # clusters with >1 distinct image left
    decisions: list[tuple[list[int], int | None, int]] = []
    samples: list[tuple[list[int], int]] = []

    for members in clusters:
        cluster_decisions, has_remainder = _resolve_cluster(members, meta, known)
        ham = cluster_h.get(members[0], 0)
        if has_remainder:
            remainder_clusters += 1
        for group, keeper in cluster_decisions:
            if keeper is None:
                degen_groups += 1
            else:
                copy_groups += 1
                files_staged += len(group) - 1
                if len(samples) < 6:
                    samples.append((group, keeper))
            decisions.append((group, keeper, ham))

    total = len(clusters)
    files_total = len(meta)

    # ── Summary ─────────────────────────────────────────────────────────────
    t = Table(title="Near-duplicate clusters", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Category", style="cyan")
    t.add_column("Count", justify="right")
    t.add_column("Action", style="dim")
    t.add_row("Same-name copy groups", f"{copy_groups:,}",
              "AUTO keep best copy, stage the rest")
    t.add_row("Degenerate copy groups", f"{degen_groups:,}",
              "AUTO keep ALL (pHash junk, never delete)")
    t.add_row("[yellow]Clusters w/ multiple distinct images[/yellow]",
              f"{remainder_clusters:,}",
              "kept; OPTIONAL human pass (likely keep all)")
    t.add_row("[bold]Clusters total[/bold]", f"[bold]{total:,}[/bold]",
              f"from {files_total:,} files")
    console.print()
    console.print(t)
    console.print(
        f"\n  Files auto-staged for deletion (same-name redundant copies only): "
        f"[bold red]~{files_staged:,}[/bold red]\n"
    )

    # Sample a few auto decisions so the user can sanity-check the keep policy.
    if samples:
        console.print("  [dim]Sample auto-decisions (★ = kept, all same filename):[/dim]")
        for group, keeper in samples:
            ranked = sorted(group,
                            key=lambda f: keep_score(meta[f], known), reverse=True)
            console.rule(style="dim")
            for f in ranked:
                m = meta[f]
                star = "[green]★[/green]" if f == keeper else " "
                dims = (f"{m['width']}×{m['height']}"
                        if m.get("width") and m.get("height") else "?")
                mb = (m.get("size_bytes") or 0) / 1_048_576
                console.print(f"   {star} [dim]{dims:>11}  {mb:5.1f}MB[/dim]  {m['path']}")
        console.print()

    # ── Commit or dry-run ─────────────────────────────────────────────────────
    if not commit:
        console.print(
            "  [yellow]DRY RUN[/yellow] — nothing written.\n"
            "  Re-run with [cyan]--commit[/cyan] to record these decisions, then "
            "(optionally) run [cyan]review[/cyan] for the "
            f"{remainder_clusters:,} clusters with distinct look-alikes.\n"
        )
        return

    with console.status("Recording auto-decisions…"):
        for group, keeper, ham in decisions:
            _record_decision(db, group, keeper, ham)
        db.commit()

    print_success(
        f"Auto-resolved {copy_groups + degen_groups:,} same-name copy groups "
        f"(~{files_staged:,} redundant copies staged for deletion when you run plan)."
    )
    if remainder_clusters:
        print_warning(
            f"{remainder_clusters:,} clusters still contain multiple distinct "
            "look-alike images (kept). Optional: python -m photo_organizer review"
        )


# ---------------------------------------------------------------------------
# Interactive review — one CLUSTER at a time
# ---------------------------------------------------------------------------

def _file_panel(label: str, m: dict, color: str) -> Panel:
    w, h = m.get("width"), m.get("height")
    dims = f"{w}×{h}" if (w and h) else "unknown"
    mb = (m.get("size_bytes") or 0) / 1_048_576
    body = (
        f"[dim]Path:[/dim]    {m['path']}\n"
        f"[dim]Date:[/dim]    {m.get('datetime_original') or 'unknown'}\n"
        f"[dim]Camera:[/dim]  {m.get('camera_model') or 'unknown'}\n"
        f"[dim]Size:[/dim]    {mb:.1f} MB  │  {dims}"
    )
    return Panel(body, title=f"[bold {color}]{label}[/bold {color}]",
                 border_style=color, expand=True)


def _open_images(members: list[int], meta: dict) -> None:
    if not hasattr(os, "startfile"):
        console.print("  [dim]Preview only supported on Windows.[/dim]")
        return
    opened = 0
    for f in members:
        p = meta[f]["path"]
        try:
            if Path(p).exists():
                os.startfile(p)  # noqa: S606 — open in default viewer (Windows)
                opened += 1
        except OSError:
            pass
    console.print(f"  [dim]Opened {opened} image(s) in your default viewer.[/dim]")


def review_near_dupes(db: Database) -> None:
    """Interactive near-duplicate review, one CLUSTER at a time."""
    print_phase_header("3B review", "Near-Duplicate Review")

    clusters, meta, cluster_h = _build_near_clusters(db)
    if not clusters:
        print_success("No near-duplicate pairs pending review.")
        return

    known = db.get_known_camera_models()
    # Order: most-similar (lowest hamming) clusters first, larger groups first.
    clusters.sort(key=lambda mem: (cluster_h.get(mem[0], 0), -len(mem)))

    total = len(clusters)
    console.print(f"  {total:,} clusters to review  (most similar first)\n")
    console.print(
        "  [bold]Commands:[/bold]  "
        "[cyan]<n>[/cyan] keep file n (stage the rest)  "
        "[green]k[/green] keep all  "
        "[magenta]v[/magenta] view images  "
        "[dim]s[/dim] skip  [red]q[/red] quit\n"
    )

    reviewed = staged = 0

    for i, members in enumerate(clusters, 1):
        ranked = sorted(members, key=lambda f: keep_score(meta[f], known),
                        reverse=True)
        while True:
            console.rule(
                f"[dim]Cluster {i}/{total}[/dim]  {len(members)} files  "
                f"Hamming≈[bold]{cluster_h.get(members[0], 0)}[/bold]"
            )
            panels = []
            for idx, f in enumerate(ranked, 1):
                color = "cyan" if idx == 1 else "yellow"
                panels.append(_file_panel(f"[{idx}]", meta[f], color))
            console.print(Columns(panels, equal=True))
            console.print("  [dim](listed best-keep first)[/dim]")

            try:
                choice = input("  Choice [<n>/k/v/s/q]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[yellow]Interrupted — progress saved.[/yellow]")
                _finish(reviewed, staged, total)
                return

            if choice == "v":
                _open_images(ranked, meta)
                continue  # redisplay same cluster
            if choice == "q":
                console.print("[yellow]Quit — progress saved.[/yellow]")
                _finish(reviewed, staged, total)
                return
            if choice == "s":
                reviewed += 1
                break
            if choice == "k":
                _record_decision(db, members, None, cluster_h.get(members[0], 0))
                db.commit()
                reviewed += 1
                console.print("  [dim]Kept all.[/dim]\n")
                break
            if choice.isdigit() and 1 <= int(choice) <= len(ranked):
                keeper = ranked[int(choice) - 1]
                _record_decision(db, members, keeper, cluster_h.get(members[0], 0))
                db.commit()
                reviewed += 1
                staged += len(members) - 1
                console.print(f"  [dim]Kept file [{choice}], staged "
                              f"{len(members) - 1} other(s).[/dim]\n")
                break
            console.print("  [dim]Unknown command — try again.[/dim]")

    _finish(reviewed, staged, total)


def _finish(reviewed: int, staged: int, total: int) -> None:
    print_success(
        f"Reviewed {reviewed:,} / {total:,} clusters — "
        f"{staged:,} files marked for deletion (staged when you run plan)."
    )
    if reviewed < total:
        print_warning(
            f"{total - reviewed:,} clusters still pending. "
            "Run this command again to continue."
        )
