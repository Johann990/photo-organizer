-- Photo Organizer — SQLite Schema
-- All timestamps stored as ISO-8601 strings (UTC)

-- ============================================================
-- Core file index
-- ============================================================
CREATE TABLE IF NOT EXISTS files (
    file_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path             TEXT    NOT NULL UNIQUE,  -- absolute path on external drive
    filename         TEXT    NOT NULL,
    extension        TEXT    NOT NULL,         -- lowercase, no dot: arw, jpg, cr3
    size_bytes       INTEGER,
    mtime            TEXT,                     -- file system modification time

    -- Classification
    file_type        TEXT    NOT NULL          -- RAW | CAMERA_JPEG | RESIZED_JPEG | UNKNOWN
                     CHECK(file_type IN ('RAW','CAMERA_JPEG','RESIZED_JPEG','UNKNOWN')),

    -- EXIF core fields
    datetime_original TEXT,                   -- from EXIF DateTimeOriginal
    datetime_digitized TEXT,
    camera_make      TEXT,
    camera_model     TEXT,
    width            INTEGER,
    height           INTEGER,
    gps_lat          REAL,
    gps_lon          REAL,
    gps_alt          REAL,
    lens_model       TEXT,
    iso              INTEGER,
    aperture         REAL,
    shutter_speed    TEXT,
    focal_length     REAL,
    software         TEXT,                    -- editing software if exported

    -- Hashes (computed locally, not from drive every time)
    sha256           TEXT,                    -- exact duplicate detection
    phash            TEXT,                    -- perceptual hash as 16-char hex (imagehash str)

    -- Pairing
    raw_pair_id      INTEGER REFERENCES files(file_id),  -- JPEG → its RAW partner
    jpeg_pair_id     INTEGER REFERENCES files(file_id),  -- RAW → its JPEG partner

    -- Processing state
    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','scanned','hashed','flagged','confirmed','done','error')),
    error_msg        TEXT,

    -- Audit
    scanned_at       TEXT,
    updated_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_sha256      ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_phash       ON files(phash);
CREATE INDEX IF NOT EXISTS idx_files_datetime    ON files(datetime_original);
CREATE INDEX IF NOT EXISTS idx_files_type        ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_status      ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_camera      ON files(camera_make, camera_model);

-- ============================================================
-- Duplicate pairs
-- ============================================================
CREATE TABLE IF NOT EXISTS duplicates (
    dup_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id_a        INTEGER NOT NULL REFERENCES files(file_id),
    file_id_b        INTEGER NOT NULL REFERENCES files(file_id),
    dup_type         TEXT    NOT NULL CHECK(dup_type IN ('EXACT','NEAR')),
    hamming_distance INTEGER,                 -- for NEAR dupes; 0 = identical phash
    keep_file_id     INTEGER REFERENCES files(file_id),  -- set after review
    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','reviewed','resolved')),
    resolved_at      TEXT,
    UNIQUE(file_id_a, file_id_b)
);

-- ============================================================
-- Planned file operations (preview before execution)
-- ============================================================
CREATE TABLE IF NOT EXISTS operations (
    op_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id          INTEGER NOT NULL REFERENCES files(file_id),
    op_type          TEXT    NOT NULL
                     CHECK(op_type IN ('MOVE','RENAME','STAGE_DELETE','DELETE')),
    source_path      TEXT    NOT NULL,
    target_path      TEXT,                   -- NULL for DELETE
    status           TEXT    NOT NULL DEFAULT 'planned'
                     CHECK(status IN ('planned','confirmed','done','error','skipped')),
    error_msg        TEXT,
    planned_at       TEXT,
    executed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_ops_status ON operations(status);
CREATE INDEX IF NOT EXISTS idx_ops_file   ON operations(file_id);

-- ============================================================
-- Scan resumption state
-- ============================================================
CREATE TABLE IF NOT EXISTS scan_state (
    state_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    directory        TEXT    NOT NULL UNIQUE,
    files_found      INTEGER DEFAULT 0,
    files_scanned    INTEGER DEFAULT 0,
    completed        INTEGER DEFAULT 0,      -- 0 = in progress, 1 = done
    started_at       TEXT,
    completed_at     TEXT
);

-- ============================================================
-- Phase completion checkpoints
-- ============================================================
CREATE TABLE IF NOT EXISTS phases (
    phase_name       TEXT    PRIMARY KEY,    -- scan | report | dedup | review | execute
    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','running','complete','error')),
    started_at       TEXT,
    completed_at     TEXT,
    summary_json     TEXT                    -- JSON blob with phase stats
);

INSERT OR IGNORE INTO phases(phase_name) VALUES
    ('scan'), ('report'), ('dedup_exact'), ('dedup_near'), ('review'), ('execute');

-- ============================================================
-- Run log (errors + events)
-- ============================================================
CREATE TABLE IF NOT EXISTS run_log (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    level            TEXT    NOT NULL CHECK(level IN ('INFO','WARN','ERROR')),
    phase            TEXT,
    file_id          INTEGER REFERENCES files(file_id),
    path             TEXT,
    message          TEXT    NOT NULL,
    logged_at        TEXT    NOT NULL
);

-- ============================================================
-- User-defined camera list (to identify "taken by others")
-- ============================================================
CREATE TABLE IF NOT EXISTS known_cameras (
    camera_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    make             TEXT,
    model            TEXT    NOT NULL,
    owner            TEXT    DEFAULT 'self', -- 'self' | 'other'
    UNIQUE(make, model)
);
