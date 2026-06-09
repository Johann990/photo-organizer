"""
Verify pHash is stored/read as 16-char hex TEXT and that Hamming distance
still works — including the high-bit-set value that overflowed signed INTEGER.

Run: python -m pytest tests/test_phash_text.py   (or just: python tests/test_phash_text.py)
"""

from __future__ import annotations

import sqlite3

from photo_organizer.db import SCHEMA_SQL


def _hamming(ph_a: str, ph_b: str) -> int:
    """Mirror deduper.dedup_near's comparison."""
    return bin(int(ph_a, 16) ^ int(ph_b, 16)).count("1")


def test_phash_column_is_text():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL)
    cols = {row[1]: row[2] for row in con.execute("PRAGMA table_info(files)")}
    assert cols["phash"] == "TEXT", cols["phash"]


def test_roundtrip_high_bit_set():
    """The original bug: a phash with the high bit set (>= 2**63).

    Stored as hex TEXT it roundtrips verbatim with no signed/unsigned dance.
    """
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_SQL)

    # 0xFFFFFFFFFFFFFFFF = 2**64-1, high bit set — would overflow INTEGER.
    ph = "ffffffffffffffff"
    con.execute(
        "INSERT INTO files (path, filename, extension, file_type, phash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("/x.jpg", "x.jpg", "jpg", "CAMERA_JPEG", ph),
    )
    (got,) = con.execute("SELECT phash FROM files WHERE filename='x.jpg'").fetchone()
    assert got == ph
    assert int(got, 16) == 2**64 - 1


def test_hamming_distance():
    # Identical → 0
    assert _hamming("ffffffffffffffff", "ffffffffffffffff") == 0
    # All bits differ → 64
    assert _hamming("ffffffffffffffff", "0000000000000000") == 64
    # One bit differs → 1
    assert _hamming("0000000000000001", "0000000000000000") == 1
    # High-bit operand counted correctly (the bug the helpers worked around)
    assert _hamming("8000000000000000", "0000000000000000") == 1


def test_lexicographic_sort_matches_numeric():
    """Sliding window relies on this: fixed-width hex sorts == int sort."""
    keys = ["8000000000000000", "00000000000000ff", "ffffffffffffffff", "0000000000000001"]
    by_text = sorted(keys)
    by_int = sorted(keys, key=lambda k: int(k, 16))
    assert by_text == by_int


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All phash TEXT tests passed.")
