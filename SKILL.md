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

**All pipeline phases are fully implemented.** This skill is a concise working reference.
For the complete command reference see [`docs/COMMANDS.md`](docs/COMMANDS.md); for full bilingual docs see `README.md`; for confirmed user decisions see `CLAUDE.md`.

---

## Project layout

```
C:\Projects\PhotoOrganizer\
  photo_organizer\          ← Python package (python -m photo_organizer …)
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
  CLAUDE.md                 ← confirmed decisions + doc map (CLI usage → docs/COMMANDS.md)
  README.md                 ← full bilingual docs
```

---

## CLI — core subcommands

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
  Masters\{YYYY}\{YYYY-MM-DD} {event}\{original_filename}      ← single-day, known_cameras
  Masters\{YYYY}\{start}({N}d) {event}\{original_filename}     ← multi-day 2–30 d
  Masters\{YYYY}\{start}({N}d) {event}\{mmdd}\{filename}       ← per-day split (opt-in)
  Masters\{event}\{YYYY}\{original_filename}                   ← subject (span > 30 d, named)
  Others\…                                                     ← not in known_cameras
  NoDate\…                                                     ← no EXIF + no mtime
  {event-folder}\Videos\{date}_{seq:04d}.EXT                   ← video co-located with event
  Videos\NoDate\video_{seq:04d}.EXT                            ← video with no date
  _staging\to_delete\{stage_reason}\{file_id}_{filename}       ← safe deletion queue
```

- `{event}` = source parent folder name (sanitised). Omitted for date/serial/drive-root names → `{YYYY-MM-DD}/` or `{start}({N}d)/`.
- Span > 30 days with NO event name → per-day `{YYYY-MM-DD} {event}/` + WARN in run_log.
- Span > 30 days WITH event name → subject collection (`{event}/{YYYY}/`); confirm in `review --organize`.
- Videos co-locate in the **same event folder** as the event's photos (`Masters` or `Others` follows the event's photos).
- Per-day split: opt-in via `review --organize`; each file lands in `{mmdd}/` by its own date.
- Name collisions → `_conflict_N` suffix + WARN in run_log.
- No EXIF date → fall back to filesystem **mtime** (WARN in run_log); only truly date-less files go to `NoDate/`.
- `_staging/to_delete/` sub-folders by `stage_reason`: `resized_jpeg/` `exact_dupe/` `near_dupe/` `redundant_copy/` `folder_merge/`.
- **`review` → `plan`**: `review` records near-dupe decisions in the `duplicates` table only; `plan` reads them and creates the `STAGE_DELETE`. Run `review` before `plan` (or re-run `plan`). `plan --force` won't wipe review decisions.

---

## Video date resolution

Priority order (first applicable wins):
1. Human override in `folder_overrides`
2. Filename date — treated as **MEDIUM** confidence
3. Sibling photo date — borrow the folder's photo date (`_sibling_date_hints`); only fills videos still at LOW/no-date
4. `mtime` — LOW confidence (may be copy date)

ExifTool tag order: `CreationDate` (Apple, includes timezone) → `CreateDate` → `MediaCreateDate`.  
HIGH/MEDIUM true dates are **never** overwritten by sibling borrowing.  
`base` (Masters vs Others) follows the event's photos (any known-camera photo → Masters, else Others); falls back to the video's own camera when no event resolves.

---

## DB key tables & columns

**`files`** — one row per file  
`file_id` · `path` · `file_type` · `datetime_original` · `camera_make` · `camera_model`  
`sha256` · `phash` (16-char hex TEXT) · `software` · `width` · `height`  
`rating` (INTEGER 0–5, NULL = unrated) · `keywords` (JSON array) · `description` · `label` (XMP colour)  
`date_source` ∈ `exif_original | filename | exif_digitized | mtime | none`  
`date_confidence` ∈ `HIGH | MEDIUM | LOW` (NULL when no date)  
`duration_seconds` · `video_codec` · `frame_rate` (video only)  
`status`: `pending → scanned → hashed → flagged → confirmed → done | error`

**`duplicates`** — `file_id_a`, `file_id_b`, `dup_type` (EXACT|NEAR), `keep_file_id`  
**`operations`** — `file_id`, `op_type` (MOVE|STAGE_DELETE|RENAME|DELETE), `source_path`, `target_path`, `status`,  
  `stage_reason` (`resized_jpeg|exact_dupe|near_dupe|redundant_copy|folder_merge_loser`), `dupe_of_file_id`  
**`run_log`** — `level` (INFO|WARN|ERROR), `phase`, `path`, `message`

`phash` is stored as **16-char hex TEXT** (`str(imagehash.phash(img))`). Older DBs used a signed INTEGER workaround — migrate with `dedup --force` (re-hash from disk) or `scripts/migrate_phash_to_hex.py` (DB-only, no disk read).

---

## Redundant-copy auto-staging

`plan` uses union-find to group every copy of the same shot into a connected component, keeping only the best (`keep_score`) and staging the rest as `STAGE_DELETE / redundant_copy`.

Two safe link edges:
1. **Same stem + same capture time + matching aspect ratio** — the same frame re-saved or downscaled at any size (even pHash-drifted thumbnails). A *different* aspect (crop/rotation) is NOT linked → kept.
2. **Same capture time + identical non-junk pHash** — catches renamed exports (e.g. `image00017.jpg`). Only links a non-camera-original export name to a shot; two different camera-original filenames (e.g. `IMG_9606` vs `IMG_9607`) are distinct shutter actuations and are **never** linked → stay in near review.

**Folder rule**: a JPEG inside a derivative folder (`share/resize/export/web/thumb/…`) with a same-stem master in the same event → staged directly (downscaled+cropped exports collide in pHash and often have date stripped; folder name is the reliable signal).

Junk pHash exclusion: a pHash shared by ≥ 8 files is not used as a link edge.  
RAW and VIDEO are out of scope. Unique shots (single-member component) are never staged.  
These ops are exempt from the 1-day byte-survival safety net and excluded from near review.

---

## Near-review cluster filters

`dedup` records all pairs within the Hamming threshold. `review` / `review --web` applies three filters when forming clusters:

1. **Exclude EXACT losers** — byte-identical duplicates (same sha256) are already staged by `plan`; only the winner appears in clusters.
2. **Exclude junk pHashes** — a pHash shared by ≥ 8 files (low-information images: dark scenes, plain backgrounds) produces false collisions; the whole batch is excluded.
3. **Tight cluster threshold (`_CLUSTER_HAMMING`, default 2)** — union-find is single-linkage and would chain unrelated look-alikes into a giant blob at the detection threshold; re-grouping at distance ≤ 2 prevents this while still correctly uniting genuine bursts (same folder, seconds apart, every link ≤ 2).

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
