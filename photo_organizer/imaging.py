"""
imaging.py — Centralised Pillow setup.

Importing this module registers the HEIF/HEIC opener with Pillow (via
``pillow-heif``) so that ``PIL.Image.open()`` can decode iPhone ``.heic`` /
``.heif`` files. Any module that opens images with Pillow should
``import photo_organizer.imaging`` (or import it for its side effect) BEFORE
calling ``Image.open`` on a path that might be HEIC.

pillow-heif is an optional dependency: if it is not installed, HEIC files
simply fail to open (logged as a per-file error) instead of crashing the run.
Install with::

    pip install pillow-heif

/ Imports register HEIC support with Pillow when pillow-heif is available;
absence degrades gracefully (HEIC files error per-file, no crash).
"""

from __future__ import annotations

# Whether HEIC/HEIF decoding is available in this environment.
HEIF_AVAILABLE = False

try:  # optional dependency — graceful when missing
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False
