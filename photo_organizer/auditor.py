"""
auditor.py — DB decision-ledger consistency audit (`photo_organizer audit`).

Read-only, DB-only — never touches disk. Distinct from the other two
DB-health tools already in this project:
  - validate()  — pre-flight: is the ENVIRONMENT ready (config/ExifTool/sample
                  disk scan) before the pipeline ever runs (validator.py).
  - reconcile() — conservation proof: did every scanned file land in exactly
                  one terminal state, did any vanish (reconcile.py).
auditor asks a third, distinct question: is the ledger's OWN DATA internally
consistent, and did past bugs actually get cleaned up. See the
"project-trust-pillars" memory for why this exists (Pillar 1 — DB quality is
the foundation everything downstream inherits).

Idempotent: clears its own prior run_log(phase='audit') rows before each run,
so re-running never accumulates stale findings.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from rich import box
from rich.table import Table

from .db import Database
from .planner import _DATE_AUDIT_FILE_TYPES
from .progress import (
    console,
    print_error,
    print_phase_header,
    print_success,
    print_warning,
)

ERROR = "ERROR"
WARN = "WARN"

_OFFENDER_DISPLAY_LIMIT = 50


@dataclass
class Check:
    name: str
    severity: str
    count: int = 0
    offenders: list[tuple[int | None, str | None, str]] = field(default_factory=list)
    fix_hint: str = ""


@dataclass
class Report:
    checks: list[Check]

    @property
    def exit_ok(self) -> bool:
        return not any(c.severity == ERROR and c.count > 0 for c in self.checks)

    def find(self, name: str) -> Check:
        for c in self.checks:
            if c.name == name:
                return c
        raise KeyError(name)


# ---------------------------------------------------------------------------
# Event-name residue detection.
#
# Catches the bugs fixed in this project's planner._sanitize_event: a
# duplicated YYYYMMDD stamp glued straight onto a CJK name, or a leftover
# "(Nd)" day-count token leaking into the label segment itself.
#
# Deliberately does NOT catch a leftover date-RANGE tail-day on an AMBIGUOUS
# separator (e.g. "19_清水溪" from a "20020413_19 清水溪" source, using '_' or
# '-'). _sanitize_event only strips that when given the folder's actual EXIF
# date range to corroborate the tail day against — a check working purely
# off the already-baked target_path label has no such date context, and a
# bare regex would collide with a real short numeric label ("101_煙火" —
# Taipei 101). Ranges on an UNAMBIGUOUS separator ('~' or '&', e.g.
# "23_NICE_出差" from "20080614~23 NICE 出差") ARE now fixed at the source
# and so should never reach a fresh plan with that residue — if one does,
# it's stale 'done'/pre-fix data, not a current-code gap.
# ---------------------------------------------------------------------------

_EVENT_FOLDER_SEG = re.compile(r"^(\d{4}-\d{2}-\d{2})(\(\d{1,2}d\))?\s+(.+)$")
_RESIDUE_8DIGIT = re.compile(r"\d{8}")
_RESIDUE_LEFTOVER_ND = re.compile(r"^\d{1,2}d(?:_|$)")


def _extract_event_label(target_path: str) -> str | None:
    """Return the label segment of the FIRST path part shaped like an event
    folder ('{YYYY-MM-DD}[(Nd)] {label}'), or None if no such part exists."""
    for part in re.split(r"[\\/]+", target_path):
        m = _EVENT_FOLDER_SEG.match(part)
        if m:
            return m.group(3)
    return None


def _label_has_residue(label: str) -> bool:
    return bool(_RESIDUE_8DIGIT.search(label) or _RESIDUE_LEFTOVER_ND.match(label))


_BASE_NAMES = {"masters", "others"}


def _base_and_event_key(target_path: str) -> tuple[str | None, str | None]:
    """Return (lowercase base, event-folder-name) from a target_path shaped
    like '.../Masters|Others/{YYYY}/{event folder}/...', or (None, None)."""
    parts = re.split(r"[\\/]+", target_path)
    for i, p in enumerate(parts):
        if p.lower() in _BASE_NAMES and i + 2 < len(parts):
            return p.lower(), parts[i + 2]
    return None, None


# ---------------------------------------------------------------------------
# Individual checks — each is read-only and returns one Check.
# ---------------------------------------------------------------------------

def _check_orphan_operations(conn) -> Check:
    rows = conn.execute(
        "SELECT op_id, file_id FROM operations "
        "WHERE file_id NOT IN (SELECT file_id FROM files)"
    ).fetchall()
    # file_id is None here, not r[1]: r[1] is the MISSING/invalid id itself,
    # and run_log.file_id carries its own FK to `files` — logging the
    # dangling id there would trip the very same constraint we're reporting.
    offenders = [
        (None, None, f"operations.op_id={r[0]} references missing file_id={r[1]}")
        for r in rows
    ]
    return Check(
        "orphan_operations", ERROR, len(offenders), offenders,
        "An operations row references a file_id that no longer exists in "
        "`files` — likely left behind by a manual DB edit. Delete the row, "
        "or restore the missing file.",
    )


def _check_dangling_duplicates(conn) -> Check:
    file_ids = {r[0] for r in conn.execute("SELECT file_id FROM files")}
    offenders = []
    for dup_id, a, b, keep in conn.execute(
        "SELECT dup_id, file_id_a, file_id_b, keep_file_id FROM duplicates"
    ):
        missing = [
            label for label, fid in (("file_id_a", a), ("file_id_b", b), ("keep_file_id", keep))
            if fid is not None and fid not in file_ids
        ]
        if missing:
            offenders.append((
                None, None,
                f"duplicates.dup_id={dup_id} dangling ref(s): {', '.join(missing)}",
            ))
    return Check(
        "dangling_duplicates", ERROR, len(offenders), offenders,
        "A duplicates row points at a file_id that no longer exists in "
        "`files`. Delete the row — see memory "
        "[[db-perfolder-query-include-stage-delete]] for how this DB's "
        "dupe bookkeeping (EXACT/NEAR + keep_file_id) works.",
    )


def _check_self_referential_duplicate(conn) -> Check:
    """A duplicates row pairing a file with itself.

    NOT a check for 'reviewed but keep_file_id is NULL' — that combination is
    the DESIGNED encoding for a 'keep all' decision on a NEAR cluster
    (reviewer._record_decision: "keeper=None ... records keep_file_id=NULL
    so plan stages nothing"), and EXACT-type rows never populate
    keep_file_id at all (planner.py:1486 — decided in memory via keep_score,
    never written back). Flagging either as an error would be a false
    positive against this project's own by-design schema usage.
    """
    rows = conn.execute(
        "SELECT dup_id FROM duplicates WHERE file_id_a = file_id_b"
    ).fetchall()
    offenders = [
        (None, None, f"duplicates.dup_id={r[0]} pairs a file with itself (file_id_a = file_id_b)")
        for r in rows
    ]
    return Check(
        "self_referential_duplicate", ERROR, len(offenders), offenders,
        "A duplicates row has file_id_a == file_id_b — degenerate data from "
        "a matching bug. Delete the row.",
    )


def _check_target_path_collision(conn) -> Check:
    rows = conn.execute(
        "SELECT target_path, COUNT(DISTINCT file_id) AS n FROM operations "
        "WHERE op_type='MOVE' AND status='done' AND target_path IS NOT NULL "
        "GROUP BY target_path HAVING n > 1"
    ).fetchall()
    offenders = [
        (None, r[0], f"{r[1]} different files all landed at: {r[0]}")
        for r in rows
    ]
    return Check(
        "target_path_collision", ERROR, len(offenders), offenders,
        "Two different files both ended up pointing at the same on-disk "
        "path — one silently overwrote the other, or the DB double-booked "
        "a destination instead of using the executor's _conflict_N "
        "suffixing. Inspect both files' history before touching disk.",
    )


def _check_event_name_residue(
    conn, status_filter: tuple[str, ...], name: str, severity: str
) -> Check:
    placeholders = ",".join("?" * len(status_filter))
    rows = conn.execute(
        f"SELECT file_id, target_path FROM operations "
        f"WHERE op_type='MOVE' AND status IN ({placeholders}) "
        f"AND target_path IS NOT NULL",
        status_filter,
    ).fetchall()
    offenders = []
    for fid, target_path in rows:
        label = _extract_event_label(target_path)
        if label and _label_has_residue(label):
            offenders.append((
                fid, target_path,
                f"event label still carries a date stamp: '{label}'",
            ))
    if severity == WARN:
        hint = (
            "Not yet on disk — re-run `plan --force` to regenerate this "
            "with the current _sanitize_event; it self-heals for free."
        )
    else:
        hint = (
            "Already landed on disk under the wrong name — a re-plan will "
            "NOT touch it (done ops are frozen). Needs a surgical DB + "
            "folder rename, one folder at a time."
        )
    return Check(name, severity, len(offenders), offenders, hint)


def _check_cross_base_empty_twin(conn) -> Check:
    move_bases: dict[str, set[str]] = defaultdict(set)
    stage_bases: dict[str, set[str]] = defaultdict(set)
    for op_type, target_path in conn.execute(
        "SELECT op_type, target_path FROM operations "
        "WHERE op_type IN ('MOVE','STAGE_DELETE') AND status='done' "
        "AND target_path IS NOT NULL"
    ):
        base, key = _base_and_event_key(target_path)
        if not base or not key:
            continue
        (move_bases if op_type == "MOVE" else stage_bases)[key].add(base)

    offenders = []
    for key, stage_set in stage_bases.items():
        moved_set = move_bases.get(key, set())
        empty_bases = stage_set - moved_set
        if empty_bases and moved_set:
            for b in sorted(empty_bases):
                offenders.append((
                    None, None,
                    f"'{key}' exists empty under {b}/ — all its files were "
                    f"staged-out duplicates of the copy under "
                    f"{sorted(moved_set)}/",
                ))
    return Check(
        "cross_base_empty_twin", WARN, len(offenders), offenders,
        "The same event folder name exists under both Masters/ and Others/ "
        "but one side has zero surviving (MOVEd) files — an empty folder "
        "will be left on disk. Safe to rmdir once confirmed empty.",
    )


def _check_low_confidence_dates(conn) -> Check:
    # Scoped to exactly the file types planner._DATE_AUDIT_FILE_TYPES rates
    # (RAW/CAMERA_JPEG/DEV_JPEG/HEIC/VIDEO). RESIZED_JPEG and UNKNOWN are
    # deliberately skipped by that audit (planner.py:1381) because they're
    # headed for STAGE_DELETE, not date-based filing — a stale or absent
    # date_confidence on them carries no misfiling risk and would otherwise
    # inflate this count with noise (647 RESIZED_JPEG rows still carrying a
    # LOW value from before that exclusion existed, observed 2026-06-22).
    placeholders = ",".join("?" * len(_DATE_AUDIT_FILE_TYPES))
    n = conn.execute(
        f"SELECT COUNT(*) FROM files WHERE file_type IN ({placeholders}) "
        f"AND (date_confidence='LOW' OR date_confidence IS NULL)",
        _DATE_AUDIT_FILE_TYPES,
    ).fetchone()[0]
    return Check(
        "low_confidence_dates", WARN, n, [],
        "Files with a LOW or not-yet-audited date confidence may be filed "
        "into the wrong year. Run `plan --dates-only`, then review: "
        "SELECT path, message FROM run_log WHERE phase='review' AND "
        "message LIKE 'Suspicious-date%';",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def audit(db: Database) -> Report:
    """Run all consistency checks and return a Report. Prints a summary
    table and logs every finding to run_log(phase='audit')."""
    conn = db.conn
    conn.execute("DELETE FROM run_log WHERE phase='audit'")

    checks = [
        _check_orphan_operations(conn),
        _check_dangling_duplicates(conn),
        _check_self_referential_duplicate(conn),
        _check_target_path_collision(conn),
        _check_event_name_residue(conn, ("done",), "event_name_residue_done", ERROR),
        _check_event_name_residue(
            conn, ("planned", "confirmed"), "event_name_residue_pending", WARN
        ),
        _check_cross_base_empty_twin(conn),
        _check_low_confidence_dates(conn),
    ]

    for check in checks:
        for fid, path, message in check.offenders:
            db.log(check.severity, f"[{check.name}] {message}",
                   phase="audit", file_id=fid, path=path)
    db.commit()

    _print_report(checks)
    return Report(checks)


def _print_report(checks: list[Check]) -> None:
    print_phase_header("audit", "DB Consistency Audit")

    if not any(c.count for c in checks):
        print_success(
            "All checks clean — 0 findings across orphan refs, dangling "
            "dupes, unresolved reviews, path collisions, event-name "
            "residue, empty twins, and low-confidence dates."
        )
        return

    t = Table(title="Audit Findings", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Check", style="cyan")
    t.add_column("Severity")
    t.add_column("Count", justify="right")
    t.add_column("Fix hint", style="dim")

    any_error = False
    for c in checks:
        if c.count == 0:
            continue
        style = "red" if c.severity == ERROR else "yellow"
        any_error = any_error or c.severity == ERROR
        t.add_row(
            c.name, f"[{style}]{c.severity}[/{style}]",
            f"[{style}]{c.count:,}[/{style}]", c.fix_hint,
        )

    console.print()
    console.print(t)

    for c in checks:
        if c.count == 0 or not c.offenders:
            continue
        console.print()
        console.rule(f"[dim]{c.name}[/dim]")
        for _fid, _path, message in c.offenders[:_OFFENDER_DISPLAY_LIMIT]:
            console.print(f"    {message}")
        if len(c.offenders) > _OFFENDER_DISPLAY_LIMIT:
            console.print(
                f"    … +{len(c.offenders) - _OFFENDER_DISPLAY_LIMIT} more "
                f"(see run_log WHERE phase='audit')"
            )

    console.print()
    if any_error:
        print_error(
            "Audit FAILED — ERROR-severity findings above must be fixed "
            "before trusting this DB."
        )
    else:
        print_warning("Audit passed with WARN-only findings — review at your convenience.")
