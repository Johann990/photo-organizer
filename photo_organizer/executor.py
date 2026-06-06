"""
executor.py — Phase 5: Execute Confirmed Operations

Reads the operations table (status='confirmed') and executes:
  1. Creates target directories as needed
  2. os.rename() — no data copy, instant on same volume
  3. Resolves filename collisions at destination
  4. Updates DB status to 'done' after each successful rename
  5. Verifies total file count before vs after

Only runs after explicit confirmation from Phase 4 (plan).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.table import Table

from .db import Database
from .progress import (
    PhaseProgress,
    console,
    print_phase_header,
    print_success,
    print_warning,
    print_error,
)

DB_COMMIT_EVERY = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def execute(
    db: Database,
    force: bool = False,
    *,
    year: str | None = None,
    camera: str | None = None,
    software: str | None = None,
    file_type: str | None = None,
) -> None:
    """
    Phase 5: Execute confirmed operations.

    Optional filters select a SUBSET to run now (the rest stay 'confirmed' for
    a later run, so you can move in batches):
      year      — EXIF year, e.g. "2023"
      camera    — substring match on camera_model, e.g. "ILCE-7RM2"
      software  — substring match on software, e.g. "Lightroom"
      file_type — exact file_type, e.g. "RAW" / "CAMERA_JPEG" / "DEV_JPEG" / "VIDEO"
    """
    print_phase_header("5/5", "Executing Operations")

    filtered = any([year, camera, software, file_type])

    # When a full (unfiltered) run already completed, skip unless --force.
    # Filtered runs always proceed (there may be remaining batches).
    if not force and not filtered and db.phase_complete("execute"):
        print_success("Phase 5 already complete. Use --force to re-run.")
        return

    # ── Load confirmed operations (optionally filtered) ───────────────────────
    where = ["o.status = 'confirmed'"]
    params: list = []
    if year:
        where.append("substr(f.datetime_original, 1, 4) = ?")
        params.append(str(year))
    if camera:
        where.append("LOWER(f.camera_model) LIKE '%' || LOWER(?) || '%'")
        params.append(camera)
    if software:
        where.append("LOWER(f.software) LIKE '%' || LOWER(?) || '%'")
        params.append(software)
    if file_type:
        where.append("f.file_type = ?")
        params.append(file_type.upper())

    ops = db.conn.execute(
        f"""
        SELECT o.op_id, o.file_id, o.op_type, o.source_path, o.target_path
        FROM   operations o JOIN files f USING(file_id)
        WHERE  {" AND ".join(where)}
        ORDER  BY o.op_type   -- MOVE before STAGE_DELETE (alphabetical)
        """,
        params,
    ).fetchall()

    if filtered:
        active = ", ".join(
            f"{k}={v}" for k, v in
            (("year", year), ("camera", camera), ("software", software), ("type", file_type))
            if v
        )
        console.print(f"  [cyan]Filtered run:[/cyan] {active}")

    if not ops:
        if filtered:
            print_warning("No confirmed operations match this filter.")
        else:
            print_warning(
                "No confirmed operations found. "
                "Run 'plan --db <path> --target <path>' first."
            )
        return

    total = len(ops)
    console.print(f"  Confirmed operations: {total:,}\n")

    # Count before for verification
    files_before: int = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE status NOT IN ('error')"
    ).fetchone()[0]

    db.set_phase_status("execute", "running")

    done = errors = skipped = collisions = 0
    count_by_type: dict[str, int] = {}
    commit_buf = 0

    # ── Execute ───────────────────────────────────────────────────────────────
    with PhaseProgress("Executing", total=total, phase="5/5") as prog:
        for op in ops:
            src = Path(op["source_path"])
            dst = Path(op["target_path"])

            if not src.exists():
                db.conn.execute(
                    "UPDATE operations SET status='skipped', error_msg='source not found' "
                    "WHERE op_id=?",
                    (op["op_id"],),
                )
                db.log(
                    "WARN", "Source not found — already moved?",
                    phase="execute", file_id=op["file_id"], path=str(src),
                )
                skipped += 1
            else:
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)

                    # Resolve collision at destination.
                    # This should not normally happen (original filenames are
                    # unique per source folder). When it does, we never
                    # overwrite — we append _conflict_N AND log it so the user
                    # can investigate the two colliding sources afterwards.
                    if dst.exists() and dst.resolve() != src.resolve():
                        intended = dst
                        stem, suffix = dst.stem, dst.suffix
                        n = 1
                        while dst.exists():
                            dst = dst.parent / f"{stem}_conflict_{n}{suffix}"
                            n += 1
                        collisions += 1
                        db.log(
                            "WARN",
                            f"Name collision: '{intended}' already exists; "
                            f"moved '{src}' to '{dst}' instead.",
                            phase="execute", file_id=op["file_id"], path=str(src),
                        )

                    os.rename(src, dst)

                    now = _now()
                    db.conn.execute(
                        "UPDATE operations SET status='done', executed_at=? WHERE op_id=?",
                        (now, op["op_id"]),
                    )
                    db.conn.execute(
                        "UPDATE files SET status='done', path=?, updated_at=? WHERE file_id=?",
                        (str(dst), now, op["file_id"]),
                    )
                    count_by_type[op["op_type"]] = count_by_type.get(op["op_type"], 0) + 1
                    done += 1

                except OSError as exc:
                    msg = str(exc)
                    # Cross-device rename: Linux EXIF 18, Windows WinError 17
                    if (
                        "cross-device" in msg.lower()
                        or "[Errno 18]" in msg
                        or "WinError 17" in msg
                        or "cannot move" in msg.lower()
                    ):
                        msg += (
                            " — source and target must be on the same drive. "
                            "On Windows: ensure --target uses the same drive letter "
                            "as the source photos (e.g. both on E:\\)."
                        )
                    db.conn.execute(
                        "UPDATE operations SET status='error', error_msg=? WHERE op_id=?",
                        (msg, op["op_id"]),
                    )
                    db.log(
                        "ERROR", msg, phase="execute",
                        file_id=op["file_id"], path=str(src),
                    )
                    errors += 1

            commit_buf += 1
            if commit_buf >= DB_COMMIT_EVERY:
                db.commit()
                commit_buf = 0

            prog.advance(1, current_path=str(src))

    db.commit()

    # ── Summary table ─────────────────────────────────────────────────────────
    t = Table(title="Execution Summary", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Category", style="cyan")
    t.add_column("Count", justify="right")

    t.add_row(
        "Moved to Masters/ or Others/",
        f"{count_by_type.get('MOVE', 0):,}",
    )
    t.add_row(
        "Moved to _staging/to_delete/",
        f"{count_by_type.get('STAGE_DELETE', 0):,}",
    )
    if collisions:
        t.add_row("[yellow]Name collisions (renamed _conflict_N)[/yellow]", f"{collisions:,}")
    if skipped:
        t.add_row("[yellow]Skipped (source not found)[/yellow]", f"{skipped:,}")
    if errors:
        t.add_row("[red]Errors[/red]", f"[red]{errors:,}[/red]")
    t.add_row("[bold]Total[/bold]", f"[bold]{done + skipped + errors:,}[/bold]")

    console.print()
    console.print(t)

    # ── Verification ─────────────────────────────────────────────────────────
    files_done: int = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE status = 'done'"
    ).fetchone()[0]
    files_error: int = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE status = 'error'"
    ).fetchone()[0]
    files_other: int = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE status NOT IN ('done', 'error', 'confirmed')"
    ).fetchone()[0]
    accounted = files_done + files_error + files_other

    console.print(
        f"\n  Verification: {files_before:,} files before  →  "
        f"{accounted:,} accounted for "
        f"([green]{files_done:,}[/green] done, "
        f"[yellow]{files_other:,}[/yellow] pending/unknown, "
        f"[red]{files_error:,}[/red] error)"
    )

    if errors == 0 and skipped == 0:
        print_success(
            f"All {done:,} operations completed successfully. "
            f"No data was permanently deleted — "
            f"staged files live in _staging/to_delete/ for 30 days before final removal."
        )
    else:
        if errors:
            print_error(
                f"{errors:,} operations failed. "
                "Query run_log WHERE phase='execute' AND level='ERROR' for details."
            )
        if skipped:
            print_warning(f"{skipped:,} files skipped (sources were already missing).")
    if collisions:
        print_warning(
            f"{collisions:,} name collisions were renamed (_conflict_N) — nothing was overwritten.\n"
            "  Investigate: SELECT path, message FROM run_log "
            "WHERE phase='execute' AND message LIKE 'Name collision%';"
        )

    remaining: int = db.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE status = 'confirmed'"
    ).fetchone()[0]

    summary = {
        "done": done,
        "errors": errors,
        "skipped": skipped,
        "moved": count_by_type.get("MOVE", 0),
        "staged": count_by_type.get("STAGE_DELETE", 0),
        "remaining": remaining,
    }
    # Only mark the phase complete once nothing is left; a filtered/partial run
    # leaves it 'running' so a later execute can continue without --force.
    if remaining == 0:
        db.set_phase_status("execute", "complete", summary)
    else:
        db.set_phase_status("execute", "running", summary)
        print_warning(
            f"{remaining:,} confirmed operations still pending — "
            "run execute again (optionally with another filter) to continue."
        )

    console.print(
        "\n  [dim]To revert everything moved so far: "
        "[cyan]python -m photo_organizer undo[/cyan][/dim]"
    )


# ---------------------------------------------------------------------------
# Undo — reverse completed operations back to their original locations
# ---------------------------------------------------------------------------

def undo(db: Database, force: bool = False) -> None:
    """
    Revert every completed ('done') operation: move each file from its current
    location back to the original source_path recorded at plan time.

    Uses files.path as the current location (so files renamed _conflict_N on
    execute are still found) and operations.source_path as the original target.
    Never overwrites: if an original path is already occupied, that file is
    left in place and logged. After a successful undo the 'execute' phase is
    reset to 'pending' so the plan can be re-run.
    """
    print_phase_header("undo", "Revert Moved Files")

    rows = db.conn.execute(
        """
        SELECT o.op_id, o.file_id, o.source_path, f.path AS current_path
        FROM   operations o JOIN files f USING(file_id)
        WHERE  o.status = 'done'
        ORDER  BY o.executed_at DESC, o.op_id DESC
        """
    ).fetchall()

    if not rows:
        print_warning("Nothing to undo — no completed operations found.")
        return

    total = len(rows)
    console.print(
        f"  This will move [bold]{total:,}[/bold] files back to their "
        f"original locations (including staged files).\n"
    )

    if not force:
        try:
            ans = input("Proceed with undo? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != "y":
            console.print("[yellow]Cancelled — nothing changed.[/yellow]")
            return

    reverted = errors = skipped = 0
    commit_buf = 0

    with PhaseProgress("Undoing", total=total, phase="undo") as prog:
        for r in rows:
            cur = Path(r["current_path"])
            orig = Path(r["source_path"])

            if not cur.exists():
                db.log(
                    "WARN", "Undo skipped — current file missing",
                    phase="undo", file_id=r["file_id"], path=str(cur),
                )
                skipped += 1
            elif orig.exists() and orig.resolve() != cur.resolve():
                db.log(
                    "ERROR",
                    f"Undo skipped — original path already occupied: {orig}",
                    phase="undo", file_id=r["file_id"], path=str(cur),
                )
                errors += 1
            else:
                try:
                    orig.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(cur, orig)
                    now = _now()
                    db.conn.execute(
                        "UPDATE operations SET status='confirmed', executed_at=NULL "
                        "WHERE op_id=?",
                        (r["op_id"],),
                    )
                    db.conn.execute(
                        "UPDATE files SET status='confirmed', path=?, updated_at=? "
                        "WHERE file_id=?",
                        (str(orig), now, r["file_id"]),
                    )
                    reverted += 1
                except OSError as exc:
                    db.log(
                        "ERROR", f"Undo failed: {exc}",
                        phase="undo", file_id=r["file_id"], path=str(cur),
                    )
                    errors += 1

            commit_buf += 1
            if commit_buf >= DB_COMMIT_EVERY:
                db.commit()
                commit_buf = 0
            prog.advance(1, current_path=str(cur))

    db.commit()

    # Allow the plan to be executed again from a clean slate.
    if reverted and errors == 0 and skipped == 0:
        db.set_phase_status("execute", "pending")

    t = Table(title="Undo Summary", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Category", style="cyan")
    t.add_column("Count", justify="right")
    t.add_row("Reverted to original", f"{reverted:,}")
    if skipped:
        t.add_row("[yellow]Skipped (current file missing)[/yellow]", f"{skipped:,}")
    if errors:
        t.add_row("[red]Errors (original path occupied / IO)[/red]", f"{errors:,}")
    console.print()
    console.print(t)

    if errors == 0 and skipped == 0:
        print_success(f"Undo complete — {reverted:,} files restored to original locations.")
    else:
        print_warning(
            "Some files were not reverted — review with: "
            "SELECT path, message FROM run_log WHERE phase='undo';"
        )
