"""
classifier.py — File type classification logic.

Determines whether a file is RAW, CAMERA_JPEG, RESIZED_JPEG, or UNKNOWN.
Rules are applied in priority order; first match wins.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# RAW extensions (lowercase, no dot)
# ---------------------------------------------------------------------------

RAW_EXTENSIONS: frozenset[str] = frozenset({
    # Sony
    "arw", "srf", "sr2",
    # Canon
    "cr3", "cr2", "crw",
    # Nikon
    "nef", "nrw",
    # Fujifilm
    "raf",
    # Panasonic / Leica
    "rw2", "rwl",
    # Olympus / OM System
    "orf",
    # Pentax / Ricoh
    "pef",
    # Adobe DNG (treat as RAW)
    "dng",
    # Samsung
    "srw",
    # Sigma
    "x3f",
    # Phase One
    "iiq",
    # Hasselblad
    "3fr", "fff",
})

JPEG_EXTENSIONS: frozenset[str] = frozenset({"jpg", "jpeg"})

# ---------------------------------------------------------------------------
# Video extensions (lowercase, no dot)
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    "mp4", "mov", "m4v", "avi", "mkv", "wmv", "flv", "webm",
    "mts", "m2ts", "3gp", "3g2", "mpg", "mpeg",
})

# ---------------------------------------------------------------------------
# Resized signals
# ---------------------------------------------------------------------------

# Confirmed by user: path component named "resized" (case-insensitive)
_RESIZED_FOLDER_CONFIRMED = "resized"

# Secondary folder-name signals (enable after reviewing scan report)
_RESIZED_FOLDER_SECONDARY: frozenset[str] = frozenset({
    "resize", "export", "exports", "share", "shared", "sharing",
    "web", "websize", "web_size", "small", "thumb", "thumbs",
    "thumbnails", "preview", "previews", "instagram", "ig",
    "blog", "send", "sent", "proof", "proofs",
})

# Filename-stem suffix patterns (secondary signal)
_RESIZED_STEM_PATTERNS: list[re.Pattern] = [
    re.compile(r"_sm$"),
    re.compile(r"_small$"),
    re.compile(r"_web$"),
    re.compile(r"_w$"),
    re.compile(r"_thumb$"),
    re.compile(r"_tn$"),
    re.compile(r"_\d{3,4}px$"),   # e.g. _1200px
    re.compile(r"-resized$"),
    re.compile(r"_export$"),
    re.compile(r"_share$"),
    re.compile(r"_ig$"),
    re.compile(r"_blog$"),
]

# RAW developers / serious editors — a JPEG carrying one of these in its
# Software/Creator tag was developed or edited from a RAW (or heavily edited),
# NOT straight out of camera.  Used as a PRIMARY signal to label DEV_JPEG
# (always on, independent of the resized secondary signals).
DEV_SOFTWARE: frozenset[str] = frozenset({
    "lightroom", "camera raw", "capture one", "captureone",
    "darktable", "aperture", "photoshop", "dxo", "photolab",
    "luminar", "on1", "rawtherapee", "affinity", "silkypix",
    "capture nx", "nx studio", "raw power", "exposure x",
    "acdsee", "gimp", "pixelmator",
})

# EXIF software signals (secondary, weak)
_EXPORT_SOFTWARE: frozenset[str] = frozenset({
    "lightroom", "aperture", "capture one", "darktable",
    "photos", "google photos", "flickr", "squarespace",
    "wordpress", "whatsapp", "line", "telegram",
})

# Resolution threshold for secondary signal (pixels wide)
# Very conservative default — tune after scan report
RESIZED_WIDTH_THRESHOLD = 2500


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class FileClassifier:
    """
    Classify a file as RAW, CAMERA_JPEG, RESIZED_JPEG, or UNKNOWN.

    Parameters
    ----------
    use_secondary_signals:
        Enable folder/suffix/software secondary signals.
        Set to True only after reviewing the scan report and confirming
        these patterns apply to this specific library.
    resized_width_threshold:
        Override the default pixel-width threshold for the resolution signal.
    """

    def __init__(
        self,
        use_secondary_signals: bool = False,
        resized_width_threshold: int = RESIZED_WIDTH_THRESHOLD,
    ):
        self.use_secondary = use_secondary_signals
        self.width_threshold = resized_width_threshold

    def classify(self, path: Path, exif: dict) -> str:
        """
        Return one of: 'RAW', 'VIDEO', 'DEV_JPEG', 'CAMERA_JPEG',
        'RESIZED_JPEG', 'UNKNOWN'
        """
        ext = path.suffix.lstrip(".").lower()

        # ── Rule 1: RAW extension ──────────────────────────────────────────
        if ext in RAW_EXTENSIONS:
            return "RAW"

        # ── Rule 1b: Video extension ───────────────────────────────────────
        if ext in VIDEO_EXTENSIONS:
            return "VIDEO"

        # ── Rule 2: Not a JPEG at all → UNKNOWN ───────────────────────────
        if ext not in JPEG_EXTENSIONS:
            return "UNKNOWN"

        # ── Rule 3 (confirmed): path contains "resized" ───────────────────
        path_str_lower = str(path).lower()
        if _RESIZED_FOLDER_CONFIRMED in path_str_lower:
            return "RESIZED_JPEG"

        # Developed-from-RAW / edited JPEG — positive ID via editing software.
        # Always applied (not gated on secondary signals); only distinguishes
        # DEV_JPEG from CAMERA_JPEG, never overrides RESIZED/UNKNOWN below.
        software = (exif.get("Software") or "").lower()
        developed = any(s in software for s in DEV_SOFTWARE)
        camera_or_dev = "DEV_JPEG" if developed else "CAMERA_JPEG"

        if not self.use_secondary:
            return camera_or_dev

        # ── Secondary signals (only when enabled) ─────────────────────────
        score = 0

        # Folder name match (strong: +2)
        parts_lower = {p.lower() for p in path.parts}
        if parts_lower & _RESIZED_FOLDER_SECONDARY:
            score += 2

        # Filename stem suffix (strong: +2)
        stem_lower = path.stem.lower()
        if any(pat.search(stem_lower) for pat in _RESIZED_STEM_PATTERNS):
            score += 2

        # Resolution below threshold (weak: +1)
        width = exif.get("ImageWidth") or exif.get("ExifImageWidth") or 0
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = 0
        if 0 < width < self.width_threshold:
            score += 1

        # Software export signal (weak: +1) — reuses `software` from above
        if any(s in software for s in _EXPORT_SOFTWARE):
            score += 1

        if score >= 2:
            return "RESIZED_JPEG"

        # Score == 1: ambiguous → UNKNOWN (never auto-delete)
        if score == 1:
            return "UNKNOWN"

        return camera_or_dev

    def is_supported(self, path: Path) -> bool:
        """Return True if this file type should be indexed at all."""
        ext = path.suffix.lstrip(".").lower()
        return (
            ext in RAW_EXTENSIONS
            or ext in JPEG_EXTENSIONS
            or ext in VIDEO_EXTENSIONS
            or ext in {"tif", "tiff", "heic", "heif", "png", "webp"}
        )
