"""
reporter.py — Phase 2: Scan Report

Queries the DB and prints a human-readable summary.
No files are touched. User reviews before proceeding.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .db import Database
from .progress import console, print_phase_header, print_success

from rich.table import Table
from rich.panel import Panel
from rich import box


def report(db: Database) -> dict:
    """
    Phase 2: Generate and print the scan report.
    Returns summary dict.
    """
    print_phase_header("2/5", "Scan Report")

    total = db.total_files()
    if total == 0:
        console.print("[yellow]No files in database. Run scan first.[/yellow]")
        return {}

    by_type   = db.count_by_type()
    cameras   = db.camera_model_counts()
    date_min, date_max = db.date_range()
    no_date   = db.no_exif_date_count()
    res       = db.resolution_buckets()

    # ── Overview table ─────────────────────────────────────────────────────
    overview = Table(title="File Type Breakdown", box=box.SIMPLE_HEAVY, show_header=True)
    overview.add_column("Type",       style="cyan")
    overview.add_column("Count",      justify="right")
    overview.add_column("% of total", justify="right")

    type_order = ["RAW", "CAMERA_JPEG", "DEV_JPEG", "RESIZED_JPEG", "VIDEO", "UNKNOWN"]
    for ft in type_order:
        n = by_type.get(ft, 0)
        pct = n / total * 100 if total else 0
        overview.add_row(ft, f"{n:,}", f"{pct:.1f}%")
    overview.add_row("[bold]TOTAL[/bold]", f"[bold]{total:,}[/bold]", "100%")

    console.print(overview)

    # ── Resolution breakdown ───────────────────────────────────────────────
    res_table = Table(title="JPEG Resolution Distribution", box=box.SIMPLE, show_header=True)
    res_table.add_column("Bucket",  style="cyan")
    res_table.add_column("Count",   justify="right")
    for bucket, n in res.items():
        res_table.add_row(bucket, f"{n:,}")
    console.print(res_table)

    # ── Date range ─────────────────────────────────────────────────────────
    console.print(
        f"\n[bold]Date range:[/bold]  "
        f"{date_min or 'unknown'} → {date_max or 'unknown'}\n"
        f"  No EXIF date: [yellow]{no_date:,}[/yellow] files"
    )

    # ── Camera models ──────────────────────────────────────────────────────
    cam_table = Table(title="Camera Models", box=box.SIMPLE, show_header=True)
    cam_table.add_column("Model",   style="cyan")
    cam_table.add_column("Files",   justify="right")
    for model, n in cameras[:20]:   # top 20
        cam_table.add_row(model, f"{n:,}")
    if len(cameras) > 20:
        cam_table.add_row(f"… +{len(cameras)-20} more", "")
    console.print(cam_table)

    # ── Ratings & keywords (if any exist) ─────────────────────────────────
    if db.has_any_metadata_field():
        ratings = db.ratings_distribution()
        rated_total = sum(n for r, n in ratings.items() if r is not None and r > 0)

        if rated_total > 0:
            rat_table = Table(title="Star Ratings", box=box.SIMPLE, show_header=True)
            rat_table.add_column("Rating", style="cyan")
            rat_table.add_column("Count",  justify="right")
            for stars in range(5, 0, -1):
                n = ratings.get(stars, 0)
                if n:
                    rat_table.add_row("★" * stars + "☆" * (5 - stars), f"{n:,}")
            unrated = ratings.get(None, 0) + ratings.get(0, 0)
            rat_table.add_row("[dim]No rating[/dim]", f"[dim]{unrated:,}[/dim]")
            console.print(rat_table)

        top_kws = db.top_keywords(20)
        if top_kws:
            kw_table = Table(title="Top Keywords", box=box.SIMPLE, show_header=True)
            kw_table.add_column("Keyword", style="cyan")
            kw_table.add_column("Files",   justify="right")
            for kw, n in top_kws:
                kw_table.add_row(kw, f"{n:,}")
            console.print(kw_table)

    # ── Videos (if any exist) ──────────────────────────────────────────────
    video_n = by_type.get("VIDEO", 0)
    if video_n:
        total_dur = db.conn.execute(
            "SELECT COALESCE(SUM(duration_seconds), 0) FROM files WHERE file_type = 'VIDEO'"
        ).fetchone()[0] or 0
        hrs = int(total_dur // 3600)
        mins = int((total_dur % 3600) // 60)

        vid_table = Table(title="Videos", box=box.SIMPLE, show_header=True)
        vid_table.add_column("Metric", style="cyan")
        vid_table.add_column("Value",  justify="right")
        vid_table.add_row("Count", f"{video_n:,}")
        vid_table.add_row("Total duration", f"{hrs:,}h {mins:02d}m")
        console.print(vid_table)

        codec_rows = db.conn.execute(
            """
            SELECT COALESCE(video_codec, 'unknown') AS codec, COUNT(*) AS n
            FROM files WHERE file_type = 'VIDEO'
            GROUP BY codec ORDER BY n DESC LIMIT 10
            """
        ).fetchall()
        if codec_rows:
            codec_table = Table(title="Video Codecs", box=box.SIMPLE, show_header=True)
            codec_table.add_column("Codec", style="cyan")
            codec_table.add_column("Files", justify="right")
            for r in codec_rows:
                codec_table.add_row(str(r["codec"]), f"{r['n']:,}")
            console.print(codec_table)

    # ── Duplicate estimate ─────────────────────────────────────────────────
    resized_count = by_type.get("RESIZED_JPEG", 0)
    console.print(
        f"\n[bold]Quick estimates:[/bold]\n"
        f"  Resized JPEGs (safe to delete):  [red]{resized_count:,}[/red]\n"
        f"  Run Phase 3 for duplicate analysis."
    )

    # ── Next step prompt ───────────────────────────────────────────────────
    console.print(
        Panel(
            "[bold]Review the report above.[/bold]\n\n"
            "If the resized JPEG count or any camera model looks wrong, "
            "adjust classification rules before continuing.\n\n"
            "When ready:  [cyan]python -m photo_organizer dedup --db <path>[/cyan]",
            title="Next Step",
            border_style="green",
        )
    )

    db.set_phase_status("report", "complete", {
        "total": total,
        "by_type": by_type,
        "no_date": no_date,
    })

    return {"total": total, "by_type": by_type}


# ---------------------------------------------------------------------------
# Unknown-camera distribution (DB-only, no disk read)
# ---------------------------------------------------------------------------

def _counter_table(title: str, counter: Counter, *, top: int | None = None,
                   by_key: bool = False, label: str = "Value") -> Table:
    t = Table(title=title, box=box.SIMPLE, show_header=True)
    t.add_column(label, style="cyan")
    t.add_column("Files", justify="right")
    items = sorted(counter.items()) if by_key else counter.most_common(top)
    for key, n in items:
        t.add_row(str(key), f"{n:,}")
    remaining = len(counter) - len(items)
    if remaining > 0:
        t.add_row(f"[dim]… +{remaining:,} more[/dim]", "")
    return t


def report_unknown_cameras(db: Database) -> dict:
    """
    Show how files with NO camera model break down — by type, year, make,
    software and source folder. DB-only; touches no files.
    """
    print_phase_header("unknown", "Unknown Camera Model — distribution")

    rows = db.conn.execute(
        "SELECT file_type, datetime_original, camera_make, software, path "
        "FROM files WHERE camera_model IS NULL OR camera_model = ''"
    ).fetchall()

    total_all = db.total_files()
    n = len(rows)
    if n == 0:
        print_success("No files with an unknown camera model. 🎉")
        return {"unknown": 0, "total": total_all}

    pct = n / total_all * 100 if total_all else 0
    console.print(
        f"\n  Unknown-camera files: [bold]{n:,}[/bold] of {total_all:,} "
        f"([yellow]{pct:.1f}%[/yellow])\n"
    )

    by_type, by_year, by_make, by_soft, by_folder = (
        Counter(), Counter(), Counter(), Counter(), Counter()
    )
    for r in rows:
        by_type[r["file_type"]] += 1
        by_year[(r["datetime_original"] or "")[:4] or "(no date)"] += 1
        by_make[r["camera_make"] or "(no make)"] += 1
        by_soft[(r["software"] or "").strip() or "(no software)"] += 1
        by_folder[str(Path(r["path"]).parent)] += 1

    console.print(_counter_table("By file type", by_type, label="file_type"))
    console.print(_counter_table("By year", by_year, by_key=True, label="year"))
    console.print(_counter_table("By camera make", by_make, top=20, label="make"))
    console.print(_counter_table("By software", by_soft, top=20, label="software"))
    console.print(_counter_table("By source folder", by_folder, top=30, label="folder"))

    print_success(
        f"{n:,} unknown-camera files summarised (DB only — no files touched). "
        "These move to Others/ unless their model is added to known_cameras."
    )
    return {
        "unknown": n, "total": total_all,
        "by_type": dict(by_type), "by_year": dict(by_year),
    }
