"""
reconcile.py — Conservation proof: prove no file was silently lost.

After execute (or a partial execute), this audits the `files` table and proves
that every row lands in exactly ONE terminal state, double-entry-style: the
per-state counts must sum to the total scanned, and UNACCOUNTED must be 0.

This is an elevation of the post-execute summary in executor.py — same
terminology (moved / staged / collisions / skipped / errors), but framed as a
balance sheet over the *whole* library rather than a single run.

Two tiers:
  1. Default (DB-only, fast): balance-sheet table. If UNACCOUNTED != 0, the
     offending file_ids/paths are printed and the command exits non-zero.
  2. --verify-disk (I/O): for every file with a 'done' MOVE/STAGE_DELETE op,
     assert os.path.exists(files.path). A 'done' row whose path is missing on
     disk is a LOST FILE — reported loudly and the command exits non-zero.

Read-only: never moves or deletes anything. Anomalies are logged to run_log
with phase='reconcile'.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from rich import box
from rich.table import Table

from .db import Database
from .progress import (
    PhaseProgress,
    console,
    print_error,
    print_phase_header,
    print_success,
    print_warning,
)

# A moved file whose name carries this suffix was renamed by the executor to
# avoid clobbering an existing destination (see executor.py).
_CONFLICT_RE = re.compile(r"_conflict_\d+$")

# Terminal states, in display order.
MOVED = "moved"
STAGED = "staged"
CONFLICT = "conflict-renamed"
DELETED = "deleted (acknowledged)"
SKIPPED = "skipped/pending"
ERROR = "error"
UNTOUCHED = "untouched-by-design"
UNACCOUNTED = "UNACCOUNTED"

_DONE_OPS = {"MOVE", "STAGE_DELETE"}


def _classify(file_row, ops: list) -> str:
    """
    Assign a single terminal state to one `files` row given its operations.

    Order matters — error wins over everything so failures are never hidden
    behind a (stale) done/pending classification. DELETED is checked next,
    ahead of MOVE/STAGE_DELETE: a file organized by `execute` and LATER
    removed via `sync.acknowledge_deleted` (sync.py, Pillar 2) carries both
    a done MOVE/STAGE_DELETE op (its original organize) and a done DELETE op
    (the later acknowledgment) — DELETED reflects its current, final state.
    """
    file_status = file_row["status"]
    op_statuses = {o["status"] for o in ops}

    # error: surfaced first so nothing masks it.
    if file_status == "error" or "error" in op_statuses:
        return ERROR

    if any(o["op_type"] == "DELETE" and o["status"] == "done" for o in ops):
        return DELETED

    done_move = any(o["op_type"] == "MOVE" and o["status"] == "done" for o in ops)
    done_stage = any(
        o["op_type"] == "STAGE_DELETE" and o["status"] == "done" for o in ops
    )

    if done_move:
        if _CONFLICT_RE.search(Path(file_row["path"]).stem):
            return CONFLICT
        return MOVED
    if done_stage:
        return STAGED

    # Any operation still waiting to run (normal for a partial execute).
    if op_statuses & {"planned", "confirmed", "pending", "skipped"}:
        return SKIPPED

    # No operation at all.
    if not ops:
        if file_row["file_type"] == "UNKNOWN":
            return UNTOUCHED  # left in place by design
        return UNACCOUNTED   # a kept file with no plan — should never happen

    # Has a 'done' op of some other type, or some state we can't classify.
    return UNACCOUNTED


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def reconcile(db: Database, verify_disk: bool = False) -> bool:
    """
    Audit the files table. Returns True if the library balances (UNACCOUNTED==0
    and, with verify_disk, no lost files), False otherwise.
    """
    print_phase_header("reconcile", "Conservation Proof")

    files = db.conn.execute(
        "SELECT file_id, path, file_type, status FROM files ORDER BY file_id"
    ).fetchall()
    total = len(files)

    if total == 0:
        print_warning("No files in the database — nothing to reconcile.")
        return True

    # Group operations by file_id in one pass.
    ops_by_file: dict[int, list] = {}
    for op in db.conn.execute(
        "SELECT file_id, op_type, status FROM operations"
    ).fetchall():
        ops_by_file.setdefault(op["file_id"], []).append(op)

    counts: dict[str, int] = {
        MOVED: 0, STAGED: 0, CONFLICT: 0, DELETED: 0, SKIPPED: 0,
        ERROR: 0, UNTOUCHED: 0, UNACCOUNTED: 0,
    }
    unaccounted: list[tuple[int, str]] = []
    # Files claiming a 'done' MOVE/STAGE — these are what --verify-disk checks.
    done_on_disk: list[tuple[int, str]] = []

    for f in files:
        ops = ops_by_file.get(f["file_id"], [])
        state = _classify(f, ops)
        counts[state] += 1
        if state == UNACCOUNTED:
            unaccounted.append((f["file_id"], f["path"]))
        if state in (MOVED, STAGED, CONFLICT):
            done_on_disk.append((f["file_id"], f["path"]))

    # ── Balance sheet ─────────────────────────────────────────────────────────
    t = Table(title="Reconciliation Balance Sheet", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Terminal state", style="cyan")
    t.add_column("Files", justify="right")
    t.add_column("Notes", style="dim")

    t.add_row("Moved → Masters/ or Others/", f"{counts[MOVED]:,}", "MOVE done")
    t.add_row("Staged → _staging/to_delete/", f"{counts[STAGED]:,}", "STAGE_DELETE done")
    if counts[CONFLICT]:
        t.add_row(
            "[yellow]Conflict-renamed (_conflict_N)[/yellow]",
            f"{counts[CONFLICT]:,}", "moved, name collision",
        )
    if counts[DELETED]:
        t.add_row(
            "Deleted (acknowledged)",
            f"{counts[DELETED]:,}", "removed post-organize via `sync delete`",
        )
    if counts[SKIPPED]:
        t.add_row(
            "[yellow]Skipped / pending[/yellow]",
            f"{counts[SKIPPED]:,}", "op not yet executed",
        )
    if counts[UNTOUCHED]:
        t.add_row(
            "Untouched by design (UNKNOWN)",
            f"{counts[UNTOUCHED]:,}", "no op — left in place",
        )
    if counts[ERROR]:
        t.add_row("[red]Error[/red]", f"[red]{counts[ERROR]:,}[/red]", "files/op status=error")

    accounted = total - counts[UNACCOUNTED]
    t.add_section()
    t.add_row("[bold]Accounted for[/bold]", f"[bold]{accounted:,}[/bold]", "")
    unacc_style = "red" if counts[UNACCOUNTED] else "green"
    t.add_row(
        f"[bold {unacc_style}]UNACCOUNTED[/bold {unacc_style}]",
        f"[bold {unacc_style}]{counts[UNACCOUNTED]:,}[/bold {unacc_style}]",
        "must be 0",
    )
    t.add_row("[bold]Total scanned[/bold]", f"[bold]{total:,}[/bold]", "")

    console.print()
    console.print(t)

    ok = True

    # ── Unaccounted rows (DB-level imbalance) ────────────────────────────────
    if unaccounted:
        ok = False
        console.print()
        print_error(
            f"{len(unaccounted):,} file(s) could not be classified into any "
            "terminal state — the library does NOT balance."
        )
        for fid, path in unaccounted[:50]:
            console.print(f"    [red]file_id={fid}[/red]  {path}")
        if len(unaccounted) > 50:
            console.print(f"    [dim]… +{len(unaccounted) - 50} more[/dim]")
        for fid, path in unaccounted:
            db.log(
                "ERROR", "Unaccounted file — no clean terminal state",
                phase="reconcile", file_id=fid, path=path,
            )
        db.commit()

    # ── Tier 2: verify the claimed locations exist on disk ───────────────────
    if verify_disk:
        console.print()
        console.rule("[dim]--verify-disk — confirming each 'done' file exists[/dim]")
        lost: list[tuple[int, str]] = []
        with PhaseProgress("Verifying", total=len(done_on_disk), phase="reconcile") as prog:
            for fid, path in done_on_disk:
                if not os.path.exists(path):
                    lost.append((fid, path))
                prog.advance(1, current_path=path)

        if lost:
            ok = False
            console.print()
            print_error(
                f"LOST FILES — {len(lost):,} row(s) marked done but MISSING on disk:"
            )
            for fid, path in lost[:50]:
                console.print(f"    [red]file_id={fid}[/red]  {path}")
            if len(lost) > 50:
                console.print(f"    [dim]… +{len(lost) - 50} more[/dim]")
            for fid, path in lost:
                db.log(
                    "ERROR", "LOST FILE — done op but path missing on disk",
                    phase="reconcile", file_id=fid, path=path,
                )
            db.commit()
        else:
            print_success(
                f"All {len(done_on_disk):,} moved/staged files confirmed present on disk."
            )

    # ── Verdict ───────────────────────────────────────────────────────────────
    console.print()
    if ok:
        print_success(
            f"Balanced — all {total:,} scanned files accounted for, "
            "UNACCOUNTED == 0. No file was silently lost."
        )
    else:
        print_error(
            "Reconciliation FAILED — see anomalies above "
            "(also logged: SELECT path, message FROM run_log WHERE phase='reconcile')."
        )
    return ok
