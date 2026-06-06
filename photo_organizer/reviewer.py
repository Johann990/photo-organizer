"""
reviewer.py — Interactive near-duplicate review CLI

Shows each near-duplicate pair with metadata and asks the user to decide:
  [a] keep A (stage B for deletion)
  [b] keep B (stage A for deletion)
  [k] keep both (mark pair resolved, no deletion)
  [s] skip (leave pending for later)
  [q] quit (save progress and exit)

Only files explicitly marked for deletion here are staged; the executor
will not touch near-duplicate files unless they are confirmed here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich import box

from .db import Database
from .progress import console, print_phase_header, print_success, print_warning


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_panel(label: str, path: str, size: int | None,
                dt: str | None, cam: str | None,
                w: int | None, h: int | None,
                color: str) -> Panel:
    size_mb = (size or 0) / 1_048_576
    dims = f"{w}×{h}" if (w and h) else "unknown"
    body = (
        f"[dim]Path:[/dim]    {path}\n"
        f"[dim]Date:[/dim]    {dt or 'unknown'}\n"
        f"[dim]Camera:[/dim]  {cam or 'unknown'}\n"
        f"[dim]Size:[/dim]    {size_mb:.1f} MB  │  {dims}"
    )
    return Panel(body, title=f"[bold {color}]{label}[/bold {color}]",
                 border_style=color, expand=True)


def review_near_dupes(db: Database) -> None:
    """Interactive near-duplicate review session."""
    print_phase_header("3B review", "Near-Duplicate Review")

    pairs = db.conn.execute(
        """
        SELECT d.dup_id,
               d.file_id_a, d.file_id_b,
               d.hamming_distance,
               fa.path           AS path_a,  fa.size_bytes AS size_a,
               fa.datetime_original AS dt_a, fa.camera_model AS cam_a,
               fa.width          AS w_a,     fa.height AS h_a,
               fb.path           AS path_b,  fb.size_bytes AS size_b,
               fb.datetime_original AS dt_b, fb.camera_model AS cam_b,
               fb.width          AS w_b,     fb.height AS h_b
        FROM   duplicates d
        JOIN   files fa ON d.file_id_a = fa.file_id
        JOIN   files fb ON d.file_id_b = fb.file_id
        WHERE  d.dup_type = 'NEAR' AND d.status = 'pending'
        ORDER  BY d.hamming_distance
        """
    ).fetchall()

    if not pairs:
        print_success("No near-duplicate pairs pending review.")
        return

    total = len(pairs)
    console.print(f"  {total:,} pairs to review  (sorted by similarity — most similar first)\n")
    console.print(
        "  [bold]Commands:[/bold]  "
        "[cyan][a][/cyan] keep A  "
        "[yellow][b][/yellow] keep B  "
        "[green][k][/green] keep both  "
        "[dim][s][/dim] skip  "
        "[red][q][/red] quit\n"
    )

    reviewed = staged = 0

    for i, pair in enumerate(pairs, 1):
        console.rule(
            f"[dim]Pair {i}/{total}[/dim]  "
            f"Hamming distance: [bold]{pair['hamming_distance']}[/bold]"
        )

        pa = _file_panel(
            "A", pair["path_a"], pair["size_a"],
            pair["dt_a"], pair["cam_a"], pair["w_a"], pair["h_a"],
            "cyan",
        )
        pb = _file_panel(
            "B", pair["path_b"], pair["size_b"],
            pair["dt_b"], pair["cam_b"], pair["w_b"], pair["h_b"],
            "yellow",
        )
        console.print(Columns([pa, pb], equal=True))
        console.print()

        try:
            choice = input("  Choice [a/b/k/s/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Interrupted — progress saved.[/yellow]")
            break

        if choice == "q":
            console.print("[yellow]Quit — progress saved.[/yellow]")
            break

        if choice == "s":
            reviewed += 1
            continue

        if choice not in ("a", "b", "k"):
            console.print("  [dim]Unknown command — skipped.[/dim]")
            continue

        now = _now()

        if choice == "k":
            # Keep both — just mark reviewed
            db.conn.execute(
                "UPDATE duplicates SET status='reviewed', resolved_at=? WHERE dup_id=?",
                (now, pair["dup_id"]),
            )
        else:
            keep_id = pair["file_id_a"] if choice == "a" else pair["file_id_b"]
            stage_id = pair["file_id_b"] if choice == "a" else pair["file_id_a"]
            stage_path = pair["path_b"] if choice == "a" else pair["path_a"]
            stage_filename = Path(stage_path).name

            db.conn.execute(
                "UPDATE duplicates SET status='reviewed', keep_file_id=?, resolved_at=? "
                "WHERE dup_id=?",
                (keep_id, now, pair["dup_id"]),
            )
            # Insert a STAGE_DELETE operation so Phase 5 will move the loser
            # Use file_id prefix to avoid staging-folder collisions
            staging_target = _get_staging_root(db) / f"{stage_id}_{stage_filename}"
            db.conn.execute(
                """
                INSERT OR IGNORE INTO operations
                    (file_id, op_type, source_path, target_path, status, planned_at)
                VALUES (?, 'STAGE_DELETE', ?, ?, 'confirmed', ?)
                """,
                (stage_id, stage_path, str(staging_target), now),
            )
            db.conn.execute(
                "UPDATE files SET status='confirmed' WHERE file_id=?",
                (stage_id,),
            )
            staged += 1

        db.commit()
        reviewed += 1

        console.print(f"  [dim]Saved.[/dim]\n")

    remaining = total - reviewed
    print_success(f"Reviewed {reviewed:,} / {total:,} pairs — {staged:,} staged for deletion.")
    if remaining > 0:
        print_warning(
            f"{remaining:,} pairs still pending. Run this command again to continue."
        )


def _get_staging_root(db: Database) -> Path:
    """Read target_root from the plan phase summary, or fall back to a safe default."""
    row = db.conn.execute(
        "SELECT summary_json FROM phases WHERE phase_name = 'review'"
    ).fetchone()
    if row and row["summary_json"]:
        import json
        summary = json.loads(row["summary_json"])
        target_root = summary.get("target_root")
        if target_root:
            return Path(target_root) / "_staging" / "near_dupes"
    # Fallback: use a relative path (user should have run plan first)
    return Path("_staging") / "near_dupes"
