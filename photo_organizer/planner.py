"""
planner.py — Phase 4: Action Plan Generator

Queries the DB and builds a complete list of operations:
  - STAGE_DELETE for resized JPEGs and exact-duplicate non-keepers
  - MOVE+RENAME for all kept files into the target directory structure
  - Surface near-duplicate pairs for human review (never auto-staged)

No files are touched here.  User reviews the summary and must type 'y'
to confirm before Phase 5 can run.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from rich import box
from rich.table import Table

from .db import Database
from .folders_report import write_no_event_report
from .progress import console, print_phase_header, print_success


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_exif_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    raw = s.strip()
    # Strip a trailing timezone offset such as "+08:00" / "-05:00" / "Z".
    # Apple/QuickTime CreationDate (video) embeds it; we keep the wall-clock time.
    raw = re.sub(r"(?:Z|[+-]\d{2}:?\d{2})$", "", raw).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _parse_mtime(s: str | None) -> datetime | None:
    """
    Parse the stored filesystem mtime (ISO-8601, e.g. '2023-06-15T10:30:00+00:00').
    Used only as a LAST-RESORT date when a file has no EXIF date — mtime can be
    unreliable (copying may reset it), so it is preferred over NoDate/ but always
    logged.  Returns a naive datetime (tz dropped) to match _parse_exif_dt.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Date forensics — filename-embedded timestamps + confidence-rated resolution
# ---------------------------------------------------------------------------

# In a 15-year library EXIF dates are often faked (Android screenshots),
# stripped (WhatsApp/Telegram → unreliable mtime) or mtime is reset by a
# FAT32/exFAT copy. We never trust a single signal blindly: we cross-check
# the EXIF DateTimeOriginal against the date the capturing app embedded in
# the FILENAME, and tag every resolved date with a confidence + source so
# suspicious ones can be surfaced before a file is mis-filed into the wrong year.

MIN_PLAUSIBLE_YEAR = 1990
# Bit-rot / FAT-epoch sentinels cameras and bad clocks emit; never a real capture.
_FAKE_SENTINEL_DATES = {date(1980, 1, 1), date(2000, 1, 1)}
# EXIF and filename within this many days → corroborated (HIGH).
_AGREE_DAYS = 1
# EXIF and filename more than this many days apart → contradiction (filename wins, LOW).
_CONTRADICT_DAYS = 2

# Filename timestamp patterns, tried in order; each captures named groups
# y/mon/day (always) and optional h/min/sec. Anchored at the start of the
# stem so a stray digit run mid-name can't masquerade as a date.
_FILENAME_PATTERNS = [
    # Google Pixel: PXL_20211103_080910123  (trailing milliseconds dropped)
    re.compile(
        r"^PXL_(?P<y>\d{4})(?P<mon>\d{2})(?P<day>\d{2})_"
        r"(?P<h>\d{2})(?P<min>\d{2})(?P<sec>\d{2})\d*", re.IGNORECASE),
    # IMG_/VID_/MVIMG_ with time: IMG_20230615_143022
    re.compile(
        r"^(?:IMG|VID|MVIMG|MVI)[_-](?P<y>\d{4})(?P<mon>\d{2})(?P<day>\d{2})"
        r"[_-](?P<h>\d{2})(?P<min>\d{2})(?P<sec>\d{2})", re.IGNORECASE),
    # Android screenshot with time: Screenshot_20230615-143022
    #   or Screenshot_2023-06-15-14-30-22  (dashes optional throughout)
    re.compile(
        r"^Screenshot[_-](?P<y>\d{4})-?(?P<mon>\d{2})-?(?P<day>\d{2})"
        r"[-_](?P<h>\d{2})-?(?P<min>\d{2})-?(?P<sec>\d{2})", re.IGNORECASE),
    # WhatsApp: IMG-20230615-WA0001  (no time component)
    re.compile(
        r"^(?:IMG|VID)-(?P<y>\d{4})(?P<mon>\d{2})(?P<day>\d{2})-WA\d+",
        re.IGNORECASE),
    # Signal: Signal-2023-06-15-143022  or  Signal-2023-06-15
    re.compile(
        r"^Signal[-_](?P<y>\d{4})-(?P<mon>\d{2})-(?P<day>\d{2})"
        r"(?:[-_T](?P<h>\d{2})(?P<min>\d{2})(?P<sec>\d{2}))?", re.IGNORECASE),
    # IMG_/VID_ date only: IMG_20230615  (no time, not followed by more digits)
    re.compile(
        r"^(?:IMG|VID|MVIMG|MVI)[_-](?P<y>\d{4})(?P<mon>\d{2})(?P<day>\d{2})(?!\d)",
        re.IGNORECASE),
    # Screenshot date only: Screenshot_2023-06-15 / Screenshot_20230615
    re.compile(
        r"^Screenshot[_-](?P<y>\d{4})-?(?P<mon>\d{2})-?(?P<day>\d{2})(?!\d)",
        re.IGNORECASE),
    # Bare leading date + sequence: 20230615_123456 / 20230615-001
    re.compile(
        r"^(?P<y>\d{4})(?P<mon>\d{2})(?P<day>\d{2})[_-]\d{3,}", re.IGNORECASE),
]


def _parse_filename_dt(filename: str | None) -> datetime | None:
    """
    Extract a capture timestamp embedded in a filename (e.g. IMG_20230615_143022,
    PXL_…, Screenshot_…, IMG-…-WA####, Signal-…). Returns a naive datetime when
    the digits form a valid calendar date (midnight when no time is present),
    else None. Conservative: an out-of-range month/day yields None, not a guess.

    Rationale: messaging apps strip EXIF, but the capturing app named the file at
    capture time — so the filename date is often MORE reliable than residual EXIF.
    """
    if not filename:
        return None
    stem = Path(filename).stem or filename
    for pat in _FILENAME_PATTERNS:
        m = pat.match(stem)
        if not m:
            continue
        g = m.groupdict()
        try:
            return datetime(
                int(g["y"]), int(g["mon"]), int(g["day"]),
                int(g.get("h") or 0), int(g.get("min") or 0), int(g.get("sec") or 0),
            )
        except (TypeError, ValueError):
            continue  # invalid calendar date/time → try the next pattern
    return None


def _is_sane_date(dt: datetime | None, today: date) -> bool:
    """A date we can trust as real: not None, not in the future, not absurdly
    old, and not a known fake sentinel (FAT epoch / bad-clock default)."""
    if dt is None:
        return False
    if dt.year < MIN_PLAUSIBLE_YEAR:
        return False
    if dt.date() > today:
        return False
    if dt.date() in _FAKE_SENTINEL_DATES:
        return False
    return True


def _resolve_date(
    row: Any, today: date | None = None
) -> tuple[datetime | None, str, str | None]:
    """
    Resolve a file's date with a confidence rating and the signal it came from.

    Returns (datetime_or_None, source, confidence) where
        source     ∈ {exif_original, filename, exif_digitized, mtime, none}
        confidence ∈ {HIGH, MEDIUM, LOW}  (None when source == 'none')

    Resolution ladder (first applicable wins):
      1. camera_model present AND DateTimeOriginal sane → exif_original, HIGH
         (real cameras don't fake DateTimeOriginal)
      2. DateTimeOriginal and filename-date agree within ~1 day → exif_original, HIGH
      3. filename-date sane and contradicts DateTimeOriginal by > ~2 days
         → filename, LOW   (EXIF was faked/injected; the capture app's name wins)
      4. DateTimeOriginal sane (no corroboration, no camera) → exif_original, MEDIUM
      5. datetime_digitized sane → exif_digitized, MEDIUM
      6. mtime present → mtime, LOW   (last resort; may be a copy date)
      7. otherwise → None, none

    `today` is injectable so future-date detection is deterministic in tests.
    """
    if today is None:
        today = datetime.now().date()

    exif = _parse_exif_dt(row["datetime_original"])
    fname = _parse_filename_dt(row["filename"])
    digit = _parse_exif_dt(row["datetime_digitized"])
    mt = _parse_mtime(row["mtime"])
    has_camera = bool(row["camera_model"])

    exif_sane = _is_sane_date(exif, today)
    fname_sane = _is_sane_date(fname, today)

    # 1. Trusted camera with a sane capture time.
    if has_camera and exif_sane:
        return exif, "exif_original", "HIGH"

    # 2 & 3. Cross-check EXIF against the filename date when both are present.
    if exif_sane and fname_sane:
        day_gap = abs((exif.date() - fname.date()).days)
        if day_gap <= _AGREE_DAYS:
            return exif, "exif_original", "HIGH"
        if day_gap > _CONTRADICT_DAYS:
            return fname, "filename", "LOW"
        # 1 < gap <= 2 days: ambiguous → fall through to MEDIUM EXIF.

    # 3b. EXIF absent/insane but a sane filename date exists → use it.
    # For videos (which carry no EXIF to contradict), the filename timestamp IS
    # the camera's capture date — trust it (MEDIUM), don't bury it at LOW.
    if not exif_sane and fname_sane:
        conf = "MEDIUM" if row["file_type"] == "VIDEO" else "LOW"
        return fname, "filename", conf

    # 4. Lone sane EXIF, no corroboration, no camera.
    if exif_sane:
        return exif, "exif_original", "MEDIUM"

    # 5. DateTimeDigitized as a secondary EXIF date.
    if _is_sane_date(digit, today):
        return digit, "exif_digitized", "MEDIUM"

    # 6. Filesystem mtime — always low confidence (a copy may have reset it).
    if mt is not None:
        return mt, "mtime", "LOW"

    # 7. No usable date at all.
    return None, "none", None


def _effective_date(row: Any) -> tuple[datetime | None, bool]:
    """
    Backward-compatible shim over _resolve_date for callers that only need
    (date, used_mtime). Retained so existing path-building / counting code is
    unchanged while date resolution now goes through the forensics ladder.
    """
    dt, source, _ = _resolve_date(row)
    return dt, source == "mtime"


def _parse_override_date(s: str | None) -> datetime | None:
    """Parse a folder_overrides.date_override 'YYYY-MM-DD' string."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _sibling_date_hints(db: Database) -> dict[str, datetime]:
    """For each source folder, a representative capture date taken from the
    folder's confidently-dated NON-VIDEO photos (HIGH/MEDIUM). Used to date
    videos in that folder that have no capture time of their own. Representative
    date = the most common photo date in the folder (ties → earliest)."""
    from collections import Counter, defaultdict

    by_folder: dict[str, Counter] = defaultdict(Counter)
    for r in db.conn.execute(
        "SELECT path, datetime_original, date_confidence, file_type FROM files "
        "WHERE file_type != 'VIDEO' AND status != 'error' "
        "AND date_confidence IN ('HIGH','MEDIUM') AND datetime_original IS NOT NULL"
    ):
        dt = _parse_exif_dt(r["datetime_original"])
        if dt is None:
            continue
        folder = str(PureWindowsPath(r["path"]).parent)
        by_folder[folder][dt.date()] += 1
    hints: dict[str, datetime] = {}
    for folder, counter in by_folder.items():
        # most common date; tie-break to the earliest date
        best = min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        hints[folder] = datetime(best.year, best.month, best.day)
    return hints


def _effective_date_with_override(
    row: Any, overrides: dict | None, sibling_hints: dict | None = None
) -> tuple[datetime | None, bool]:
    """Like _effective_date, but a folder-level date_override replaces the date
    ONLY for files whose date_confidence is LOW or NULL (never HIGH/MEDIUM — real
    or corroborated EXIF is trusted). Returns (date, used_mtime); an applied
    override reports used_mtime=False (it's a manual date, not an mtime guess).

    When no override applies and the file is a VIDEO still at LOW/None
    confidence (i.e. no good date of its own — a video with a trustworthy
    filename date already became MEDIUM in `_resolve_date` and never reaches
    here), borrow the representative capture date of confidently-dated sibling
    photos in the same source folder (`sibling_hints`, see `_sibling_date_hints`).
    """
    dt, used_mtime = _effective_date(row)
    if not overrides and not sibling_hints:
        return dt, used_mtime
    conf = row["date_confidence"]
    if conf in ("LOW", None):
        parent = str(Path(row["path"]).parent)
        # 1. explicit user folder date override (any file type) — highest priority.
        if overrides:
            ov = overrides.get(parent)
            if ov is not None and ov["date_override"]:
                parsed = _parse_override_date(ov["date_override"])
                if parsed is not None:
                    return parsed, False
        # 2. videos with no good date of their own → borrow folder's photo date.
        if sibling_hints and row["file_type"] == "VIDEO":
            hint = sibling_hints.get(parent)
            if hint is not None:
                return hint, False
    return dt, used_mtime


def _sanitize_camera(model: str | None) -> str:
    """Return a filesystem-safe, truncated camera model string."""
    if not model:
        return "Unknown"
    s = re.sub(r"[^\w]", "_", model).strip("_")
    return s[:20] or "Unknown"


_LEADING_DATE = re.compile(
    r"^(?:"
    r"\d{8}(?:_\d{6})?"       # 20100815 / 20100815_143022
    r"|\d{4}_\d{4}"           # 2010_2012  (year range)
    r"|\d{4}(?:_\d{2}){1,2}"  # 2010_08 / 2010_08_15
    r"|\d{4}"                 # 2012  (standalone year)
    r")(?=_|$)"
)


def _sanitize_event(folder_name: str | None) -> str:
    """
    Filesystem-safe, truncated 'event & location' string derived from the
    source file's parent folder name.  Returns "" when unusable (empty or a
    drive root like 'E:\\'), so the caller can omit the segment entirely.

    The date is always added separately as the folder PREFIX
    ({YYYY-MM-DD}_{event}), so a leading date stamp inside the source folder
    name is redundant and is stripped — "20100815 倩家" → "倩家", "2012 Trip"
    → "Trip".  Separator runs (including literal underscores, since '_' is a
    word char) collapse to a single '_'.
    """
    if not folder_name:
        return ""
    # A drive-root parent (e.g. "E:\\") has no real name component.
    if re.fullmatch(r"[A-Za-z]:[\\/]?", folder_name):
        return ""
    s = re.sub(r"[\W_]+", "_", folder_name).strip("_")
    s = _LEADING_DATE.sub("", s).strip("_")
    return s[:40]


def _is_unorganised_folder_name(name: str | None) -> bool:
    """
    True when a source folder name carries NO human-meaningful event/location —
    i.e. it is just a date, a plain number/sequence, or a camera dump folder.
    These are almost certainly folders that were never manually organised.

    Examples flagged: "" (root), "2023", "2023-06", "2023-06-15", "20230615",
    "12345", "100CANON", "100MSDCF", "DCIM", "IMG_1234", "DSC01234", "P1010001".
    Examples NOT flagged: "Kyoto", "Japan_Trip", "Wedding 2023", "Grandma 90th".
    """
    n = (name or "").strip()
    if not n:
        return True
    if re.fullmatch(r"[A-Za-z]:[\\/]?", n):            # drive root, e.g. E:\
        return True
    if re.fullmatch(r"\d{4}([-_.]?\d{1,2}){0,2}", n):  # 2023 / 2023-06 / 2023-06-15
        return True
    if re.fullmatch(r"\d{8}([-_]?\d{6})?", n):         # 20230615 / 20230615_143022
        return True
    if re.fullmatch(r"\d+", n):                        # pure number / sequence
        return True
    low = n.lower()
    # 100CANON / 101NIKON / 100_FUJI, optionally with a .done/.old/.bak/_copy
    # archival suffix (100EOS5D.done) — the suffix marks a processed dump, so the
    # climb skips it and reaches the real event/subject above (小英照, 小蓉訂婚).
    if re.fullmatch(r"\d{3}[-_]?[a-z0-9]{2,6}(?:[._](?:done|old|bak|copy))?", low):
        return True
    if low == "dcim":
        return True
    # single camera-filename-style dump (IMG_1234, DSC01234, P1010001, GOPR0001)
    if re.fullmatch(r"(img|dsc|dscf|p|pic|photo|mvi|gopr)[-_]?\d{3,}", low):
        return True
    return False


# Container / device / temp folder names that carry NO event or subject meaning.
# Unlike `_is_unorganised_folder_name` (which catches dates/serials/camera dumps),
# these are real *words* that nonetheless must never become an event/subject label
# — they name a storage bucket, a device, or a scratch area, not a moment or a
# theme. Used by `_resolve_event_folder` so the climb skips past them instead of
# stopping and mislabelling everything "Sony_TX5" / "Raw_Files" / "手機 Sony DCIM".
_CONTAINER_NAMES: frozenset[str] = frozenset({
    # storage buckets
    "raw_files", "rawtank", "raw_old", "albums", "album", "photos", "照片",
    "jpg", "jpeg", "jpegtank", "depository_aged", "完整備份相簿",
    "slides", "pic", "pics", "samples", "icqreceived_old", "thmbnl", "thmbnls",
    "待整理",                              # "to be organised" — a TODO bucket
    "m4root",                              # Sony Memory Stick standard root dir
    # RAW-processing / derivative-export output dirs (the real event is ABOVE
    # them: …/20081101 小蓉訂婚/Develops/ → 小蓉訂婚).  Mirrors the resize/share
    # tokens used by `_derivative_event_root` for redundant-copy staging.
    "develop", "develops", "developed", "export", "exports", "share",
    # software/junk dirs seen in the wild (MS Office sample images, etc.)
    "station", "office11",
    # generic placeholder/scratch words — EXACT-match only (not a substring
    # rule), so a real name that merely contains one of these (Old_Believers_
    # Trip, backup_plan_for_japan) is unaffected; only a folder whose whole
    # normalised name equals one of these is a container.
    "old", "new", "misc", "backup", "bak", "copy", "untitled",
    "新增資料夾", "未命名", "暫存",
})

# Substrings / patterns marking a device dump, scratch area, or burn batch.
_DEVICE_SUBSTRINGS = ("手機", "ipad", "iphone")   # phone/tablet backup dumps
_CONTAINER_PATTERNS: list[re.Pattern] = [
    re.compile(r"^dcim"),                 # DCIM / DCIM_Storage / DCIM_Working
    re.compile(r"^temp(\d+|_\d+)?$"),     # Temp / Temp2011 / Temp_20100627 (scratch)
    re.compile(r"已燒錄"),                 # "_200602 已燒錄_4.35" (CD/DVD burn batch)
    re.compile(r"^\d{6}$"),               # 200609 — bare 6-digit burn/date label
    re.compile(r"^v\d+_"),                # "V50 小鐵" → v50_… (phone model + nick)
    re.compile(r"(?:_|^)[tf]x\d"),        # Sony_TX5 / Sweden_FX5 (camera models)
]


def _is_container_or_device(name: str | None) -> bool:
    """True when a folder name is a storage bucket / device / scratch area —
    a real word that must NOT be used as an event or subject label."""
    if not name:
        return True
    norm = re.sub(r"[\W_]+", "_", name).strip("_").lower()
    if not norm:
        return True
    if norm in _CONTAINER_NAMES:
        return True
    if any(s in norm for s in _DEVICE_SUBSTRINGS):
        return True
    return any(p.search(norm) for p in _CONTAINER_PATTERNS)


def _norm_path(p: Path | str) -> str:
    """Separator/case-normalised path string for boundary comparison (no I/O)."""
    return str(Path(p)).replace("/", "\\").rstrip("\\").lower()


def _resolve_event_folder(
    path: Path, scan_roots: list[Path] | None = None
) -> Path | None:
    """Climb from a file's parent folder up to the nearest ancestor that carries
    a real event/subject label, returning that folder (or None if none exists).

    The DATE always comes from EXIF (date forensics) — this resolves only the
    LABEL. Photos are often buried in camera-dump subfolders (151CANON) or
    date-divider subfolders (200909) under the meaningful folder (郁, 小英照,
    20050814 蒙古), so we skip past names that are unorganised (date/serial/dump)
    or a container/device/scratch bucket, and stop at the first real name.

    Returns the FIRST (lowest) qualifying ancestor, so a real event folder is
    never overshot into its storage parent (…/Albums/2013/墾丁凱撒/JPG/x.jpg
    resolves to 墾丁凱撒, not Albums).

    ``scan_roots`` bounds the climb: it never goes ABOVE a scan-input root. The
    root itself may be the event (you scanned an event folder directly), but once
    the only names up to the root are containers/dumps the file has no event →
    None (a no-event date folder), instead of mislabelling a top-level storage
    folder above the root as a subject. Default (None) = unbounded.
    """
    roots = {_norm_path(r) for r in (scan_roots or [])}
    for folder in path.parents:
        name = folder.name
        if not name:                              # reached a drive root
            return None
        if (not _is_unorganised_folder_name(name)  # date / serial / camera dump
                and not _is_container_or_device(name)  # bucket / device / scratch
                and _sanitize_event(name)):            # a usable label survives
            return folder
        if roots and _norm_path(folder) in roots:
            # Reached a scan-input root with no usable label → no event; do not
            # climb into storage folders above the root.
            return None
    return None


# Camera-original filenames: a prefix the camera assigns + a sequence number.
# The camera gives EACH shutter actuation its own number, so two DIFFERENT
# camera-original names can never be the same shot copied — they are distinct
# frames (a burst).  A renamed/exported copy loses this form (image00017.jpg),
# which is how Edge 2 tells a copy apart from a burst.
_CAMERA_ORIGINAL_NAME = re.compile(
    r"(?:img|_mg|dsc|dscf|dscn|pict?|photo|mvi|gopr|pano|cimg|hpim|sdc|dji)"
    r"[-_]?\d{3,}",
    re.IGNORECASE,
)


def _is_camera_original_name(filename: str | None) -> bool:
    """True when the filename looks like an unedited camera original
    (IMG_9606, DSC03639, P1010001) rather than a renamed export."""
    stem = Path(filename or "").stem.strip()
    return bool(_CAMERA_ORIGINAL_NAME.fullmatch(stem))


# Source folders whose photo dates span more than this many days are treated
# as "not a single outing" (e.g. a phone dump) and fall back to per-day folders.
MAX_EVENT_SPAN_DAYS = 30


def _compute_event_groups(
    db: Database, stage_ids: set[int], scan_roots: list[Path] | None = None,
    overrides: dict | None = None, sibling_hints: dict | None = None,
) -> dict[str, dict]:
    """
    Group kept photos by their RESOLVED event folder (`_resolve_event_folder`,
    which climbs past camera-dump / date-divider subfolders) and classify each
    group by its EXIF date span:

        kind="event"   2…MAX_EVENT_SPAN_DAYS days → one date-prefixed folder
                       {"start": date, "span": int}
        kind="subject" > MAX_EVENT_SPAN_DAYS days → a named collection, kept
                       together and subdivided by year ({label}/{YYYY}/)
                       {"label": str}

    Returned map is keyed by the resolved folder path string. Single-day groups
    are omitted (handled inline as {YYYY-MM-DD}_{event}). Files whose folder does
    not resolve to a real name (None) are omitted too — they fall back to the
    per-day / no-event path in `_build_target_path`.

    Why a long span splits the two: a named folder spanning months/years is not
    one outing but a recurring SUBJECT (a child, a pet, a place revisited). Per
    the user, those are organised by name, not exploded into hundreds of per-day
    folders — that scattering is exactly what the subject collection avoids.
    Only UN-named long-span folders (resolve → None) remain bulk dumps.

    Date source is `_effective_date` (EXIF, else filesystem mtime fallback), not
    bare EXIF — this is what lets VIDEO (often EXIF/QuickTime-metadata-less) and
    UNKNOWN-status photos enter grouping at all. A side effect: photos with no
    EXIF date that previously fell straight through to the no-date/per-day path
    now participate in event/subject grouping via mtime, same as everything else.
    """
    from collections import defaultdict as _dd

    dates_by_folder: dict[str, list] = _dd(list)
    label_by_folder: dict[str, str] = {}
    for batch in db.iter_files():
        for row in batch:
            if row["status"] == "error" or row["file_id"] in stage_ids:
                continue
            if row["file_type"] not in (
                "RAW", "CAMERA_JPEG", "DEV_JPEG", "HEIC", "VIDEO",
            ):
                continue  # UNKNOWN stays in place
            dt, _used_mtime = _effective_date_with_override(row, overrides, sibling_hints)
            if dt is None:
                continue
            folder = _resolve_event_folder(Path(row["path"]), scan_roots)
            if folder is None:
                continue  # no real label → per-day / no-event fallback
            key = str(folder)
            dates_by_folder[key].append(dt.date())
            label_by_folder.setdefault(key, _sanitize_event(folder.name))

    groups: dict[str, dict] = {}
    for key, dates in dates_by_folder.items():
        dmin, dmax = min(dates), max(dates)
        span = (dmax - dmin).days + 1
        label = label_by_folder[key]
        if span > MAX_EVENT_SPAN_DAYS:
            groups[key] = {"kind": "subject", "label": label}
            db.log(
                "INFO",
                f"Subject collection '{label}' spans {span} days "
                f"({dmin.isoformat()}…{dmax.isoformat()}) — organised as "
                f"{label}/{{year}}/ instead of per-day folders.",
                phase="review", path=key,
            )
        elif span > 1:
            groups[key] = {"kind": "event", "start": dmin, "span": span}
        # span == 1 → single-day event, handled inline with the resolved label
    return groups


def keep_score(row: Any, known_cameras: set[str]) -> tuple:
    """Rank duplicate copies; the highest tuple is the one to KEEP.

    Shared by exact-duplicate resolution (planner) and near-duplicate review
    (reviewer) so a single, consistent policy decides which copy survives —
    whether the relationship is recorded as EXACT or NEAR.

    Order (user-chosen):
      1. highest resolution            (best image wins; ties for exact copies)
      2. largest file size             (least re-compressed)
      3. known-camera / Masters folder (camera_model in known_cameras)
      4. real event-name parent folder (not a date/serial/camera-dump name)
      5. shortest path                 (main location over deep backup)
      6. lowest file_id                (deterministic final tiebreak)
    """
    area = (row["width"] or 0) * (row["height"] or 0)
    model = (row["camera_model"] or "").lower()
    in_known = 1 if (model and model in known_cameras) else 0
    parent = Path(row["path"]).parent.name
    has_event = 1 if (not _is_unorganised_folder_name(parent)
                      and _sanitize_event(parent)) else 0
    return (area, row["size_bytes"] or 0, in_known, has_event,
            -len(row["path"]), -row["file_id"])


# ---------------------------------------------------------------------------
# Union-Find: reconstruct exact-duplicate groups from pair rows
# ---------------------------------------------------------------------------

def _exact_dup_groups(db: Database) -> list[list[int]]:
    rows = db.conn.execute(
        "SELECT file_id_a, file_id_b FROM duplicates WHERE dup_type = 'EXACT'"
    ).fetchall()

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        # iterative path compression
        while parent.get(x, x) != root:
            nxt = parent.get(x, x)
            parent[x] = root
            x = nxt
        return root

    def union(a: int, b: int) -> None:
        pa, pb = find(a), find(b)
        if pa != pb:
            parent[pa] = pb

    for row in rows:
        for fid in (row["file_id_a"], row["file_id_b"]):
            parent.setdefault(fid, fid)
        union(row["file_id_a"], row["file_id_b"])

    groups: dict[int, list[int]] = defaultdict(list)
    for fid in parent:
        groups[find(fid)].append(fid)

    return [g for g in groups.values() if len(g) > 1]


def _pick_keeper(db: Database, group: list[int], known_cameras: set[str]) -> int:
    """Return the file_id to KEEP, per the shared keep_score policy."""
    ph = ",".join("?" * len(group))
    rows = db.conn.execute(
        f"SELECT file_id, path, width, height, size_bytes, camera_model "
        f"FROM files WHERE file_id IN ({ph})", group
    ).fetchall()
    return max(rows, key=lambda r: keep_score(r, known_cameras))["file_id"]


# Aspect-ratio equality tolerance — a pure downscale preserves the ratio; a crop
# or rotation changes it, so those are NOT treated as resize copies.
_RESIZE_ASPECT_TOL = 0.02

# A pHash shared by this many files is non-discriminative (low-information image:
# dark/flat) and collides across unrelated photos, so it must never drive a
# content-equality decision.  Shared with reviewer's near-cluster junk filter.
JUNK_PHASH_MIN_FILES = 8

# Folder-name tokens that mark a throwaway export / derivative copy.  A path
# component is a "derivative folder" when, split on non-alphanumerics, any of its
# tokens is in this set (so "resize+crop" matches via {resize, crop}).  "jpeg" is
# intentionally absent — in this library a "Jpeg" folder holds developed masters.
# Downscaled+cropped derivatives are unreliable for pHash/date matching (different
# shots can collide once shrunk), so the FOLDER is the trustworthy signal here.
_DERIVATIVE_FOLDER_TOKENS = frozenset({
    "resize", "resized", "crop", "cropped", "share", "shared", "export",
    "exports", "web", "websize", "small", "thumb", "thumbs", "thumbnail",
    "thumbnails", "preview", "previews", "proof", "proofs",
})

_FOLDER_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _is_derivative_folder(name: str) -> bool:
    return bool(set(_FOLDER_TOKEN_SPLIT.split(name.lower())) & _DERIVATIVE_FOLDER_TOKENS)


def _derivative_event_root(path: Path) -> str | None:
    """Lowercased ancestor dir just ABOVE the first derivative folder, or None.

    For `…/EVENT/share/IMG.jpg` returns `…/event`; a same-stem master kept under
    EVENT (e.g. `EVENT/Jpeg/IMG.JPG` or `EVENT/IMG.CR2`) lives under this root.
    """
    parts = path.parts
    for i in range(len(parts) - 1):  # directory components only (skip filename)
        if _is_derivative_folder(parts[i]):
            return str(Path(*parts[:i])).lower()
    return None


def redundant_copy_ids(db: Database) -> set[int]:
    """file_ids that are redundant copies of a shot whose better version survives.

    Copies of one shot are linked into a connected component, then the keep_score
    best of each component is KEPT and every other member is staged.  Two edges
    link "the same shot", both independently safe:

      • same (filename stem + EXIF datetime_original) AND matching aspect ratio —
        the same camera frame re-saved/downscaled (any size, even a tiny thumbnail
        whose pHash drifted).  Different aspect (a CROP or rotation) is NOT linked,
        so deliberately cropped versions are kept.
      • same (datetime_original + identical non-junk pHash) — the same image even
        if the file was RENAMED (e.g. an export named image00017.jpg).

    Plus a folder rule: a JPEG inside a derivative-export folder (share/resize/
    crop/…) is staged when a same-stem master exists in the same event.  Such
    downscaled+cropped exports defeat pHash (different shots collide once shrunk)
    and often have their EXIF date stripped, so the folder name is the reliable
    signal; the master (and RAW) elsewhere is what preserves the content.

    Because grouping is by shot — not by which copy is "best" — there is no
    keeper-vs-keeper conflict: each component keeps exactly one survivor, so a
    tiny thumbnail is always staged when a larger sibling exists.  A unique shot
    (singleton component) is never staged.  RAW/VIDEO are out of scope (only
    CAMERA_JPEG/DEV_JPEG/HEIC), so RAW masters are always kept.  Genuine bursts
    are NOT linked — two DIFFERENT camera-original filenames are distinct shutter
    actuations even when their pHash is identical (Hamming 0), so Edge 2 only
    links a non-camera-original export name to a shot; the burst pair remains for
    near-duplicate review.  DB-only — no disk read.
    """
    rows = db.conn.execute(
        "SELECT file_id, filename, path, width, height, size_bytes, "
        "camera_model, datetime_original, phash FROM files "
        "WHERE file_type IN ('CAMERA_JPEG','DEV_JPEG','HEIC') "
        "AND datetime_original IS NOT NULL "
        "AND width > 0 AND height > 0"
    ).fetchall()

    known = db.get_known_camera_models()
    junk = {
        r[0]
        for r in db.conn.execute(
            "SELECT phash FROM files WHERE phash IS NOT NULL "
            "GROUP BY phash HAVING COUNT(*) >= ?",
            (JUNK_PHASH_MIN_FILES,),
        )
    }

    meta = {r["file_id"]: r for r in rows}
    parent: dict[int, int] = {r["file_id"]: r["file_id"] for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Edge 1 — same (stem, datetime), pairwise within matching aspect ratio.
    by_shot: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        stem = Path(r["filename"]).stem.strip().lower()
        by_shot[(stem, r["datetime_original"])].append(r)
    for members in by_shot.values():
        for i in range(len(members)):
            ai = members[i]["width"] / members[i]["height"]
            for j in range(i + 1, len(members)):
                aj = members[j]["width"] / members[j]["height"]
                if abs(ai - aj) <= _RESIZE_ASPECT_TOL:
                    union(members[i]["file_id"], members[j]["file_id"])

    # Edge 2 — same (datetime, identical non-junk pHash): a renamed/re-encoded
    # COPY of one shot.  But two DIFFERENT camera-original filenames at the same
    # instant are distinct shutter actuations (a burst), never one shot copied —
    # so a copy is only ever a NON-camera-original name (image00017.jpg).  Link
    # such exports to the best camera original (or, if the group is all exports,
    # to each other); never link camera-original ↔ camera-original, leaving real
    # bursts for near-duplicate review.
    by_content: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        if r["phash"] and r["phash"] not in junk:
            by_content[(r["datetime_original"], r["phash"])].append(r)
    for members in by_content.values():
        if len(members) < 2:
            continue
        cameras = [m for m in members if _is_camera_original_name(m["filename"])]
        exports = [m for m in members if not _is_camera_original_name(m["filename"])]
        if cameras:
            anchor = max(cameras, key=lambda m: keep_score(m, known))["file_id"]
            for e in exports:
                union(anchor, e["file_id"])
        else:
            for m in members[1:]:
                union(members[0]["file_id"], m["file_id"])

    components: dict[int, list[int]] = defaultdict(list)
    for fid in parent:
        components[find(fid)].append(fid)

    losers: set[int] = set()
    for members in components.values():
        if len(members) < 2:
            continue
        keeper = max(members, key=lambda f: keep_score(meta[f], known))
        losers.update(f for f in members if f != keeper)

    # Folder rule — derivative-export copies with a same-stem master in-event.
    # Index every NON-derivative file's parent dir by stem (RAW counts as a
    # master), then stage any derivative-folder JPEG whose event root contains a
    # same-stem master.  Independent of pHash/date, so it catches stripped-date,
    # cropped, and cross-shot-colliding exports the edges above can't place.
    masters_by_stem: dict[str, list[str]] = defaultdict(list)
    for r in db.conn.execute("SELECT filename, path FROM files"):
        p = Path(r["path"])
        if _derivative_event_root(p) is None:  # a master lives outside such folders
            stem = Path(r["filename"]).stem.strip().lower()
            masters_by_stem[stem].append(str(p.parent).lower())
    # Candidate query is separate from `rows`: derivative exports often have their
    # EXIF date stripped (so they fail the datetime filter), but the folder rule
    # doesn't need a date.
    for r in db.conn.execute(
        "SELECT file_id, filename, path FROM files "
        "WHERE file_type IN ('CAMERA_JPEG','DEV_JPEG','HEIC') "
        "AND width > 0 AND height > 0"
    ):
        root = _derivative_event_root(Path(r["path"]))
        if root is None:
            continue
        stem = Path(r["filename"]).stem.strip().lower()
        if any(d.startswith(root) for d in masters_by_stem.get(stem, ())):
            losers.add(r["file_id"])

    return losers


def _folder_merge_loser_ids(db: Database) -> tuple[set[int], int]:
    """Return (loser_file_ids_to_stage, unique_to_loser_count).

    For each reviewed folder_overlap with a keeper:
    - Files in the loser subtree whose SHA-256 exists in the keeper subtree
      → returned in loser_ids (to be STAGE_DELETE'd by plan).
    - Files unique to the loser (no SHA match, or sha=NULL)
      → counted in unique_count; NOT staged; move through normal pipeline.
    - Files with status='done' or status='error' → skipped entirely.
    - keeper=NULL ('both') → no staging for that pair (filtered by SQL).

    DB-only, no disk reads, idempotent.

    Performance: a single pass over `files` assigns each file to every keeper/
    loser folder whose subtree contains it (walking the path's ancestor chain).
    The earlier design ran two `path LIKE 'folder\\%'` scans per pair — and
    SQLite's LIKE never uses an index — so on a 243k-file library with 460
    reviewed pairs it cost ~320 s (920 full-table scans). The single-pass
    ancestor walk runs in ~3 s (a measured ~98x speedup, byte-identical result)
    and matches the LIKE subtree semantics exactly.
    """
    reviewed = db.conn.execute(
        "SELECT folder_a, folder_b, keeper "
        "FROM folder_overlaps "
        "WHERE status='reviewed' AND keeper IN ('a','b')"
    ).fetchall()

    if not reviewed:
        return set(), 0

    # Resolve each pair to (keeper_folder, loser_folder) and collect the folder
    # names we care about. A folder may be a keeper in one pair and a loser in
    # another, so the two sets are independent.
    pairs: list[tuple[str, str]] = []
    keeper_folders: set[str] = set()
    loser_folders: set[str] = set()
    for row in reviewed:
        if row["keeper"] == "a":
            kf, lf = row["folder_a"], row["folder_b"]
        else:
            kf, lf = row["folder_b"], row["folder_a"]
        pairs.append((kf, lf))
        keeper_folders.add(kf)
        loser_folders.add(lf)

    # Single pass: bucket each non-error file under every keeper/loser ancestor
    # folder. folder_overlaps rows are rolled-up ancestors, so files live in
    # subfolders — walking the parent chain reproduces `path LIKE 'folder\%'`
    # (each ancestor that exactly equals a target folder matches; sibling
    # prefixes like D:\B vs D:\B2 never collide because the chain only yields
    # whole path components).
    keeper_shas_by_folder: dict[str, set[str]] = defaultdict(set)
    loser_files_by_folder: dict[str, list[tuple[int, str | None]]] = defaultdict(list)

    for r in db.conn.execute("SELECT file_id, path, sha256, status FROM files"):
        status = r["status"]
        if status == "error":
            continue
        fid, sha = r["file_id"], r["sha256"]
        cur = str(PureWindowsPath(r["path"]).parent)
        while cur:
            if cur in keeper_folders and sha is not None:
                keeper_shas_by_folder[cur].add(sha)
            if cur in loser_folders and status != "done":
                loser_files_by_folder[cur].append((fid, sha))
            idx = cur.rfind("\\")
            if idx < 0:
                break
            cur = cur[:idx]

    loser_ids: set[int] = set()
    unique_count = 0
    for kf, lf in pairs:
        keeper_shas = keeper_shas_by_folder.get(kf, set())
        for fid, sha in loser_files_by_folder.get(lf, []):
            if sha is None or sha not in keeper_shas:
                unique_count += 1
            else:
                loser_ids.add(fid)

    return loser_ids, unique_count


# ---------------------------------------------------------------------------
# Target path builder
# ---------------------------------------------------------------------------

def _build_target_path(
    row: Any,
    target_root: Path,
    known_cameras: set[str],
    counters: dict[tuple, int],
    event_groups: dict[str, dict] | None = None,
    scan_roots: list[Path] | None = None,
    overrides: dict | None = None,
    sibling_hints: dict | None = None,
) -> Path:
    """
    Compute the reorganised destination path for a file to keep.

    The {event} label is the RESOLVED event folder name (`_resolve_event_folder`
    climbs past camera-dump / date-divider subfolders), so a photo buried in
    …/20050814 蒙古/0808/IMG.jpg is labelled "蒙古", not "0808".

    Photos (ORIGINAL filename kept):
      Single-day event:
        Masters/{YYYY}/{YYYY-MM-DD}_{event}/{original_name}
      Multi-day event (2…MAX_EVENT_SPAN_DAYS days, from event_groups):
        Masters/{YYYY}/{start-date}_{N}d_{event}/{original_name}
        — ALL of the event's files land in this one folder (year = start year).
      Subject collection (named folder spanning > MAX_EVENT_SPAN_DAYS days):
        Masters/{event}/{YYYY}/{original_name}
        — kept together under the name, subdivided by year (NOT per-day).
      Others/  — camera not in known_cameras list
      NoDate/  — no EXIF datetime
      The {event} segment is dropped when the folder does not resolve to a real
      name, leaving just {YYYY-MM-DD}/ (or {start-date}_{N}d/).  RAW+JPEG pairs
      keep the same stem naturally because original camera filenames are kept.
    Videos (own layout — date only, parent folder as event/location):
      Videos/{YYYY}/{YYYY-MM-DD}_{event}_{seq:04d}.EXT
      Videos/NoDate/{event}_{seq:04d}.EXT  — no EXIF datetime
      The {event} segment is dropped when the parent folder name is unusable.
      Subject collection (resolved folder spans > MAX_EVENT_SPAN_DAYS days):
        Videos/{label}/{YYYY}/{YYYY-MM-DD}_{seq:04d}.EXT
        Videos/{label}/NoDate/video_{seq:04d}.EXT  — this file itself has no date
        — same rationale as the photo branch: a recurring theme, not one outing.

    Filename collisions inside a folder are resolved by the executor
    (it appends _conflict_N when a destination already exists).
    """
    ext = (row["extension"] or "jpg").upper()
    dt, _ = _effective_date_with_override(row, overrides, sibling_hints)  # EXIF date, else mtime/override/sibling fallback

    # Folder-level event-name override: non-empty event_name on the file's
    # immediate source parent folder wins over the auto-resolved label.
    _ov = (overrides or {}).get(str(Path(row["path"]).parent))
    _ov_event = _sanitize_event(_ov["event_name"]) if (_ov and _ov["event_name"]) else ""

    # ── Videos: dedicated tree, date-only, event = source parent folder ──────
    if row["file_type"] == "VIDEO":
        resolved = _resolve_event_folder(Path(row["path"]), scan_roots)
        group = (event_groups or {}).get(str(resolved)) if resolved else None

        if group and group["kind"] == "subject":
            # Mirrors the photo subject branch: a named folder spanning months/
            # years is a recurring theme (child, pet, revisited place), not one
            # outing — keep videos together under the label instead of scattering
            # them into bare Videos/{YYYY}/.
            label = _ov_event or group["label"] or _sanitize_event(Path(row["path"]).parent.name)
            if dt is None:
                subdir = target_root / "Videos" / label / "NoDate"
                stem = "video"
            else:
                subdir = target_root / "Videos" / label / dt.strftime("%Y")
                stem = dt.strftime("%Y-%m-%d")
            key = (str(subdir), stem)
            seq = counters.get(key, 0)
            counters[key] = seq + 1
            return subdir / f"{stem}_{seq:04d}.{ext}"

        # ── original logic, unchanged: event / no-group / unresolved ─────────
        event = _ov_event or _sanitize_event(Path(row["path"]).parent.name)
        if dt is None:
            subdir = target_root / "Videos" / "NoDate"
            date_part = ""
        else:
            subdir = target_root / "Videos" / dt.strftime("%Y")
            date_part = dt.strftime("%Y-%m-%d")
        parts = [p for p in (date_part, event) if p]
        stem = "_".join(parts) if parts else "video"

        key = (str(subdir), stem)
        seq = counters.get(key, 0)
        counters[key] = seq + 1
        return subdir / f"{stem}_{seq:04d}.{ext}"

    # ── Photos: event / subject folder, original filename preserved ──────────
    # The label comes from the RESOLVED event folder (climbs past camera-dump /
    # date-divider subfolders); falls back to "" (no label) when unresolved.
    resolved = _resolve_event_folder(Path(row["path"]), scan_roots)
    event = _ov_event or (_sanitize_event(resolved.name) if resolved else "")
    group = (event_groups or {}).get(str(resolved)) if resolved else None

    if dt is None:
        subdir = target_root / "NoDate"
    else:
        # known_cameras contains lowercase strings; compare case-insensitively
        model_lower = (row["camera_model"] or "").lower()
        in_known = bool(model_lower and model_lower in known_cameras)
        base = target_root / ("Masters" if in_known else "Others")

        if group and group["kind"] == "subject":
            # Long-span named collection: keep together, subdivide by year only.
            # Event-name override wins over the auto-derived subject label too.
            folder = _ov_event or group["label"] or event
            subdir = base / folder / dt.strftime("%Y")
        elif group and group["kind"] == "event":
            # Multi-day event: whole event collapses into one start-year folder.
            start = group["start"]
            stamp = f"{start.isoformat()}_{group['span']}d"
            folder = f"{stamp}_{event}" if event else stamp
            subdir = base / start.strftime("%Y") / folder
        else:
            # Single-day event (or unresolved): per-day folder with the label.
            day = dt.strftime("%Y-%m-%d")
            folder = f"{day}_{event}" if event else day
            subdir = base / dt.strftime("%Y") / folder

    return subdir / row["filename"]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _candidate_str(row: Any) -> str:
    """Render the competing date signals for a file as a compact one-liner,
    e.g. 'EXIF=2023-06-15 filename=2015-04-02 mtime=2024-01-02'. Only signals
    that parse to a date are shown."""
    parts: list[str] = []
    pairs = [
        ("EXIF", _parse_exif_dt(row["datetime_original"])),
        ("filename", _parse_filename_dt(row["filename"])),
        ("digitized", _parse_exif_dt(row["datetime_digitized"])),
        ("mtime", _parse_mtime(row["mtime"])),
    ]
    for name, dt in pairs:
        if dt is not None:
            parts.append(f"{name}={dt.date().isoformat()}")
    return " ".join(parts) if parts else "no candidates"


_DATE_AUDIT_FILE_TYPES = ("RAW", "CAMERA_JPEG", "DEV_JPEG", "HEIC", "VIDEO")


def audit_dates(db: Database, today: date | None = None) -> dict[str, int]:
    """
    DB-only date forensics pass (no disk read, no ExifTool — like `reclassify`).

    For every dated file type, resolve a date via the confidence ladder, persist
    `date_source` / `date_confidence` on the row, and log every LOW-confidence
    date to run_log (phase='review', prefix 'Suspicious-date') so the user can
    review mis-dated files BEFORE they are filed into the wrong year by `plan`.

    Idempotent: clears its own prior run_log entries first, so re-running never
    duplicates them. Returns a summary count dict.
    """
    # Idempotency: drop prior suspicious-date entries before re-logging.
    db.conn.execute(
        "DELETE FROM run_log WHERE phase='review' AND message LIKE 'Suspicious-date%'"
    )
    db.commit()

    counts = {"total": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "none": 0}
    low = 0
    commit_buf = 0

    for batch in db.iter_files():
        for row in batch:
            if row["status"] == "error" or row["file_type"] not in _DATE_AUDIT_FILE_TYPES:
                continue
            dt, source, conf = _resolve_date(row, today=today)
            counts["total"] += 1
            counts[conf if conf else "none"] += 1

            db.update_file(row["file_id"], date_source=source, date_confidence=conf)
            commit_buf += 1

            # Surface LOW-confidence dates that still resolved to *a* date — a
            # None date is plain NoDate, not a mis-dating risk.
            if conf == "LOW" and dt is not None:
                low += 1
                db.log(
                    "WARN",
                    f"Suspicious-date [LOW] {row['path']}: "
                    f"{_candidate_str(row)} → chose {source} ({dt.date().isoformat()})",
                    phase="review", path=row["path"], file_id=row["file_id"],
                )

            if commit_buf >= 1000:
                db.commit()
                commit_buf = 0

    if low:
        db.log(
            "WARN",
            f"Suspicious-date summary: {low} file(s) have a LOW-confidence date "
            "(faked/stripped EXIF or copy-reset mtime). Review before execute:\n"
            "  SELECT path, message FROM run_log "
            "WHERE phase='review' AND message LIKE 'Suspicious-date%';",
            phase="review",
        )
    db.commit()

    return {"total": counts["total"], "low": low,
            "high": counts["HIGH"], "medium": counts["MEDIUM"]}


def plan(db: Database, target_root: Path, force: bool = False,
         assume_yes: bool = False, scan_roots: list[Path] | None = None) -> None:
    """
    Phase 4: build the operations table and prompt the user to confirm.

    Parameters
    ----------
    target_root:
        Root of the reorganised library.  Must be on the same volume as the
        source photos so that Phase 5 can use os.rename() (no data copy).
    assume_yes:
        Skip the interactive confirmation prompt and auto-confirm the plan.
    """
    print_phase_header("4/5", "Action Plan")

    if not force and db.phase_complete("review"):
        print_success("Phase 4 already complete. Use --force to redo.")
        return

    if force:
        db.conn.execute(
            "DELETE FROM operations WHERE status IN ('planned', 'confirmed')"
        )
        db.commit()

    db.set_phase_status("review", "running")

    # ── 0. Date forensics: rate every file's date + flag suspicious ones ──────
    # Populates date_source / date_confidence and logs LOW-confidence dates to
    # run_log so faked/stripped-EXIF files are surfaced before they are filed.
    with console.status("Auditing file dates (confidence + source)…"):
        date_audit = audit_dates(db)

    # Per-folder event-name / date overrides (O2): user-set corrections keyed by
    # a file's immediate source parent folder. Loaded once and threaded through
    # event grouping and target-path building.
    folder_overrides = db.get_folder_overrides()

    # Per-folder representative photo date (V2): lets a date-less video borrow
    # the capture date of confidently-dated sibling photos in its own source
    # folder, instead of falling to an unreliable mtime.
    sibling_hints = _sibling_date_hints(db)

    # ── 1. Identify files to stage-delete ────────────────────────────────────
    stage_ids: set[int] = set()

    # 1a. All RESIZED_JPEGs
    for batch in db.iter_files(file_type="RESIZED_JPEG"):
        for row in batch:
            stage_ids.add(row["file_id"])

    # Files staged because their CONTENT is preserved by a larger original (a
    # different sha256), not by a byte-identical twin.  The 1d safety net (which
    # guarantees each sha256 group keeps one copy) must EXEMPT these, or it would
    # "rescue" a unique-sha resized copy and defeat the staging.
    content_safe_stage: set[int] = set(stage_ids)  # RESIZED_JPEGs from 1a

    # 1b. Exact duplicate non-keepers
    # Heavily-mirrored libraries can have 100k+ exact groups, so this must stay
    # O(N): bulk-load metadata once and pick keepers in memory. We do NOT write
    # duplicates.keep_file_id for EXACT — it is never read back (staging is driven
    # by stage_ids), and the old per-group UPDATE scanned the whole duplicates
    # table once per group (O(groups × rows) → hours on big mirrors).
    known_cameras = db.get_known_camera_models()
    with console.status("Resolving exact duplicate groups…"):
        groups = _exact_dup_groups(db)
        dup_keepers: set[int] = set()
        dup_non_keepers: set[int] = set()

        member_ids = [fid for g in groups for fid in g]
        meta: dict[int, Any] = {}
        for i in range(0, len(member_ids), 900):
            chunk = member_ids[i:i + 900]
            ph = ",".join("?" * len(chunk))
            for r in db.conn.execute(
                f"SELECT file_id, path, width, height, size_bytes, camera_model "
                f"FROM files WHERE file_id IN ({ph})", chunk
            ):
                meta[r["file_id"]] = r

        for group in groups:
            keeper = max(group, key=lambda f: keep_score(meta[f], known_cameras))
            dup_keepers.add(keeper)
            for fid in group:
                if fid != keeper:
                    dup_non_keepers.add(fid)
                    stage_ids.add(fid)

    # 1c. Near-duplicate losers the user marked for deletion in `review`.
    # The reviewer only records the decision (duplicates.status='reviewed' +
    # keep_file_id); we derive the loser here and stage it.  Doing it in plan
    # (rather than in the reviewer) means a `plan --force` rebuild cannot wipe
    # the decision, and the loser never also receives a MOVE op.
    near_dup_losers: set[int] = set()
    for r in db.conn.execute(
        "SELECT file_id_a, file_id_b, keep_file_id FROM duplicates "
        "WHERE dup_type = 'NEAR' AND status = 'reviewed' AND keep_file_id IS NOT NULL"
    ).fetchall():
        loser = r["file_id_a"] if r["keep_file_id"] == r["file_id_b"] else r["file_id_b"]
        near_dup_losers.add(loser)
        stage_ids.add(loser)

    # 1c-bis. Redundant copies — re-encodes and downscales of a shot whose better
    # version survives (different sha256, so EXACT dedup never catches them).
    # keeper = best/largest; the redundant copies' content is preserved by that
    # survivor, so they are content-safe (exempt from the 1d byte-survival net).
    with console.status("Finding redundant copies (re-encodes + resizes)…"):
        redundant_copy_losers = redundant_copy_ids(db)
    stage_ids.update(redundant_copy_losers)
    content_safe_stage.update(redundant_copy_losers)

    # 1d. Folder-merge loser staging — files in reviewed "loser" folders whose
    # SHA-256 is confirmed present in the "keeper" folder subtree.
    with console.status("Staging folder-merge losers…"):
        folder_merge_losers, fm_unique_count = _folder_merge_loser_ids(db)
    stage_ids.update(folder_merge_losers)
    # NOT added to content_safe_stage: keeper has the same SHA, so the 1e
    # safety net correctly protects the keeper copy without exemption.

    # 1e. Safety net — never stage EVERY byte-identical copy of a file.
    # EXACT and NEAR resolution choose keepers independently, so their staging
    # sets can overlap in a way that stages all copies of one sha256 group
    # (e.g. a NEAR loser that is also another group's only EXACT survivor).
    # Guarantee each sha256 group retains its best copy; rescue + log any group
    # found fully staged.  This also protects every NEAR keeper's content,
    # because a staged keeper always has a byte-identical twin kept here.
    sha_groups: dict[str, list[int]] = defaultdict(list)
    for r in db.conn.execute(
        "SELECT file_id, sha256 FROM files WHERE sha256 IS NOT NULL"
    ).fetchall():
        sha_groups[r["sha256"]].append(r["file_id"])
    rescued = 0
    for fids in sha_groups.values():
        # Only files whose survival matters BYTE-wise count here; a content-safe
        # copy (resized/RESIZED) is preserved by a larger original elsewhere, so
        # a group made entirely of those may be fully staged without rescue.
        relevant = [f for f in fids if f not in content_safe_stage]
        if relevant and all(f in stage_ids for f in relevant):
            keeper = _pick_keeper(db, relevant, known_cameras)
            stage_ids.discard(keeper)
            near_dup_losers.discard(keeper)
            dup_non_keepers.discard(keeper)
            folder_merge_losers.discard(keeper)
            rescued += 1
    if rescued:
        db.log(
            "WARN",
            f"{rescued} sha256 group(s) were fully staged by overlapping "
            "EXACT/NEAR decisions; kept the best copy of each to prevent loss.",
            phase="review",
        )
        db.commit()

    # ── 2. Near-duplicate stats (info only — never auto-staged) ──────────────
    near_pairs: int = db.conn.execute(
        "SELECT COUNT(*) FROM duplicates WHERE dup_type = 'NEAR' AND status = 'pending'"
    ).fetchone()[0]

    near_file_ids: set[int] = set()
    for row in db.conn.execute(
        "SELECT file_id_a, file_id_b FROM duplicates "
        "WHERE dup_type = 'NEAR' AND status = 'pending'"
    ).fetchall():
        near_file_ids.update([row["file_id_a"], row["file_id_b"]])

    # ── 3. Build all operations in one pass ───────────────────────────────────
    staging_root = target_root / "_staging" / "to_delete"
    counters: dict[tuple, int] = {}
    now = _now()
    ops: list[dict] = []
    mtime_fallback = 0   # files dated from filesystem mtime (no EXIF date)

    # Pre-measure event/subject groups so each resolved folder maps to one
    # destination folder (multi-day event, or year-subdivided subject collection).
    with console.status("Measuring event date spans…"):
        event_groups = _compute_event_groups(
            db, stage_ids, scan_roots, overrides=folder_overrides,
            sibling_hints=sibling_hints,
        )
        db.commit()

    for batch in db.iter_files():
        for row in batch:
            fid = row["file_id"]
            if row["status"] == "error":
                continue

            if fid in stage_ids:
                # Include file_id in staging name to avoid collisions
                target = staging_root / f"{fid}_{row['filename']}"
                ops.append({
                    "file_id": fid,
                    "op_type": "STAGE_DELETE",
                    "source_path": row["path"],
                    "target_path": str(target),
                    "status": "planned",
                    "planned_at": now,
                })
            else:
                if row["file_type"] == "UNKNOWN":
                    continue  # leave truly-unknown files in place
                _dt, used_mtime = _effective_date(row)
                if used_mtime and _dt is not None:
                    mtime_fallback += 1
                target = _build_target_path(
                    row, target_root, known_cameras, counters, event_groups,
                    scan_roots, overrides=folder_overrides,
                    sibling_hints=sibling_hints,
                )
                ops.append({
                    "file_id": fid,
                    "op_type": "MOVE",
                    "source_path": row["path"],
                    "target_path": str(target),
                    "status": "planned",
                    "planned_at": now,
                })

    with console.status(f"Writing {len(ops):,} operations to database…"):
        db.conn.executemany(
            """
            INSERT OR IGNORE INTO operations
                (file_id, op_type, source_path, target_path, status, planned_at)
            VALUES (:file_id, :op_type, :source_path, :target_path, :status, :planned_at)
            """,
            ops,
        )
        db.commit()

    # ── 3b. No-event source folders — likely never organised ─────────────────
    # A source folder whose name is empty/a date/a plain number/a camera dump
    # (e.g. "2023-06-15", "20230615", "100CANON", "DCIM") carries no real
    # event/location. Group these by SOURCE folder so the user can revisit them.
    no_event_src: dict[str, int] = {}
    for op in ops:
        if op["op_type"] != "MOVE":
            continue
        parent = Path(op["source_path"]).parent
        if _is_unorganised_folder_name(parent.name) or not _sanitize_event(parent.name):
            no_event_src[str(parent)] = no_event_src.get(str(parent), 0) + 1

    if no_event_src:
        for folder, n in sorted(no_event_src.items()):
            db.log(
                "INFO",
                f"No-event source folder (date/serial name): {folder} ({n} files)",
                phase="review", path=folder,
            )
        db.commit()

    # ── 3c. Files dated from filesystem mtime (no EXIF date) ──────────────────
    if mtime_fallback:
        db.log(
            "WARN",
            f"{mtime_fallback} files had no EXIF date — dated from filesystem "
            "mtime instead (may be inaccurate if files were copied).",
            phase="review",
        )
        db.commit()

    # ── 4. Summary stats ─────────────────────────────────────────────────────
    resized_n: int = db.conn.execute(
        "SELECT COUNT(*) FROM files WHERE file_type = 'RESIZED_JPEG'"
    ).fetchone()[0]
    move_n: int = db.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE op_type = 'MOVE' AND status = 'planned'"
    ).fetchone()[0]
    stage_n: int = db.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE op_type = 'STAGE_DELETE' AND status = 'planned'"
    ).fetchone()[0]
    stage_bytes: int = db.conn.execute(
        """
        SELECT COALESCE(SUM(f.size_bytes), 0)
        FROM operations o JOIN files f USING(file_id)
        WHERE o.op_type = 'STAGE_DELETE' AND o.status = 'planned'
        """
    ).fetchone()[0]

    # ── 5. Print preview ──────────────────────────────────────────────────────
    t = Table(title="ACTION PLAN (preview)", box=box.DOUBLE_EDGE, show_header=True)
    t.add_column("Action", style="cyan", min_width=46)
    t.add_column("Files", justify="right")
    t.add_column("Notes", style="dim")

    t.add_row(
        "[red]Stage for deletion — Resized JPEGs[/red]",
        f"{resized_n:,}",
        "path contains 'resized'",
    )
    if redundant_copy_losers:
        t.add_row(
            "[red]Stage for deletion — Redundant copies (re-encodes + resizes)[/red]",
            f"{len(redundant_copy_losers):,}",
            "same shot, lower quality; best copy kept",
        )
    t.add_row(
        "[red]Stage for deletion — Exact duplicates[/red]",
        f"{len(dup_non_keepers):,}",
        "SHA-256 match, inferior path kept",
    )
    if near_dup_losers:
        t.add_row(
            "[red]Stage for deletion — Near-dupes (reviewed)[/red]",
            f"{len(near_dup_losers):,}",
            "your keep/discard choices from 'review'",
        )
    if folder_merge_losers:
        t.add_row(
            "[red]Stage for deletion — Folder-merge losers[/red]",
            f"{len(folder_merge_losers):,}",
            "redundant copies in folded-in folder; keeper has all SHA-256s",
        )
    if fm_unique_count:
        t.add_row(
            "[yellow]Unique files in loser folder(s)[/yellow]",
            f"{fm_unique_count:,}",
            "no SHA match in keeper — moved to library normally",
        )
    t.add_row(
        "[green]Move + rename → Masters/ or Others/[/green]",
        f"{move_n:,}",
        "renamed by date/camera",
    )
    if mtime_fallback:
        t.add_row(
            "[dim]  ↳ of which dated by file mtime[/dim]",
            f"{mtime_fallback:,}",
            "no EXIF date — mtime fallback",
        )
    if near_pairs:
        t.add_row(
            "[yellow]Near-duplicates (needs review)[/yellow]",
            f"{len(near_file_ids):,} files",
            f"{near_pairs:,} pairs — NOT auto-staged",
        )

    console.print()
    console.print(t)

    gb = stage_bytes / 1_073_741_824
    console.print(
        f"\n  Space to reclaim: [bold red]~{gb:.1f} GB[/bold red]  "
        f"(moved to _staging/to_delete/, [italic]not[/italic] permanently deleted)\n"
    )
    console.print(f"  Target root: [dim]{target_root}[/dim]\n")

    # ── No-event folders: surface for later manual organising ────────────────
    if no_event_src:
        total_ne = sum(no_event_src.values())
        ne_table = Table(
            title=f"⚠  No-event source folders (date / serial name) — {len(no_event_src):,} folders, {total_ne:,} files",
            box=box.SIMPLE, show_header=True,
        )
        ne_table.add_column("Source folder (likely never organised)", style="yellow")
        ne_table.add_column("Files", justify="right")
        for folder, n in sorted(no_event_src.items(), key=lambda kv: -kv[1])[:30]:
            ne_table.add_row(folder, f"{n:,}")
        if len(no_event_src) > 30:
            ne_table.add_row(f"[dim]… +{len(no_event_src) - 30} more[/dim]", "")
        console.print(ne_table)

        # Write the COMPLETE list to a collapsible, sortable HTML report.
        report_path = db.path.parent / "_staging" / "no_event_folders.html"
        try:
            write_no_event_report(no_event_src, report_path)
            console.print(
                f"  Full interactive list (expand/collapse + sort): "
                f"[cyan]{report_path}[/cyan]"
            )
        except OSError as e:
            db.log("WARN", f"Could not write no-event HTML report: {e}",
                   phase="review")

        console.print(
            "  Their names are just a date / number / camera dump (e.g. 2023-06-15, 100CANON, DCIM),\n"
            "  so the target folder has no event/location label.\n"
            "  Full list also recorded in run_log — review later with:\n"
            "    [cyan]SELECT path, message FROM run_log "
            "WHERE phase='review' AND message LIKE 'No-event%';[/cyan]\n"
        )

    if date_audit["low"]:
        console.print(
            f"  [yellow]⚠[/yellow]  {date_audit['low']:,} file(s) have a "
            "[bold]LOW-confidence date[/bold] (faked/stripped EXIF or copy-reset mtime)\n"
            "     — they may be filed into the wrong year. Review before execute with:\n"
            "    [cyan]SELECT path, message FROM run_log "
            "WHERE phase='review' AND message LIKE 'Suspicious-date%';[/cyan]\n"
        )

    if near_pairs:
        console.print(
            f"  [yellow]⚠[/yellow]  {near_pairs:,} near-duplicate pairs require human review before deletion.\n"
            "     Run: [cyan]python -m photo_organizer review --db <path>[/cyan]\n"
        )

    # ── 6. Confirm ───────────────────────────────────────────────────────────
    if not assume_yes:
        console.print(
            "  [dim]Above is only a PREVIEW — no files have been touched yet.[/dim]\n"
            "  [bold]y[/bold] = approve this plan (marks the operations as "
            "confirmed). Nothing moves until you then run "
            "[cyan]execute[/cyan].\n"
            "  [bold]N[/bold] = discard this plan and change nothing.\n"
        )
        try:
            ans = input(
                "Approve this plan?  (move/stage files later via 'execute')  [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != "y":
            console.print("\n[yellow]Cancelled — no operations confirmed.[/yellow]")
            db.conn.execute("DELETE FROM operations WHERE status = 'planned'")
            db.commit()
            db.set_phase_status("review", "pending")
            return

    db.conn.execute(
        "UPDATE operations SET status = 'confirmed' WHERE status = 'planned'"
    )
    db.conn.execute(
        "UPDATE files SET status = 'confirmed' "
        "WHERE file_id IN (SELECT file_id FROM operations WHERE status = 'confirmed')"
    )
    db.commit()

    confirmed_n: int = db.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE status = 'confirmed'"
    ).fetchone()[0]

    db.set_phase_status("review", "complete", {
        "target_root": str(target_root),
        "stage_delete": stage_n,
        "move": move_n,
        "near_dupe_pairs": near_pairs,
        "space_bytes": stage_bytes,
    })

    print_success(f"Plan confirmed — {confirmed_n:,} operations ready for Phase 5.")
    console.print(
        "  Run: [cyan]python -m photo_organizer execute --db <path>[/cyan]"
    )
