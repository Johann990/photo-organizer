"""
reclassifier.py — Re-run file_type classification using the DB only.

No disk access, no ExifTool: it reuses the `software` and `width` values that
were already stored at scan time. Use this to pick up new classification rules
(e.g. the DEV_JPEG split) without re-scanning the whole drive.

Only CAMERA_JPEG / DEV_JPEG rows are considered — those are the only types
whose classification can change under the DEV_JPEG rule. RAW, VIDEO, RESIZED
and UNKNOWN are left untouched. The operation is idempotent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich import box
from rich.table import Table

from .classifier import FileClassifier
from .db import Database
from .progress import PhaseProgress, console, print_phase_header, print_success

DB_COMMIT_EVERY = 1000


def reclassify(db: Database, use_secondary_signals: bool = False) -> dict[str, Any]:
    """
    Recompute file_type for CAMERA_JPEG / DEV_JPEG rows from stored metadata.

    Returns a summary dict with the count of changed rows and the transition
    breakdown (e.g. {"CAMERA_JPEG→DEV_JPEG": 8432}).
    """
    print_phase_header("reclassify", "Re-classify from DB (no disk read)")

    classifier = FileClassifier(use_secondary_signals=use_secondary_signals)

    rows = db.conn.execute(
        "SELECT file_id, path, file_type, software, width FROM files "
        "WHERE file_type IN ('CAMERA_JPEG', 'DEV_JPEG')"
    ).fetchall()

    total = len(rows)
    if total == 0:
        print_success("No CAMERA_JPEG / DEV_JPEG rows to reclassify.")
        return {"total": 0, "changed": 0, "transitions": {}}

    transitions: dict[str, int] = {}
    changed = 0
    commit_buf = 0

    with PhaseProgress("Reclassifying", total=total, phase="reclassify") as prog:
        for r in rows:
            # Reconstruct the minimal EXIF dict classify() needs — no disk read.
            exif = {"Software": r["software"], "ImageWidth": r["width"]}
            old = r["file_type"]
            new = classifier.classify(Path(r["path"]), exif)

            if new != old:
                db.update_file(r["file_id"], file_type=new)
                key = f"{old}→{new}"
                transitions[key] = transitions.get(key, 0) + 1
                changed += 1
                commit_buf += 1
                if commit_buf >= DB_COMMIT_EVERY:
                    db.commit()
                    commit_buf = 0

            prog.advance(1)

    db.commit()

    # ── Summary ──────────────────────────────────────────────────────────────
    t = Table(title="Reclassification Summary", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Transition", style="cyan")
    t.add_column("Files", justify="right")
    if transitions:
        for key, n in sorted(transitions.items(), key=lambda kv: -kv[1]):
            t.add_row(key, f"{n:,}")
    else:
        t.add_row("[dim]no changes[/dim]", "0")
    console.print()
    console.print(t)

    print_success(
        f"Reclassified {changed:,} of {total:,} JPEG rows "
        f"(no files touched — DB only)."
    )
    return {"total": total, "changed": changed, "transitions": transitions}
