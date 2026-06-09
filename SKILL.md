---
name: photo-organizer
description: >
  Maintain and extend the Photo Organizer CLI tool in the current project directory.
  Use this skill for any task involving: adding features, fixing bugs, adjusting
  classification rules, changing output directory structure, modifying the DB
  schema, or answering questions about how the pipeline works.
  Also use when the user runs a command and needs help interpreting output or
  errors, or wants to query the photos.db database.
triggers:
  - photo organizer
  - photo_organizer
  - photos.db
  - scan photos
  - dedup photos
  - reclassify
  - unknown cameras
---

# Photo Organizer — Reference Skill

**All 6 phases are fully implemented.** This skill is a concise working reference.
For full docs see `README.md`; for confirmed user decisions see `CLAUDE.md`.

---

## Project layout

```
C:\Projects\PhotoOrganizer\
  photo-organizer\          ← Python package (python -m photo_organizer …)
    __main__.py             ← CLI entry point; all subcommands wired here
    classifier.py           ← file_type classification (RAW/DEV_JPEG/CAMERA_JPEG/VIDEO/…)
    scanner.py              ← Phase 1: os.walk → ExifTool batch → DB
    reporter.py             ← Phase 2: scan report + unknown-cameras report
    deduper.py              ← Phase 3: SHA-256 exact + pHash near-dup
    reviewer.py             ← Phase 3B: interactive near-dup review CLI
    planner.py              ← Phase 4: build operations table, confirm
    executor.py             ← Phase 5: os.rename + undo
    reclassifier.py         ← DB-only re-classify (no disk read)
    db.py                   ← SQLite wrapper; schema + migrations
    exiftool.py             ← ExifTool subprocess wrapper (batch JSON mode)
    config.py               ← PhotoConfig dataclass; load_config()
    validator.py            ← pre-flight checks (validate subcommand)
    progress.py             ← Rich progress bars / console helpers
  config.example.json
  CLAUDE.md                 ← confirmed decisions, full CLI usage
  README.md                 ← full bilingual docs
```

---

## CLI — all subcommands

```bat
python -m photo_organizer validate        --config config.json
python -m photo_organizer scan            --config config.json
python -m photo_organizer report          --config config.json
python -m photo_organizer unknown-cameras --config config.json   # DB-only
python -m photo_organizer dedup           --config config.json
python -m photo_organizer review          --config config.json   # interactive
python -m photo_organizer plan            --config config.json
python -m photo_organizer execute         --config config.json   # + filters below
python -m photo_organizer undo            --config config.json
python -m photo_organizer reclassify      --config config.json   # DB-only
```

**execute filters** (combinable; rest stays `confirmed` for next run):
```bat
--year 2023  |  --camera ILCE-7RM2  |  --software Lightroom  |  --type DEV_JPEG
```

---

## Key design decisions

| Topic | Decision |
|-------|----------|
| Disk access | **Scan-once** — ExifTool reads disk; all analysis runs from SQLite DB |
| Moves | `os.rename()` only — same drive required; no data copy |
| Deletion | **Never permanent** — moves to `_staging/to_delete/` first |
| Near-dup | Human review required; never auto-deleted |
| Video near-dup | SHA-256 only; pHash is image-only (ffmpeg keyframe = future) |
| Filenames | **Original filenames preserved** (no rename) |
| Platform | Windows-first; `pathlib` throughout for future macOS |

---

## file_type enum

| Type | Meaning |
|------|---------|
| `RAW` | Camera RAW (ARW, CR3, NEF, RAF, DNG …) |
| `CAMERA_JPEG` | Straight-out-of-camera JPEG (no editing software tag) |
| `DEV_JPEG` | Developed/edited JPEG — Software tag matches Lightroom, Capture One, darktable, Photoshop … |
| `RESIZED_JPEG` | Export/resized copy — path contains "resized" (+ secondary signals) |
| `HEIC` | iPhone default (heic/heif) — full EXIF; organized like a photo into Masters/Others |
| `VIDEO` | mp4/mov/m4v/avi/mkv/wmv/flv/webm/mts/m2ts/3gp/mpg |
| `UNKNOWN` | Everything else (PNG, TIFF, WEBP, unrecognised …) — left in place |

`DEV_JPEG` / `HEIC` destination = same as `CAMERA_JPEG` (Masters/Others), just labelled differently.  
After changing classifier rules: `reclassify` updates the DB in seconds without re-scanning.

---

## Output directory structure

```
{target}\
  Masters\{YYYY}\{YYYY-MM-DD}_{event}\{original_filename}   ← known_cameras
  Masters\{YYYY}\{start}_{N}d_{event}\…                     ← multi-day (2-30 d)
  Others\…                                                  ← not in known_cameras
  NoDate\…                                                  ← no EXIF date
  Videos\{YYYY}\{YYYY-MM-DD}_{event}_{seq}.EXT              ← all video
  Videos\NoDate\…
  _staging\to_delete\{file_id}_{filename}                   ← safe deletion queue
```

- `{event}` = source parent folder name (sanitised). Omitted when it is a date/serial/drive-root.
- Span > 30 days → falls back to per-day folders + WARN in run_log.
- Name collisions → `_conflict_N` suffix + WARN in run_log.
- No EXIF date → date taken from filesystem **mtime** (WARN in run_log); only truly date-less files go to `NoDate/`.
- **`review` → `plan`**: `review` records near-dupe decisions in the `duplicates` table only; `plan` reads them and creates the `STAGE_DELETE`. Run `review` before `plan` (or re-run `plan`). `plan --force` won't wipe review decisions.

---

## DB key tables & columns

**`files`** — one row per file  
`file_id` · `path` · `file_type` · `datetime_original` · `camera_make` · `camera_model`  
`sha256` · `phash` · `software` · `width` · `height`  
`rating` · `keywords` · `description` · `label`  
`duration_seconds` · `video_codec` · `frame_rate` (video only)  
`status`: `pending → scanned → hashed → confirmed → done | error`

**`duplicates`** — `file_id_a`, `file_id_b`, `dup_type` (EXACT|NEAR), `keep_file_id`  
**`operations`** — `file_id`, `op_type` (MOVE|STAGE_DELETE), `source_path`, `target_path`, `status`  
**`run_log`** — `level` (INFO|WARN|ERROR), `phase`, `path`, `message`

---

## Schema migrations

`db.py::_apply_migrations()` is called on every `Database.connect()`.  
- Adds missing columns via `ALTER TABLE ADD COLUMN` (idempotent).  
- Rebuilds `files` table when the CHECK constraint is outdated (VIDEO / DEV_JPEG guard).  
- Always safe to run on existing DBs.

---

## Useful run_log queries (sqlite3 or python -c)

```python
import sqlite3; db = sqlite3.connect(r"C:\photos.db"); db.row_factory = sqlite3.Row
# name collisions during execute
list(db.execute("SELECT path, message FROM run_log WHERE phase='execute' AND message LIKE 'Name collision%'"))
# unorganised source folders (date/serial names)
list(db.execute("SELECT path, message FROM run_log WHERE phase='review' AND message LIKE 'No-event%'"))
# undo errors
list(db.execute("SELECT path, message FROM run_log WHERE phase='undo'"))
# source folders spanning >30 days
list(db.execute("SELECT path, message FROM run_log WHERE message LIKE 'Source folder spans%'"))
```
