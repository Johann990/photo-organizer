"""
exiftool.py — ExifTool subprocess wrapper (batch JSON mode).

Key design decisions:
- Batch ~200 files per subprocess call to amortise startup overhead
- Use -fast2 to read only the EXIF header (10–100x faster than full scan)
- Use -json for structured output
- Never import this module's internals; use ExifToolBatch as a context manager
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BATCH_SIZE = 200

# Fields we actually need — limiting reduces parse time slightly
WANTED_TAGS = [
    # Timestamps
    "-DateTimeOriginal",
    "-CreationDate",      # QuickTime/Apple video — includes local TZ offset
    "-CreateDate",
    "-MediaCreateDate",   # QuickTime video fallback
    # Camera
    "-Make",
    "-Model",
    "-LensModel",
    # Dimensions
    "-ImageWidth",
    "-ImageHeight",
    "-ExifImageWidth",
    "-ExifImageHeight",
    # GPS (# = force numeric output)
    "-GPSLatitude#",
    "-GPSLongitude#",
    "-GPSAltitude#",
    # Exposure
    "-ISO",
    "-FNumber",
    "-ExposureTime",
    "-FocalLength#",
    # Software
    "-Software",
    # File info
    "-FileSize#",
    "-MIMEType",
    # ── Metadata enrichment ──────────────────────────────────────────────────
    "-Rating",            # XMP-xmp:Rating  (0–5 stars)
    "-Keywords",          # IPTC:Keywords   (string or list)
    "-Subject",           # XMP:Subject     (same as Keywords in XMP)
    "-Description",       # XMP:Description (preferred over ImageDescription)
    "-ImageDescription",  # EXIF 0x010e     (fallback)
    "-Label",             # XMP-xmp:Label   (color label string)
    # ── Video-specific ───────────────────────────────────────────────────────
    "-Duration#",         # length in seconds (numeric)
    "-VideoFrameRate",    # frames per second
    "-CompressorID",      # video codec id (e.g. avc1, hvc1)
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_exiftool() -> str:
    # On Windows, ExifTool ships as exiftool.exe (or exiftool(-k).exe renamed)
    for candidate in ("exiftool", "exiftool.exe"):
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError(
        "exiftool not found in PATH.\n"
        "  Windows: download from https://exiftool.org and add to PATH\n"
        "           (rename 'exiftool(-k).exe' → 'exiftool.exe')\n"
        "  macOS:   brew install exiftool\n"
        "  Ubuntu:  apt install libimage-exiftool-perl"
    )


def _chunks(lst: list, size: int) -> Iterator[list]:
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ---------------------------------------------------------------------------
# ExifTool batch runner
# ---------------------------------------------------------------------------

class ExifToolBatch:
    """
    Run ExifTool over lists of files in batches.

    Usage::

        with ExifToolBatch() as et:
            for batch in chunks(files, 200):
                results = et.read(batch)
                # results: dict[str, dict]  path → exif fields
    """

    def __init__(self, batch_size: int = DEFAULT_BATCH_SIZE):
        self.batch_size = batch_size
        self._exe = _find_exiftool()

    def __enter__(self) -> "ExifToolBatch":
        return self

    def __exit__(self, *_):
        pass  # stateless — nothing to clean up

    def read(self, paths: list[Path]) -> dict[str, dict]:
        """
        Read EXIF for a list of Path objects.
        Returns a dict keyed by absolute path string.
        Missing files or parse errors are silently omitted (caller handles).
        """
        if not paths:
            return {}

        cmd = [
            self._exe,
            "-json",
            "-q",           # quiet: suppress minor warnings
            "-fast2",       # header only — much faster
            "-charset", "utf8",
            *WANTED_TAGS,
            "--",           # end of options; filenames follow
            *[str(p) for p in paths],
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,   # 200 files should never take this long
            )
        except subprocess.TimeoutExpired:
            # Return empty — scanner will mark these as errors
            return {}
        except Exception:
            return {}

        if not result.stdout.strip():
            return {}

        try:
            records = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}

        out: dict[str, dict] = {}
        for rec in records:
            src = rec.get("SourceFile")
            if src:
                # Normalize to OS path string (ExifTool always uses forward
                # slashes on Windows; callers key by str(Path(...)) which uses
                # backslashes — converting via Path() makes them consistent.)
                out[str(Path(src))] = rec
        return out

    def read_batched(
        self, paths: list[Path]
    ) -> Iterator[tuple[list[Path], dict[str, dict]]]:
        """
        Yield (batch, results) tuples, batching automatically.
        Lets the caller update progress after each batch.
        """
        for batch in _chunks(paths, self.batch_size):
            yield batch, self.read(batch)


# ---------------------------------------------------------------------------
# EXIF field extraction helpers
# ---------------------------------------------------------------------------

def parse_datetime(exif: dict) -> str | None:
    """
    Extract the best available datetime string from EXIF.

    Serves both photos and videos. For Apple/QuickTime videos, CreationDate
    carries the local timezone offset and is preferred over the UTC CreateDate.
    """
    for key in ("DateTimeOriginal", "CreationDate", "CreateDate", "MediaCreateDate"):
        val = exif.get(key)
        if val and val not in ("0000:00:00 00:00:00", ""):
            return val
    return None


def parse_gps(exif: dict) -> tuple[float | None, float | None, float | None]:
    """Return (lat, lon, alt) or (None, None, None)."""
    try:
        lat = float(exif["GPSLatitude"])
        lon = float(exif["GPSLongitude"])
        alt = float(exif.get("GPSAltitude") or 0) or None
        return lat, lon, alt
    except (KeyError, TypeError, ValueError):
        return None, None, None


def parse_dimensions(exif: dict) -> tuple[int, int]:
    """Return (width, height); 0 if unavailable."""
    def _int(val) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    w = _int(exif.get("ImageWidth") or exif.get("ExifImageWidth"))
    h = _int(exif.get("ImageHeight") or exif.get("ExifImageHeight"))
    return w, h


def parse_iso(exif: dict) -> int | None:
    try:
        return int(exif["ISO"])
    except (KeyError, TypeError, ValueError):
        return None


def parse_focal_length(exif: dict) -> float | None:
    val = exif.get("FocalLength")
    if val is None:
        return None
    try:
        # ExifTool may return "50 mm" or just 50
        return float(str(val).split()[0])
    except ValueError:
        return None


def parse_aperture(exif: dict) -> float | None:
    val = exif.get("FNumber")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Metadata enrichment helpers (rating / keywords / description / label)
# ---------------------------------------------------------------------------

def parse_rating(exif: dict) -> int | None:
    """
    Return 0–5 star rating, or None if not set.

    Sources checked in priority order:
      XMP-xmp:Rating → EXIF Rating
    Some software stores 0 as "no rating"; we preserve it.
    """
    val = exif.get("Rating")
    if val is None:
        return None
    try:
        r = int(float(str(val)))
        return max(0, min(5, r))
    except (TypeError, ValueError):
        return None


def parse_keywords(exif: dict) -> str | None:
    """
    Return keywords as a compact JSON array string, or None if absent.

    ExifTool may return Keywords / Subject as:
      - a single string  → wrap in list
      - a list of strings → use as-is
    XMP Subject is preferred over IPTC Keywords when both exist.
    """
    import json as _json

    raw = exif.get("Subject") or exif.get("Keywords")
    if raw is None:
        return None

    if isinstance(raw, str):
        kws = [raw.strip()] if raw.strip() else []
    elif isinstance(raw, list):
        kws = [str(k).strip() for k in raw if str(k).strip()]
    else:
        kws = []

    return _json.dumps(kws, ensure_ascii=False) if kws else None


def parse_description(exif: dict) -> str | None:
    """Return the best available textual description, or None."""
    # XMP Description is richer than the old EXIF ImageDescription field
    val = exif.get("Description") or exif.get("ImageDescription")
    if not val:
        return None
    s = str(val).strip()
    return s if s else None


def parse_label(exif: dict) -> str | None:
    """Return XMP color label string (e.g. 'Red', 'Yellow'), or None."""
    val = exif.get("Label")
    if not val:
        return None
    s = str(val).strip()
    return s if s else None


# ---------------------------------------------------------------------------
# Video-specific helpers (duration / codec / frame rate)
# ---------------------------------------------------------------------------

def parse_duration(exif: dict) -> float | None:
    """Return video length in seconds, or None. (-Duration# = numeric)"""
    val = exif.get("Duration")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_codec(exif: dict) -> str | None:
    """Return the video codec id (e.g. 'avc1', 'hvc1'), or None."""
    val = exif.get("CompressorID")
    if not val:
        return None
    s = str(val).strip()
    return s if s else None


def parse_framerate(exif: dict) -> float | None:
    """Return video frame rate (fps), or None."""
    val = exif.get("VideoFrameRate")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
