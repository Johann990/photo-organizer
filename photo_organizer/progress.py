"""
progress.py — Rich-based progress display for long-running phases.

Provides a consistent look across all phases:

  Phase 1/5 — Scanning
  [████████████░░░░░░░░] 62%  186,432 / 300,000 files
  Current: /Volumes/Photos/2018/Japan/
  Speed: 1,842 files/sec  │  Elapsed: 1m 41s  │  ETA: 1m 01s
  Skipped (resume): 62,100  │  Errors: 12
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Generator

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

console = Console()


# ---------------------------------------------------------------------------
# Phase progress tracker
# ---------------------------------------------------------------------------

class PhaseProgress:
    """
    Context manager for tracking progress of a single phase.

    Usage::

        with PhaseProgress("Scanning", total=300_000, phase="1/5") as p:
            for batch in batches:
                process(batch)
                p.advance(len(batch), current_path="/Volumes/...")
    """

    def __init__(
        self,
        description: str,
        total: int,
        phase: str = "",
        skipped: int = 0,
    ):
        self.description = description
        self.total = total
        self.phase_label = phase
        self.skipped = skipped
        self._errors = 0
        self._current_path = ""
        self._start = time.monotonic()

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            "[progress.percentage]{task.percentage:>5.1f}%",
            MofNCompleteColumn(),
            "files",
            TimeElapsedColumn(),
            "ETA",
            TimeRemainingColumn(),
            console=console,
            refresh_per_second=4,
        )
        self._task: TaskID | None = None
        self._live: Live | None = None

    def __enter__(self) -> "PhaseProgress":
        label = f"Phase {self.phase_label} — {self.description}" if self.phase_label else self.description
        self._task = self._progress.add_task(label, total=self.total)
        self._progress.start()
        return self

    def __exit__(self, *_):
        self._progress.stop()
        elapsed = time.monotonic() - self._start
        console.print(
            f"\n✓ [green]{self.description} complete[/green]  "
            f"[dim]{self._format_elapsed(elapsed)}  │  "
            f"Errors: {self._errors}[/dim]"
        )

    def advance(self, n: int = 1, current_path: str = "", errors: int = 0):
        """Call after processing each batch."""
        self._errors += errors
        self._current_path = current_path
        if self._task is not None:
            self._progress.advance(self._task, n)
            # Update description with live stats
            elapsed = time.monotonic() - self._start
            completed = self._progress.tasks[self._task].completed
            speed = completed / elapsed if elapsed > 0 else 0
            suffix = (
                f"  [dim]{speed:,.0f} files/sec  │  "
                f"Skipped: {self.skipped:,}  │  "
                f"Errors: {self._errors}"
            )
            if current_path:
                short = current_path[-60:] if len(current_path) > 60 else current_path
                suffix += f"\n  [dim italic]{short}[/dim italic]"
            # Rich doesn't support newlines in task description well,
            # so we just update the postfix via description
            label = (
                f"Phase {self.phase_label} — {self.description}"
                if self.phase_label
                else self.description
            )
            self._progress.update(self._task, description=label + suffix)

    def increment_errors(self, n: int = 1):
        self._errors += n

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"


# ---------------------------------------------------------------------------
# Simple spinner for indeterminate operations
# ---------------------------------------------------------------------------

@contextmanager
def spinner(message: str) -> Generator[None, None, None]:
    """Display a spinner for an operation with unknown duration."""
    with console.status(f"[bold]{message}[/bold]"):
        yield


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def print_phase_header(phase: str, title: str):
    console.rule(f"[bold cyan]Phase {phase} — {title}[/bold cyan]")


def print_success(msg: str):
    console.print(f"[bold green]✓[/bold green] {msg}")


def print_warning(msg: str):
    console.print(f"[bold yellow]⚠[/bold yellow] {msg}")


def print_error(msg: str):
    console.print(f"[bold red]✗[/bold red] {msg}")


def print_info(msg: str):
    console.print(f"  [dim]{msg}[/dim]")
