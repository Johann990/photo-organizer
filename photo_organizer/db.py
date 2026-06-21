"""
db.py — SQLite database wrapper for photo-organizer.

All DB access goes through this module. Never import sqlite3 directly
in other modules; use Database instead.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Schema (matches references/schema.sql)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    file_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path              TEXT    NOT NULL UNIQUE,
    filename          TEXT    NOT NULL,
    extension         TEXT    NOT NULL,
    size_bytes        INTEGER,
    mtime             TEXT,

    file_type         TEXT    NOT NULL
                      CHECK(file_type IN ('RAW','CAMERA_JPEG','DEV_JPEG','RESIZED_JPEG','VIDEO','HEIC','UNKNOWN')),

    datetime_original  TEXT,
    datetime_digitized TEXT,
    camera_make        TEXT,
    camera_model       TEXT,
    width              INTEGER,
    height             INTEGER,
    gps_lat            REAL,
    gps_lon            REAL,
    gps_alt            REAL,
    lens_model         TEXT,
    iso                INTEGER,
    aperture           REAL,
    shutter_speed      TEXT,
    focal_length       REAL,
    software           TEXT,

    sha256             TEXT,
    phash              TEXT,      -- perceptual hash as 16-char hex (imagehash str)

    -- Metadata enrichment (scanned from EXIF/XMP/IPTC)
    rating             INTEGER,   -- 0–5 star rating; NULL = not set
    keywords           TEXT,      -- JSON array, e.g. '["Japan","Travel"]'
    description        TEXT,      -- EXIF ImageDescription or XMP Description
    label              TEXT,      -- XMP color label ("Red", "Yellow", …)

    -- Video-specific metadata (NULL for images)
    duration_seconds   REAL,      -- video length in seconds
    video_codec        TEXT,      -- e.g. "avc1", "hvc1"
    frame_rate         REAL,      -- frames per second

    raw_pair_id        INTEGER REFERENCES files(file_id),
    jpeg_pair_id       INTEGER REFERENCES files(file_id),

    status             TEXT    NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','scanned','hashed','flagged','confirmed','done','error')),
    error_msg          TEXT,
    scanned_at         TEXT,
    updated_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_sha256   ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_phash    ON files(phash);
CREATE INDEX IF NOT EXISTS idx_files_datetime ON files(datetime_original);
CREATE INDEX IF NOT EXISTS idx_files_type     ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_status   ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_camera   ON files(camera_make, camera_model);

CREATE TABLE IF NOT EXISTS duplicates (
    dup_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id_a        INTEGER NOT NULL REFERENCES files(file_id),
    file_id_b        INTEGER NOT NULL REFERENCES files(file_id),
    dup_type         TEXT    NOT NULL CHECK(dup_type IN ('EXACT','NEAR')),
    hamming_distance INTEGER,
    keep_file_id     INTEGER REFERENCES files(file_id),
    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','reviewed','resolved')),
    resolved_at      TEXT,
    UNIQUE(file_id_a, file_id_b)
);

CREATE TABLE IF NOT EXISTS folder_overlaps (
    overlap_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_a     TEXT    NOT NULL,
    folder_b     TEXT    NOT NULL,
    shared_count INTEGER NOT NULL,
    a_only_count INTEGER NOT NULL,
    b_only_count INTEGER NOT NULL,
    coverage_a   REAL    NOT NULL,
    coverage_b   REAL    NOT NULL,
    keeper       TEXT    CHECK(keeper IN ('a','b') OR keeper IS NULL),
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','reviewed')),
    reviewed_at  TEXT,
    UNIQUE(folder_a, folder_b)
);

CREATE TABLE IF NOT EXISTS folder_overrides (
    source_folder TEXT PRIMARY KEY,   -- absolute Windows path of the source parent folder
    event_name    TEXT,               -- override event label (NULL = no override)
    date_override TEXT,               -- override date 'YYYY-MM-DD' (NULL = no override)
    note          TEXT,
    updated_at    TEXT,
    per_day_split INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS operations (
    op_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id      INTEGER NOT NULL REFERENCES files(file_id),
    op_type      TEXT    NOT NULL
                 CHECK(op_type IN ('MOVE','RENAME','STAGE_DELETE','DELETE')),
    source_path  TEXT    NOT NULL,
    target_path  TEXT,
    status       TEXT    NOT NULL DEFAULT 'planned'
                 CHECK(status IN ('planned','confirmed','in_progress','done','error','skipped')),
    error_msg    TEXT,
    planned_at   TEXT,
    executed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_ops_status ON operations(status);
CREATE INDEX IF NOT EXISTS idx_ops_file   ON operations(file_id);

CREATE TABLE IF NOT EXISTS scan_state (
    state_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    directory     TEXT    NOT NULL UNIQUE,
    files_found   INTEGER DEFAULT 0,
    files_scanned INTEGER DEFAULT 0,
    completed     INTEGER DEFAULT 0,
    started_at    TEXT,
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS phases (
    phase_name    TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','running','complete','error')),
    started_at    TEXT,
    completed_at  TEXT,
    summary_json  TEXT
);

INSERT OR IGNORE INTO phases(phase_name) VALUES
    ('scan'),('report'),('dedup_exact'),('dedup_near'),('review'),('execute');

CREATE TABLE IF NOT EXISTS run_log (
    log_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    level     TEXT    NOT NULL CHECK(level IN ('INFO','WARN','ERROR')),
    phase     TEXT,
    file_id   INTEGER REFERENCES files(file_id),
    path      TEXT,
    message   TEXT    NOT NULL,
    logged_at TEXT    NOT NULL
);

-- One row per CLI command invocation — wall-clock timing history.
-- Accumulates (never overwrites) so `timings` can show last/avg/runs and
-- future ETA estimates can read historical throughput.
CREATE TABLE IF NOT EXISTS command_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command          TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'ok'
                     CHECK(status IN ('ok','error','interrupted')),
    started_at       TEXT    NOT NULL,
    finished_at      TEXT    NOT NULL,
    duration_seconds REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_command_runs_cmd ON command_runs(command);

CREATE TABLE IF NOT EXISTS known_cameras (
    camera_id INTEGER PRIMARY KEY AUTOINCREMENT,
    make      TEXT,
    model     TEXT NOT NULL,
    owner     TEXT DEFAULT 'self',
    UNIQUE(make, model)
);

-- Tiny key/value table for durable database-wide metadata. The 'schema_version'
-- row lets a newer build refuse to operate on a database written by an even
-- newer (unknown) build, and lets older databases be recognised and migrated.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Bump whenever the on-disk schema changes in a way a fresh `connect()` (running
# SCHEMA_SQL + _apply_migrations) brings an old DB up to. The stored marker is a
# guard, not the migration mechanism: migrations stay idempotent ALTERs.
SCHEMA_VERSION = 6


class SchemaVersionError(RuntimeError):
    """Raised when a database was written by a newer, unsupported schema."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema migration — adds columns introduced after initial deploy
# ---------------------------------------------------------------------------

def _apply_migrations(conn: sqlite3.Connection) -> None:
    """
    Idempotent: adds any missing columns to existing databases.
    Safe to run on both fresh and already-populated DBs.
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(files)").fetchall()
    }
    new_cols: list[tuple[str, str]] = [
        ("rating",           "INTEGER"),  # 0–5 star rating (XMP/EXIF Rating)
        ("keywords",         "TEXT"),     # JSON array of keyword strings
        ("description",      "TEXT"),     # EXIF ImageDescription / XMP Description
        ("label",            "TEXT"),     # XMP color label
        ("duration_seconds", "REAL"),     # video length in seconds
        ("video_codec",      "TEXT"),     # video codec id
        ("frame_rate",       "REAL"),     # video frames per second
        ("date_confidence",  "TEXT"),     # HIGH / MEDIUM / LOW (date forensics)
        ("date_source",      "TEXT"),     # exif_original / filename / exif_digitized / mtime / none
    ]
    for col_name, col_type in new_cols:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col_name} {col_type}")
    conn.commit()

    _migrate_filetype_check(conn)
    _migrate_operations_check(conn)
    _migrate_folder_overrides_per_day_split(conn)


def _migrate_operations_check(conn: sqlite3.Connection) -> None:
    """
    Add 'in_progress' to the operations.status CHECK constraint.

    SQLite cannot ALTER a CHECK constraint, so we rebuild the table when the
    existing schema is missing the new value. No-op on fresh DBs and already-
    migrated ones.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operations'"
    ).fetchone()
    if not row or not row[0] or "'in_progress'" in row[0]:
        return  # already up to date

    cols = [r[1] for r in conn.execute("PRAGMA table_info(operations)").fetchall()]
    col_list = ", ".join(cols)
    new_schema = SCHEMA_SQL.replace(
        "CREATE TABLE IF NOT EXISTS operations (",
        "CREATE TABLE operations_new (",
        1,
    )
    start = new_schema.index("CREATE TABLE operations_new (")
    end = new_schema.index(");", start) + 2

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(new_schema[start:end])
        conn.execute(
            f"INSERT INTO operations_new ({col_list}) SELECT {col_list} FROM operations"
        )
        conn.execute("DROP TABLE operations")
        conn.execute("ALTER TABLE operations_new RENAME TO operations")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_status ON operations(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_file   ON operations(file_id)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_folder_overrides_per_day_split(conn: sqlite3.Connection) -> None:
    """Add folder_overrides.per_day_split to an existing DB. Idempotent: only
    ALTERs when the column is absent (a fresh DB already has it via SCHEMA_SQL)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(folder_overrides)").fetchall()]
    if "per_day_split" not in cols:
        conn.execute(
            "ALTER TABLE folder_overrides ADD COLUMN per_day_split INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()


def _migrate_filetype_check(conn: sqlite3.Connection) -> None:
    """
    SQLite cannot ALTER a CHECK constraint in place. If an existing DB was
    created before newer file_types (VIDEO, DEV_JPEG, HEIC) were allowed,
    rebuild the files table so inserting those rows no longer violates the
    constraint.  The newest type ('HEIC') acts as the up-to-date sentinel.

    No-op on fresh DBs (SCHEMA_SQL already permits HEIC) and on already-migrated DBs.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
    ).fetchone()
    if not row or not row[0] or "'HEIC'" in row[0]:
        return  # already allows the latest file_type set

    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
    col_list = ", ".join(cols)

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        # Recreate the canonical schema as files_new, copy data, swap.
        new_schema = SCHEMA_SQL.replace(
            "CREATE TABLE IF NOT EXISTS files (",
            "CREATE TABLE files_new (",
            1,
        )
        # Extract just the files_new CREATE statement (up to its closing ");").
        start = new_schema.index("CREATE TABLE files_new (")
        end = new_schema.index(");", start) + 2
        conn.execute(new_schema[start:end])
        conn.execute(
            f"INSERT INTO files_new ({col_list}) SELECT {col_list} FROM files"
        )
        conn.execute("DROP TABLE files")
        conn.execute("ALTER TABLE files_new RENAME TO files")
        # Recreate the files indexes (idempotent).
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_files_sha256   ON files(sha256)",
            "CREATE INDEX IF NOT EXISTS idx_files_phash    ON files(phash)",
            "CREATE INDEX IF NOT EXISTS idx_files_datetime ON files(datetime_original)",
            "CREATE INDEX IF NOT EXISTS idx_files_type     ON files(file_type)",
            "CREATE INDEX IF NOT EXISTS idx_files_status   ON files(status)",
            "CREATE INDEX IF NOT EXISTS idx_files_camera   ON files(camera_make, camera_model)",
        ):
            conn.execute(idx_sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _check_schema_version(conn: sqlite3.Connection) -> None:
    """
    Read (or initialise) the durable schema_version marker and guard against
    opening a database written by a NEWER, unknown build.

    - Fresh / pre-marker DB → stamp the current SCHEMA_VERSION (migrations have
      already run idempotently by the time we get here).
    - stored < SCHEMA_VERSION → an older DB that the just-run migrations have
      upgraded; bump the marker.
    - stored > SCHEMA_VERSION → written by a newer build we don't understand →
      refuse, so we never corrupt a library we can't fully interpret.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        return

    try:
        stored = int(row[0])
    except (TypeError, ValueError):
        stored = 0  # corrupt marker → treat as ancient, let migrations own it

    if stored > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Database schema version {stored} is newer than this build supports "
            f"(max {SCHEMA_VERSION}). Upgrade photo-organizer, or open the library "
            "with the version that wrote it. Refusing to proceed to avoid data loss."
        )
    if stored < SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()


def default_db_path(target_root: str | Path) -> Path:
    """
    Where the durable library database lives when the user did not pass --db.

    The DB is the durable record of every organising decision, so it belongs
    WITH the library it describes: `{target}/.photo_organizer/library.db`.
    Back up that folder = back up your organising decisions.
    """
    return Path(target_root) / ".photo_organizer" / "library.db"


def _chunks(lst: list, size: int) -> Iterator[list]:
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    """Thread-safe (one connection per instance) SQLite wrapper."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    # ---- connection --------------------------------------------------------

    def connect(self) -> "Database":
        # Create the parent directory if needed (e.g. the default
        # {target}/.photo_organizer/ home), so a fresh library DB can be created.
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA_SQL)
        _apply_migrations(self._conn)
        _check_schema_version(self._conn)
        return self

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self.connect()

    def __exit__(self, *_):
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Use 'with Database(...) as db'.")
        return self._conn

    # ---- files table -------------------------------------------------------

    def get_scanned_paths(self) -> set[str]:
        """Return paths already in DB (for resume)."""
        rows = self.conn.execute(
            "SELECT path FROM files WHERE status != 'error'"
        ).fetchall()
        return {r["path"] for r in rows}

    def insert_file_batch(self, rows: list[dict[str, Any]]):
        """Insert a batch of file rows. Skips duplicates (INSERT OR IGNORE)."""
        if not rows:
            return
        sql = """
            INSERT OR IGNORE INTO files (
                path, filename, extension, size_bytes, mtime,
                file_type,
                datetime_original, datetime_digitized,
                camera_make, camera_model,
                width, height,
                gps_lat, gps_lon, gps_alt,
                lens_model, iso, aperture, shutter_speed, focal_length, software,
                rating, keywords, description, label,
                duration_seconds, video_codec, frame_rate,
                status, scanned_at, updated_at
            ) VALUES (
                :path, :filename, :extension, :size_bytes, :mtime,
                :file_type,
                :datetime_original, :datetime_digitized,
                :camera_make, :camera_model,
                :width, :height,
                :gps_lat, :gps_lon, :gps_alt,
                :lens_model, :iso, :aperture, :shutter_speed, :focal_length, :software,
                :rating, :keywords, :description, :label,
                :duration_seconds, :video_codec, :frame_rate,
                'scanned', :scanned_at, :scanned_at
            )
        """
        now = _now()
        for row in rows:
            row.setdefault("scanned_at", now)
        self.conn.executemany(sql, rows)
        self.conn.commit()

    def log_error(self, path: str, message: str, phase: str = "scan"):
        """Insert an error row for a file that failed to scan."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO files
                (path, filename, extension, size_bytes, file_type, status, error_msg, scanned_at, updated_at)
            VALUES (?, ?, ?, 0, 'UNKNOWN', 'error', ?, ?, ?)
            """,
            (path, Path(path).name, Path(path).suffix.lstrip(".").lower(),
             message, _now(), _now()),
        )
        self.conn.execute(
            "INSERT INTO run_log (level, phase, path, message, logged_at) VALUES (?,?,?,?,?)",
            ("ERROR", phase, path, message, _now()),
        )
        self.conn.commit()

    def count_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as n FROM files GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def count_by_type(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT file_type, COUNT(*) as n FROM files GROUP BY file_type"
        ).fetchall()
        return {r["file_type"]: r["n"] for r in rows}

    def total_files(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def iter_files(
        self,
        file_type: str | None = None,
        file_types: list[str] | None = None,
        status: str | None = None,
        batch_size: int = 500,
    ) -> Iterator[list[sqlite3.Row]]:
        """Yield batches of file rows for memory-efficient iteration."""
        where_clauses = []
        params: list[Any] = []
        if file_types:
            ph = ",".join("?" * len(file_types))
            where_clauses.append(f"file_type IN ({ph})")
            params.extend(file_types)
        elif file_type:
            where_clauses.append("file_type = ?")
            params.append(file_type)
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"SELECT * FROM files {where} ORDER BY file_id"

        cursor = self.conn.execute(sql, params)
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            yield batch

    _UPDATABLE_FILE_COLS: frozenset[str] = frozenset({
        "path", "filename", "extension", "size_bytes", "mtime",
        "file_type", "datetime_original", "datetime_digitized",
        "camera_make", "camera_model", "width", "height",
        "gps_lat", "gps_lon", "gps_alt", "lens_model",
        "iso", "aperture", "shutter_speed", "focal_length", "software",
        "sha256", "phash",
        "rating", "keywords", "description", "label",
        "duration_seconds", "video_codec", "frame_rate",
        "raw_pair_id", "jpeg_pair_id",
        "status", "error_msg", "scanned_at", "updated_at",
        "date_source", "date_confidence",
    })

    def update_file(self, file_id: int, **kwargs):
        unknown = set(kwargs) - self._UPDATABLE_FILE_COLS
        if unknown:
            raise ValueError(f"update_file: unknown column(s): {sorted(unknown)}")
        kwargs["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = :{k}" for k in kwargs)
        kwargs["file_id"] = file_id
        self.conn.execute(
            f"UPDATE files SET {set_clause} WHERE file_id = :file_id", kwargs
        )
        # Don't commit here — caller batches commits for performance

    def commit(self):
        self.conn.commit()

    # ---- scan_state --------------------------------------------------------

    def upsert_scan_state(self, directory: str, files_found: int = 0,
                          files_scanned: int = 0, completed: bool = False):
        now = _now()
        self.conn.execute(
            """
            INSERT INTO scan_state (directory, files_found, files_scanned, completed, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(directory) DO UPDATE SET
                files_found   = excluded.files_found,
                files_scanned = excluded.files_scanned,
                completed     = excluded.completed,
                completed_at  = CASE WHEN excluded.completed = 1 THEN excluded.completed_at ELSE completed_at END
            """,
            (directory, files_found, files_scanned, int(completed), now,
             now if completed else None),
        )
        self.conn.commit()

    def get_completed_directories(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT directory FROM scan_state WHERE completed = 1"
        ).fetchall()
        return {r["directory"] for r in rows}

    # ---- phases ------------------------------------------------------------

    def phase_complete(self, phase_name: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM phases WHERE phase_name = ?", (phase_name,)
        ).fetchone()
        return row is not None and row["status"] == "complete"

    def set_phase_status(self, phase_name: str, status: str,
                         summary: dict | None = None):
        now = _now()
        self.conn.execute(
            """
            UPDATE phases SET status = ?, summary_json = ?,
                started_at   = CASE WHEN ? = 'running'  THEN ? ELSE started_at   END,
                completed_at = CASE WHEN ? = 'complete' THEN ? ELSE completed_at END
            WHERE phase_name = ?
            """,
            (status, json.dumps(summary) if summary else None,
             status, now, status, now, phase_name),
        )
        self.conn.commit()

    # ---- command timings ---------------------------------------------------

    def record_command_run(
        self,
        command: str,
        started_at: str,
        finished_at: str,
        duration_seconds: float,
        status: str = "ok",
    ) -> None:
        """Append one wall-clock timing row for a CLI command invocation."""
        self.conn.execute(
            """
            INSERT INTO command_runs
                (command, status, started_at, finished_at, duration_seconds)
            VALUES (?, ?, ?, ?, ?)
            """,
            (command, status, started_at, finished_at, duration_seconds),
        )
        self.conn.commit()

    def command_timings(self) -> list[dict]:
        """Per-command timing summary: runs, last/avg/min/max seconds, last time.

        Ordered by most-recently-run first.
        """
        rows = self.conn.execute(
            """
            SELECT
                command,
                COUNT(*)                AS runs,
                AVG(duration_seconds)   AS avg_s,
                MIN(duration_seconds)   AS min_s,
                MAX(duration_seconds)   AS max_s,
                MAX(finished_at)        AS last_at,
                (SELECT duration_seconds FROM command_runs c2
                  WHERE c2.command = c1.command
                  ORDER BY c2.finished_at DESC LIMIT 1) AS last_s,
                (SELECT status FROM command_runs c3
                  WHERE c3.command = c1.command
                  ORDER BY c3.finished_at DESC LIMIT 1) AS last_status
            FROM command_runs c1
            GROUP BY command
            ORDER BY last_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- duplicates --------------------------------------------------------

    def insert_duplicate(self, file_id_a: int, file_id_b: int,
                         dup_type: str, hamming: int | None = None):
        # Normalise order so (a,b) and (b,a) are treated as same pair
        a, b = (file_id_a, file_id_b) if file_id_a < file_id_b else (file_id_b, file_id_a)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO duplicates
                (file_id_a, file_id_b, dup_type, hamming_distance)
            VALUES (?, ?, ?, ?)
            """,
            (a, b, dup_type, hamming),
        )

    def insert_duplicate_batch(
        self,
        pairs: list[tuple[int, int, str, int | None]],
    ) -> None:
        """Bulk-insert near-duplicate pairs.  ~10-50× faster than per-call inserts."""
        normalized = [
            (a, b, t, h) if a < b else (b, a, t, h)
            for a, b, t, h in pairs
        ]
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO duplicates
                (file_id_a, file_id_b, dup_type, hamming_distance)
            VALUES (?, ?, ?, ?)
            """,
            normalized,
        )

    def count_duplicates(self, dup_type: str | None = None) -> int:
        if dup_type:
            return self.conn.execute(
                "SELECT COUNT(*) FROM duplicates WHERE dup_type = ?", (dup_type,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]

    # ---- folder_overlaps ---------------------------------------------------

    def clear_folder_overlaps(self):
        self.conn.execute("DELETE FROM folder_overlaps")

    def insert_folder_overlap(self, *, folder_a, folder_b, shared_count,
                              a_only_count, b_only_count, coverage_a,
                              coverage_b, keeper=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO folder_overlaps "
            "(folder_a, folder_b, shared_count, a_only_count, b_only_count, "
            " coverage_a, coverage_b, keeper) VALUES (?,?,?,?,?,?,?,?)",
            (folder_a, folder_b, shared_count, a_only_count, b_only_count,
             coverage_a, coverage_b, keeper),
        )

    def iter_folder_overlaps(self, pending_only: bool = False):
        sql = "SELECT * FROM folder_overlaps"
        if pending_only:
            sql += " WHERE status='pending'"
        sql += " ORDER BY shared_count DESC"
        return self.conn.execute(sql).fetchall()

    def record_folder_overlap_decision(self, overlap_id: int, keeper, reviewed_at: str) -> None:
        self.conn.execute(
            "UPDATE folder_overlaps SET status='reviewed', keeper=?, reviewed_at=? "
            "WHERE overlap_id=?",
            (keeper, reviewed_at, overlap_id),
        )
        if self.conn.execute("SELECT changes()").fetchone()[0] == 0:
            raise KeyError(f"overlap_id {overlap_id} not found in folder_overlaps")

    def reopen_folder_overlap(self, overlap_id: int) -> None:
        self.conn.execute(
            "UPDATE folder_overlaps SET status='pending', reviewed_at=NULL "
            "WHERE overlap_id=?",
            (overlap_id,),
        )
        if self.conn.execute("SELECT changes()").fetchone()[0] == 0:
            raise KeyError(f"overlap_id {overlap_id} not found in folder_overlaps")

    # ---- folder_overrides --------------------------------------------------

    def set_folder_override(self, source_folder: str, *, event_name=None,
                            date_override=None, note=None, per_day_split=0,
                            updated_at: str) -> None:
        """Upsert a per-folder override. event_name/date_override = None clears
        that column; per_day_split (0/1) flags an event for per-day {mmdd}/ split.
        Stores the full row keyed by source_folder."""
        self.conn.execute(
            "INSERT INTO folder_overrides "
            "(source_folder, event_name, date_override, note, per_day_split, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source_folder) DO UPDATE SET "
            "event_name=excluded.event_name, date_override=excluded.date_override, "
            "note=excluded.note, per_day_split=excluded.per_day_split, "
            "updated_at=excluded.updated_at",
            (source_folder, event_name, date_override, note, int(per_day_split), updated_at),
        )

    def clear_folder_override(self, source_folder: str) -> None:
        """Remove a folder's override row. Raises KeyError if none existed."""
        self.conn.execute(
            "DELETE FROM folder_overrides WHERE source_folder=?", (source_folder,)
        )
        if self.conn.execute("SELECT changes()").fetchone()[0] == 0:
            raise KeyError(f"no folder_override for {source_folder!r}")

    def get_folder_overrides(self) -> dict:
        """Return {source_folder: sqlite3.Row} for every override (event or date)."""
        return {
            r["source_folder"]: r
            for r in self.conn.execute("SELECT * FROM folder_overrides")
        }

    # ---- run_log -----------------------------------------------------------

    def log(self, level: str, message: str, phase: str | None = None,
            path: str | None = None, file_id: int | None = None):
        self.conn.execute(
            "INSERT INTO run_log (level, phase, file_id, path, message, logged_at) VALUES (?,?,?,?,?,?)",
            (level, phase, file_id, path, message, _now()),
        )
        # Batch: caller commits periodically

    # ---- known cameras -----------------------------------------------------

    def add_known_camera(self, make: str | None, model: str, owner: str = "self"):
        self.conn.execute(
            "INSERT OR IGNORE INTO known_cameras (make, model, owner) VALUES (?,?,?)",
            (make, model, owner),
        )
        self.conn.commit()

    def get_known_camera_models(self, owner: str = "self") -> set[str]:
        """Return lowercase model strings for case-insensitive matching."""
        rows = self.conn.execute(
            "SELECT model FROM known_cameras WHERE owner = ?", (owner,)
        ).fetchall()
        return {r["model"].lower() for r in rows}

    # ---- reporting helpers -------------------------------------------------

    def camera_model_counts(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            """
            SELECT COALESCE(camera_model, 'Unknown') as model, COUNT(*) as n
            FROM files GROUP BY camera_model ORDER BY n DESC
            """
        ).fetchall()
        return [(r["model"], r["n"]) for r in rows]

    def date_range(self) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            "SELECT MIN(datetime_original) as mn, MAX(datetime_original) as mx FROM files"
        ).fetchone()
        return row["mn"], row["mx"]

    def no_exif_date_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE datetime_original IS NULL"
        ).fetchone()[0]

    def ratings_distribution(self) -> dict[int | None, int]:
        """Return count per rating value (0–5, plus None = unrated)."""
        rows = self.conn.execute(
            "SELECT rating, COUNT(*) as n FROM files GROUP BY rating"
        ).fetchall()
        return {r["rating"]: r["n"] for r in rows}

    def top_keywords(self, limit: int = 20) -> list[tuple[str, int]]:
        """
        Return (keyword, count) pairs for the most-used keywords.
        Explodes the JSON array stored in the keywords column.
        Requires SQLite ≥ 3.38 for json_each; falls back gracefully.
        """
        try:
            rows = self.conn.execute(
                """
                SELECT value AS kw, COUNT(*) AS n
                FROM files, json_each(files.keywords)
                WHERE files.keywords IS NOT NULL
                GROUP BY value
                ORDER BY n DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [(r["kw"], r["n"]) for r in rows]
        except Exception:
            return []   # json_each not available — skip silently

    def has_any_metadata_field(self) -> bool:
        """Quick check: do any files have rating / keywords / label?"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM files "
            "WHERE rating IS NOT NULL OR keywords IS NOT NULL OR label IS NOT NULL"
        ).fetchone()
        return row[0] > 0

    def resolution_buckets(self) -> dict[str, int]:
        """JPEG resolution distribution."""
        gt4000 = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_type != 'RAW' AND width > 4000"
        ).fetchone()[0]
        mid = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_type != 'RAW' AND width BETWEEN 1000 AND 4000"
        ).fetchone()[0]
        small = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE file_type != 'RAW' AND width < 1000 AND width > 0"
        ).fetchone()[0]
        return {">4000px": gt4000, "1000–4000px": mid, "<1000px": small}
