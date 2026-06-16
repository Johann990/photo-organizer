"""
Tests for `_resolve_event_folder` bounding the climb at the scan root.

The climb skips camera-dump / container / device folders to find the event or
subject label ABOVE them. Without an upper bound it can over-shoot all the way
to a top-level folder that merely isn't a recognised container, mislabelling it
a subject. Observed on the real test folder:

    C:/PhotoTestZone/20101101_TX5/DCIM/old/DSC03639.JPG
        old (container) → DCIM (container) → 20101101_TX5 (device pattern,
        container) → PhotoTestZone (NOT a container) → wrongly chosen as subject

The fix: never climb ABOVE a scan-input root. At/below the root a real event is
still found; if only containers/dumps remain up to the root → None (no-event).
The default (scan_roots=None) keeps the prior unbounded behaviour, so existing
callers/tests are unaffected.

Run: python -m pytest tests/test_resolve_scan_root.py
"""

from __future__ import annotations

from pathlib import Path

from photo_organizer.planner import _resolve_event_folder


def test_resolve_does_not_climb_above_scan_root():
    root = Path("C:/PhotoTestZone/20101101_TX5")
    p = root / "DCIM" / "old" / "DSC03639.JPG"
    # old/DCIM/root are all containers/dumps; PhotoTestZone sits ABOVE the root
    # and must NOT be chosen → no event.
    assert _resolve_event_folder(p, scan_roots=[root]) is None


def test_resolve_finds_event_below_scan_root():
    root = Path("D:/DCIM_Storage")
    p = root / "Depository_aged" / "20050814 蒙古" / "0808" / "IMG_1.JPG"
    # A real event folder beneath the root is still resolved — the bound only
    # stops the climb from going ABOVE the root.
    assert (_resolve_event_folder(p, scan_roots=[root])
            == root / "Depository_aged" / "20050814 蒙古")


def test_scan_root_itself_can_be_the_event():
    root = Path("E:/Kyoto Trip")
    p = root / "100CANON" / "IMG_1.JPG"
    # Scanning an event folder directly: the root IS the event label.
    assert _resolve_event_folder(p, scan_roots=[root]) == root


def test_unbounded_default_still_climbs_above():
    # Without scan_roots the prior (unbounded) behaviour is preserved.
    root = Path("C:/PhotoTestZone/20101101_TX5")
    p = root / "DCIM" / "old" / "DSC03639.JPG"
    assert _resolve_event_folder(p) == Path("C:/PhotoTestZone")
