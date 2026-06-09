"""
Migrate existing DBs: phash stored as signed INTEGER → 16-char hex TEXT.

Background
----------
The old code stored imagehash.phash() as a *signed* 64-bit INTEGER (via the
former `_to_signed64()` helper) to dodge SQLite's signed-INTEGER overflow on
unsigned 64-bit values. The schema now declares `phash TEXT` and deduper.py
stores `str(imagehash.phash(img))` directly (an unsigned 16-char hex string).

SQLite is dynamically typed, so an existing DB keeps its old INTEGER values in
the (now TEXT-declared) column until each row is rewritten. This script does
that rewrite — no re-hashing, no disk reads of the photos.

Each old signed int is restored to unsigned (add 2**64 if negative) and
formatted as %016x, matching what str(imagehash.phash()) now produces.

Idempotent: rows already stored as TEXT are skipped (filtered by typeof()).
Alternative if you'd rather not run this: `dedup --force` re-hashes from disk.

Usage:
    python scripts/migrate_phash_to_hex.py C:\\photos.db
"""

from __future__ import annotations

import sqlite3
import sys


def migrate(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT file_id, phash FROM files "
            "WHERE phash IS NOT NULL AND typeof(phash) = 'integer'"
        ).fetchall()

        for file_id, signed in rows:
            unsigned = signed + (1 << 64) if signed < 0 else signed
            con.execute(
                "UPDATE files SET phash = ? WHERE file_id = ?",
                (f"{unsigned:016x}", file_id),
            )
        con.commit()
        return len(rows)
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/migrate_phash_to_hex.py <db_path>")
    n = migrate(sys.argv[1])
    print(f"Migrated {n:,} INTEGER phash value(s) to hex TEXT.")
