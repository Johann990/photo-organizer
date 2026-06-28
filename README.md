# Photo Organizer

> **中文 / English** — 本文件雙語並列  
> A resumable, IO-efficient photo library organizer for large collections (300,000–500,000+ files) on external drives.  
> 可中斷續跑、IO 高效的大型照片庫整理工具，專為外接硬碟上的 30–50 萬張照片設計。

---

## Table of Contents / 目錄

1. [Overview / 概述](#overview--概述)
2. [Prerequisites / 前置需求](#prerequisites--前置需求)
3. [Installation / 安裝](#installation--安裝)
4. [Quick Start / 快速開始](#quick-start--快速開始)
5. [config.json Reference / 設定檔說明](#configjson-reference--設定檔說明)
6. [CLI Reference / 命令列說明](#cli-reference--命令列說明)
7. [Pipeline Walkthrough / 流程詳解](#pipeline-walkthrough--流程詳解)
8. [Output Directory Structure / 輸出目錄結構](#output-directory-structure--輸出目錄結構)
9. [Database Schema / 資料庫結構](#database-schema--資料庫結構)
10. [Troubleshooting / 常見問題](#troubleshooting--常見問題)

---

## Overview / 概述

**English**  
Photo Organizer is a command-line tool that scans, deduplicates, and reorganises a photo library stored on external drives. It follows a **scan-once, operate-from-DB** architecture: all metadata is read from disk exactly once and stored in a local SQLite database; every subsequent phase (deduplication, planning, execution) works entirely against that database, minimising slow external-drive IO.

**中文**  
Photo Organizer 是一套命令列工具，用於掃描、去重、整理儲存在外接硬碟上的照片庫。採用「**掃一次，從 DB 操作**」架構：所有 metadata 只從磁碟讀取一次並存入本地 SQLite；後續所有階段（去重、規劃、執行）都直接操作資料庫，大幅降低對外接慢速硬碟的 IO 需求。

### Key Features / 主要功能

| Feature | 功能 |
|---------|------|
| Scan 300k+ files with ExifTool batch mode | 以 ExifTool 批次模式掃描 30 萬張以上 |
| Resumable at any phase | 任何階段中斷後可續跑 |
| Exact dedup (SHA-256) + near-dedup (pHash) | 精確去重 (SHA-256) + 近似去重 (pHash) |
| Interactive near-duplicate review | 近似重複人工審查 CLI |
| Preserves ratings, keywords, color labels | 保留 EXIF 評分、關鍵字、顏色標籤 |
| Multi-source input directories | 支援多個來源目錄 |
| Windows-first, macOS-compatible | Windows 優先，相容 macOS |
| No vendor lock-in (SQLite + pathlib) | 不綁定任何軟體（SQLite + pathlib） |

---

## Prerequisites / 前置需求

### Python / Python 環境

```
Python >= 3.11
```

### Python Packages / Python 套件

```bash
pip install rich Pillow imagehash
```

| Package | Version | Purpose / 用途 |
|---------|---------|---------------|
| `rich` | ≥ 13 | Progress bars, tables / 進度條、表格 |
| `Pillow` | ≥ 10 | Image loading for pHash / 讀取圖片做感知雜湊 |
| `imagehash` | ≥ 4.3 | Perceptual hashing / 感知雜湊 |

### ExifTool / ExifTool 外部工具

**Windows**  
1. Download from https://exiftool.org (Windows Executable)  
2. Rename `exiftool(-k).exe` → `exiftool.exe`  
3. Move to any folder in your `PATH` (e.g. `C:\Windows\System32\`)

**macOS**  
```bash
brew install exiftool
```

**Ubuntu/Debian**  
```bash
apt install libimage-exiftool-perl
```

### Verify Installation / 確認安裝

```bash
python -c "import rich, PIL, imagehash; print('Python packages OK')"
exiftool -ver
```

Both commands should succeed before proceeding.  
兩個都成功才能繼續。

---

## Installation / 安裝

```bash
git clone <repo>
cd PhotoOrganizer
pip install rich Pillow imagehash
```

No `setup.py` or wheel is needed — run directly as a module.  
不需要安裝套件，直接用 `-m` 執行即可。

---

## Quick Start / 快速開始

### Step 1 — Create config.json / 建立設定檔

Copy the example and fill in your paths:  
複製範例並填入你的路徑：

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
    "db":      "C:/photos.db",
    "target":  "E:/Organised",
    "workers": 4,
    "input_dirs": [
        "E:/Photos"
    ],
    "known_cameras": []
}
```

> **Note / 注意**: Leave `known_cameras` empty for now. After the scan report you will know the exact EXIF model strings to fill in.  
> 先留空 `known_cameras`，掃描報告出來後再填入正確的 EXIF Model 字串。

### Step 2 — Validate / 驗證設定

```bash
python -m photo_organizer validate --config config.json
```

This checks your config, ExifTool, disk access, and runs a sample scan on ~50 files per directory — **nothing is changed**.  
此步驟驗證設定、ExifTool、磁碟存取權限，並對每個目錄抽樣掃描 50 個檔案——**不會更動任何檔案**。

### Step 3 — Run the full pipeline / 執行完整流程

```bash
python -m photo_organizer scan     --config config.json
python -m photo_organizer report   --config config.json
# → review the report; add known_cameras to config.json
python -m photo_organizer dedup    --config config.json
python -m photo_organizer review   --config config.json   # optional near-dupe review
python -m photo_organizer plan     --config config.json
python -m photo_organizer execute  --config config.json
```

---

## config.json Reference / 設定檔說明

```jsonc
{
    // Path to the SQLite index database (created automatically)
    // SQLite 索引資料庫路徑（自動建立）
    "db": "C:/photos.db",

    // Destination root for the reorganised library
    // Must be on the SAME drive as at least one input_dir (os.rename requirement)
    // 整理後的目標根目錄
    // 必須與 input_dirs 在同一顆硬碟（os.rename 要求）
    "target": "E:/Organised",

    // Parallel ExifTool threads
    // 4 = safe for spinning HDD  |  8-16 = fine for SSD
    // 平行 ExifTool 執行緒數
    // HDD 建議 4；SSD 可以 8–16
    "workers": 4,

    // Hamming distance threshold for near-duplicate detection (default 8)
    // Lower = stricter (fewer pairs); higher = more permissive (more pairs)
    // 近似重複偵測的 Hamming 距離閾值（預設 8）
    // 越小越嚴格（配對少）；越大越寬鬆（配對多）
    "hamming_threshold": 8,

    // Enable secondary resized-JPEG signals (folder names, filename suffixes)
    // Only turn on after reviewing the scan report
    // 是否啟用二次 resized JPEG 信號（資料夾名、檔名後綴）
    // 只在看過掃描報告後再開啟
    "use_secondary_signals": false,

    // Source directories to scan — must NOT be filesystem roots (E:\, C:\, /)
    // 來源目錄清單——不能是磁碟根目錄
    "input_dirs": [
        "E:/Photos/2018-2022",
        "D:/Backup/Camera"
    ],

    // Your own cameras — files from these models go to Masters/
    // Files from any other model go to Others/
    // Use the exact Model string from EXIF (see scan report or validate output)
    // 自己的相機型號——這些型號的照片進 Masters/
    // 其他型號進 Others/
    // 填入 EXIF 的 Model 字串（見掃描報告或 validate 輸出）
    // Matching is case-insensitive / 比對大小寫不敏感
    "known_cameras": [
        { "make": "Sony",  "model": "ILCE-7RM2"    },
        { "make": "Sony",  "model": "ILCE-7M4"     },
        { "make": "Apple", "model": "iPhone 14 Pro" }
    ]
}
```

### Finding Your Camera's EXIF Model String / 找出相機的 EXIF Model 字串

After scanning, run the report to see exact model strings:  
掃描完後跑報告可以看到確切的 Model 字串：

```bash
python -m photo_organizer report --config config.json
```

Or check a single file with ExifTool directly:  
也可以直接用 ExifTool 查單一檔案：

```bash
exiftool -Model "E:/Photos/DSC00001.ARW"
```

Common mappings / 常見對照：

| Marketing Name / 行銷名稱 | EXIF Model |
|--------------------------|------------|
| Sony A7R II | `ILCE-7RM2` |
| Sony A7R III | `ILCE-7RM3` |
| Sony A7R IV | `ILCE-7RM4` |
| Sony A7 IV | `ILCE-7M4` |
| Sony A7C | `ILCE-7C` |
| Canon EOS R5 | `Canon EOS R5` |
| Canon EOS R6 | `Canon EOS R6` |
| Nikon Z6 II | `NIKON Z 6_2` |
| Fujifilm X-T5 | `X-T5` |
| iPhone 14 Pro | `iPhone 14 Pro` |

---

## CLI Reference / 命令列說明

All commands accept `--config config.json` as an alternative to specifying individual flags.  
所有命令都支援 `--config config.json`，作為個別旗標的替代方案。

> This section covers the **core pipeline** commands. For the **complete 18-command reference** (incl. `add`, `sync`, `relocate`, `clone`, `reconcile`, `audit`, `folder-merge`, `timings`) see [`docs/COMMANDS.md`](docs/COMMANDS.md).  
> 本節只列**核心流程**指令;**完整 18 個指令**(含 `add`、`sync`、`relocate`、`clone`、`reconcile`、`audit`、`folder-merge`、`timings`)見 [`docs/COMMANDS.md`](docs/COMMANDS.md)。

---

### `validate` — Pre-flight check / 正式執行前的驗證

```bash
python -m photo_organizer validate --config config.json
```

**What it does / 做什麼**:
- Validates `config.json` structure and path rules  
  驗證 `config.json` 結構和路徑規則
- Confirms ExifTool is installed and prints its version  
  確認 ExifTool 已安裝並顯示版本
- Checks database path is writable  
  確認資料庫路徑可寫入
- Checks source and target are on the same drive  
  確認來源和目標在同一顆硬碟
- Runs a sample scan (~50 files per input_dir) — **no files are changed**  
  執行抽樣掃描（每個來源目錄約 50 個檔案）——**不更動任何檔案**

**Exit code / 結束代碼**: `0` = all OK, `1` = errors found

---

### `scan` — Phase 1: Index all files / 索引所有檔案

```bash
# With config (recommended) / 使用設定檔（建議）
python -m photo_organizer scan --config config.json

# Single directory / 單一目錄
python -m photo_organizer scan E:\Photos --db C:\photos.db

# Resume after interruption / 中斷後續跑（自動）
python -m photo_organizer scan --config config.json

# Force re-scan / 強制重新掃描
python -m photo_organizer scan --config config.json --force
```

| Flag | Default | Description / 說明 |
|------|---------|-------------------|
| `--workers N` | `4` | Parallel ExifTool threads / 平行執行緒 |
| `--secondary` | off | Enable secondary resized-JPEG signals / 啟用二次 resized 信號 |
| `--force` | off | Re-scan already-indexed files / 重新掃描已索引的檔案 |

**Resumable**: The scan checkpoints after every 200-file batch. If interrupted, rerun the exact same command — already-scanned files are skipped automatically.  
**可中斷續跑**：每 200 個檔案存一次進度。中斷後重跑相同指令，已掃描的檔案自動跳過。

---

### `report` — Phase 2: Scan report / 掃描報告

```bash
python -m photo_organizer report --config config.json
```

Prints a summary of everything in the database:  
顯示資料庫中所有資料的摘要：

- File type breakdown (RAW / CAMERA_JPEG / DEV_JPEG / RESIZED_JPEG / HEIC / VIDEO / UNKNOWN)  
  檔案類型分布（`DEV_JPEG` = RAW 沖出 / 軟體編輯過的 JPEG）
- JPEG resolution distribution  
  JPEG 解析度分布
- Video summary (count, total duration, codecs) — shown when videos exist  
  影片摘要（數量、總時長、編碼）——有影片時顯示
- Date range and files without EXIF date  
  日期範圍與缺少 EXIF 日期的檔案數
- Top camera models  
  相機型號排行
- Star ratings distribution (if any photos are rated)  
  星級評分分布（如果有評分）
- Top keywords (if any photos have keywords)  
  熱門關鍵字（如果有關鍵字）

**Review this report before proceeding to dedup.**  
**繼續執行去重前請先審查此報告。**

---

### `dedup` — Phase 3: Duplicate detection / 重複偵測

```bash
python -m photo_organizer dedup --config config.json
```

Two passes / 兩個步驟：

**Pass A — Exact duplicates (SHA-256)**  
**步驟 A — 精確重複（SHA-256）**

Hashes every file. Identical SHA-256 = exact duplicate. The copy with the richer (more organised) path is kept; all others are staged for deletion.  
雜湊每個檔案。SHA-256 相同 = 完全重複。路徑較豐富（更有組織）的版本保留，其他進入暫存刪除。

**Pass B — Near duplicates (pHash)**  
**步驟 B — 近似重複（pHash）**

Computes perceptual hash for JPEG files. Pairs with Hamming distance ≤ threshold are flagged for **human review** and never auto-deleted.  
對 JPEG 計算感知雜湊。Hamming 距離 ≤ 閾值的配對標記為**人工審查**，不會自動刪除。

| Flag | Default | Description / 說明 |
|------|---------|-------------------|
| `--exact-only` | — | Only run SHA-256 pass / 只跑精確去重 |
| `--near-only` | — | Only run pHash pass / 只跑近似去重 |
| `--hamming N` | `8` | Near-dup threshold / 近似重複閾值 |

---

### `review` — Near-duplicate interactive review / 近似重複人工審查

```bash
python -m photo_organizer review --config config.json
```

Shows each near-duplicate pair side-by-side with metadata. Commands:  
並排顯示每對近似重複照片的 metadata。操作按鍵：

| Key | Action / 動作 |
|-----|--------------|
| `a` | Keep A, stage B for deletion / 保留 A，B 進暫存刪除 |
| `b` | Keep B, stage A for deletion / 保留 B，A 進暫存刪除 |
| `k` | Keep both / 兩個都保留 |
| `s` | Skip this pair / 跳過此配對 |
| `q` | Quit and save progress / 離開並儲存進度 |

Progress is saved after every decision. Re-run to continue reviewing remaining pairs.  
每次決定後立即儲存。重新執行可以繼續未完成的審查。

---

### `unknown-cameras` — Distribution of files with no camera model / 無相機型號分佈

```bash
python -m photo_organizer unknown-cameras --config config.json
```

Breaks down every file whose `camera_model` is empty — by **file type, year, make, software, and source folder** — so you can tell whether they are scanned film, phone/app exports, screenshots, or other people's files. DB-only; no disk read, no files touched. These files move to `Others/` unless you add their model to `known_cameras`.  
把所有 `camera_model` 為空的檔案依**類型／年份／廠牌／軟體／來源資料夾**展開，方便判斷它們是掃描底片、手機 App 匯出、截圖還是別人給的檔案。純算 DB、不讀碟、不動檔案。這些檔案會進 `Others/`（除非把型號加進 `known_cameras`）。

---

### `reclassify` — Re-run classification from the DB / 重新分類（不讀碟）

```bash
python -m photo_organizer reclassify --config config.json
```

Recomputes `file_type` for `CAMERA_JPEG` / `DEV_JPEG` rows using the `software` / `width` already stored at scan time — **no disk read, no ExifTool**. Use it to apply the `DEV_JPEG` split (or any classifier change) without re-scanning the whole drive.  
用掃描時已存的 `software` / `width` 重算 `CAMERA_JPEG` / `DEV_JPEG` 列的 `file_type`——**不讀磁碟、不跑 ExifTool**。用來套用 `DEV_JPEG` 分類（或任何分類規則變更），不必整碟重掃。

- DB-only → seconds even for 300k+ rows (vs minutes–hours for a full scan)  
  純算 DB → 數十萬列也只要數秒（整碟重掃要數十分鐘～數小時）
- Idempotent; only `CAMERA_JPEG` ↔ `DEV_JPEG` can change. Add `--secondary` to match the scan-time setting.  
  Idempotent；只有 `CAMERA_JPEG` ↔ `DEV_JPEG` 會變動。可加 `--secondary` 對齊掃描時設定。

---

### `plan` — Phase 4: Build action plan / 建立操作計劃

```bash
python -m photo_organizer plan --config config.json
```

Computes the complete list of operations and shows a preview:  
計算完整的操作清單並顯示預覽：

```
╔══════════════════════════════════════════════════════════════════════╗
║                       ACTION PLAN (preview)                          ║
╠══════════════════════════════════════════════════════════════════════╣
║ Stage for deletion — Resized JPEGs          │  24,891  │ path=resized ║
║ Stage for deletion — Exact duplicates       │   4,821  │ SHA-256 match║
║ Move + rename → Masters/ or Others/         │ 285,545  │ by date/cam  ║
║ Near-duplicates (needs review)              │   2,406  │ NOT auto-stgd║
╚══════════════════════════════════════════════════════════════════════╝

  Space to reclaim: ~50.5 GB  (moved to _staging/, not permanently deleted)

Confirm? [y/N]
```

**You must type `y` to confirm.** Only after confirmation will Phase 5 be allowed to run.  
**必須輸入 `y` 確認。** 確認後 Phase 5 才能執行。

`plan` also lists **no-event source folders** — folders whose name is just a date or serial number (empty, `2023-06-15`, `20230615`, pure numbers, camera dumps like `100CANON` / `DCIM` / `IMG_1234`) and were likely never organised. Folders with a real event name (`Kyoto`, `京都`, `2023 Summer`) are not listed. The full list is recorded in `run_log` for later review:  
`plan` 還會列出**名稱只是日期或流水號的來源資料夾**（空白、`2023-06-15`、`20230615`、純數字、`100CANON`、`DCIM`、`IMG_1234` 等；有真正事件名的不列入），完整清單寫進 `run_log` 供日後檢查：

```sql
SELECT path, message FROM run_log
WHERE phase='review' AND message LIKE 'No-event%';
```

Target path override:  
覆蓋目標路徑：

```bash
python -m photo_organizer plan --config config.json --target F:\NewDrive\Organised
```

---

### `execute` — Phase 5: Execute operations / 執行操作

```bash
python -m photo_organizer execute --config config.json
```

Executes all confirmed operations using `os.rename()` (no data copy — instant on same drive):  
使用 `os.rename()` 執行所有已確認的操作（同碟移動，不複製資料，幾乎瞬間完成）：

1. Creates target directory tree as needed  
   自動建立所需的目標目錄
2. Moves resized JPEGs and exact duplicates to `_staging/to_delete/`  
   將 resized JPEG 和精確重複移至 `_staging/to_delete/`
3. Moves and renames kept files into `Masters/` or `Others/`  
   將保留的檔案移動並重命名至 `Masters/` 或 `Others/`
4. Resolves filename collisions automatically (`_conflict_N` suffix)  
   自動處理目標檔名衝突（加 `_conflict_N` 後綴）
5. Verifies file count before vs after  
   驗證前後檔案總數

**No files are permanently deleted.** Staged files remain in `_staging/to_delete/` for manual review before permanent removal.  
**不會永久刪除任何檔案。** 暫存的檔案保留在 `_staging/to_delete/`，供人工確認後再永久刪除。

#### Selective / batched move / 選擇性分批搬移

Run only a subset now; the rest stays `confirmed` for a later run. Filters combine, and `execute` stays re-runnable (no `--force`) until everything is moved.  
只搬一部分，其餘維持 `confirmed` 待下次；條件可組合，全部搬完前 `execute` 可重複執行（不需 `--force`）。

```bash
python -m photo_organizer execute --config config.json --year 2023
python -m photo_organizer execute --config config.json --camera ILCE-7RM2
python -m photo_organizer execute --config config.json --software Lightroom
python -m photo_organizer execute --config config.json --type DEV_JPEG
```

| Flag | Meaning / 說明 |
|------|----------------|
| `--year YYYY` | EXIF year, e.g. `2023` / 該 EXIF 年份 |
| `--camera TEXT` | camera_model contains TEXT / 相機型號含此字串 |
| `--software TEXT` | software contains TEXT, e.g. `Lightroom` / 軟體含此字串 |
| `--type TYPE` | exact file_type (`RAW`/`CAMERA_JPEG`/`DEV_JPEG`/`RESIZED_JPEG`/`VIDEO`) |

---

### `undo` — Revert moved files / 回復已搬移的檔案

```bash
python -m photo_organizer undo --config config.json
```

Moves every completed file back to its original location, using `operations.source_path` (original) and `files.path` (current, incl. any `_conflict_N`). **Never overwrites** — if an original path is already occupied it is skipped and logged. After a clean undo the `execute` phase resets to `pending`, so you can re-plan/re-execute.  
依 `operations.source_path`（原位置）與 `files.path`（現位置，含 `_conflict_N`）把所有已搬檔案搬回原位。**絕不覆蓋** —— 原位置被占用則跳過並寫入 `run_log`（`phase='undo'`）。乾淨還原後 `execute` 階段重置為 `pending`，可重新規劃/執行。

```sql
-- 若有跳過/錯誤，查原因 / inspect skips & errors
SELECT path, message FROM run_log WHERE phase='undo';
```

---

## Pipeline Walkthrough / 流程詳解

```
External Drive (slow)                    Local SSD (fast)
      │                                        │
      │   Phase 1 — scan                       │
      │   ExifTool batch, 200 files/call ────► SQLite DB
      │   Workers: 4 (HDD) / 8-16 (SSD)        │
      │   Resumable via scan_state table        │
      │                                    Phase 2 — report
      │                                    Phase 3 — dedup (SHA-256 + pHash)
      │                                    Phase 3B — near-dupe review
      │                                    Phase 4 — plan + confirm
      │                                        │
      │   Phase 5 — execute             ◄──────┘
      │   os.rename() only
      │   (same drive = no data copy)
      │
   Organised library ready
```

### Typical Timing / 典型耗時

| Phase | 300k files on HDD / HDD 上 30 萬張 | Notes |
|-------|-----------------------------------|-------|
| Scan | 2–5 hours / 2–5 小時 | Depends on file scatter / 視檔案分散度而定 |
| Report | < 1 second / < 1 秒 | DB query only |
| Dedup exact | 1–3 hours / 1–3 小時 | SHA-256 of all files |
| Dedup near | 30–60 min / 30–60 分 | JPEG only |
| Plan | < 1 minute / < 1 分 | DB only |
| Execute | 3–10 min / 3–10 分 | os.rename, no copy |

---

## Output Directory Structure / 輸出目錄結構

```
E:\Organised\                          ← --target root
│
├── Masters\                           ← Your own cameras (known_cameras)
│   ├── 2023\                          ← events filed by year
│   │   ├── 2023-07-01 Picnic\         ← single-day:  {YYYY-MM-DD} {event}
│   │   │   └── DSC00100.ARW           ← ORIGINAL filename kept (no rename)
│   │   ├── 2023-06-15(3d) Kyoto\      ← multi-day (2–30 d):  {start}({N}d) {event}, whole trip in ONE folder
│   │   │   ├── DSC00001.ARW           ← all 3 days live here; RAW+JPEG pair shares stem
│   │   │   ├── DSC00001.JPG
│   │   │   └── Videos\                ← event's videos co-locate here (no global tree)
│   │   │       └── 2023-06-16_0001.MP4
│   │   └── 2023-09-10(5d) Tour\       ← per-day split (opt-in via review --organize)
│   │       ├── 0910\                  ← each file lands in {mmdd}/ by its own date
│   │       │   └── DSC02000.ARW
│   │       └── 0911\
│   │           └── DSC02100.ARW
│   └── Mei\                           ← subject collection: named folder, span > 30 days
│       └── 2023\                      ← event-first then year, never split
│           └── IMG_2001.HEIC          ← HEIC organised like a photo (full EXIF)
│
├── Others\                            ← Other people's cameras
│   └── 2023\
│       └── 2023-08-01\                ← {event} omitted when no folder clue
│           └── IMG_1234.JPG
│
├── NoDate\                            ← No EXIF date and no mtime fallback
│   └── DSC09999.ARW
│
├── Videos\                            ← ONLY date-less videos (can't co-locate with an event)
│   └── NoDate\
│       └── video_0001.MP4
│
└── _staging\
    └── to_delete\                     ← safe deletion queue, split by stage_reason
        ├── resized_jpeg\
        │   └── 42_IMG_resize_001.jpg  ← {file_id}_ prefix avoids collisions
        ├── exact_dupe\
        ├── near_dupe\
        ├── redundant_copy\
        └── folder_merge\
```

### Photo Folder & Filename Format / 照片資料夾與檔名格式

```
單日 / single-day:  {Masters|Others}\{YYYY}\{YYYY-MM-DD} {event}\{original_filename}
多天 / multi-day:   {Masters|Others}\{YYYY}\{start-date}({N}d) {event}\{original_filename}
依日分夾 / per-day:  {Masters|Others}\{YYYY}\{start-date}({N}d) {event}\{mmdd}\{original_filename}
主題 / subject:     {Masters|Others}\{event}\{YYYY}\{original_filename}

例: Masters\2023\2023-07-01 Picnic\DSC00100.ARW         (single day)
    Masters\2023\2023-06-15(3d) Kyoto\DSC00001.ARW      (3-day trip, all files here)
無事件線索時 / when no event clue: Masters\2023\2023-06-15\DSC00001.ARW
```

- Multi-day events (2–30 days sharing one source folder) collapse into a single folder named by start date + day count; span auto-computed from EXIF dates  
  多天活動（同一來源夾、2–30 天）收進單一資料夾，名稱用起始日+天數；跨度由 EXIF 日期自動計算
- Named folder with span > 30 days → **subject collection** `{event}\{YYYY}\` (by year, never split); a date/serial-named folder > 30 days → per-day `{YYYY-MM-DD}\` fallback + WARN  
  有事件名且跨度 > 30 天 → **主題收藏** `{event}\{YYYY}\`(依年份,不拆);只是日期/流水號名的夾 > 30 天 → 退回每日制 + WARN
- **Per-day split (opt-in)**: a multi-day event can split into `{mmdd}\` subfolders by each file's own date, enabled in `review --organize`  
  **依日分夾(選用)**:多天活動可在 `review --organize` 勾選,依每張照片自身日期分進 `{mmdd}\` 子夾
- `HEIC` (iPhone) has full EXIF and is organised like a photo into Masters/Others  
  `HEIC`(iPhone)有完整 EXIF,比照相片整理進 Masters/Others
- Date from `DateTimeOriginal` EXIF field  
  日期來自 EXIF 的 `DateTimeOriginal`
- `{event}` = sanitised source parent folder name; omitted when it is a drive root / empty  
  `{event}` = 來源父資料夾名（清理後）；若為碟機根目錄或空白則省略
- **Original filename is preserved** (no rename) → RAW+JPEG pairs share the same stem naturally  
  **保留原始檔名**（不重命名）→ RAW+JPEG 自然同 stem
- Same-name collisions inside a folder are resolved by the executor (appends `_conflict_N`)  
  同名碰撞由 executor 處理（加 `_conflict_N`）

#### Video Filename Format / 影片檔名格式

```
{event-folder}\Videos\{date}_{seq:04d}.{EXT}     ← co-located in the event's own folder

例: Masters\2023\2023-06-15(3d) Kyoto\Videos\2023-06-16_0001.MP4
無日期 / no date: Videos\NoDate\video_0001.MP4
```

- Date from `CreationDate` (Apple, with timezone) → `CreateDate` → `MediaCreateDate`  
  日期優先取 `CreationDate`（Apple，含時區）→ `CreateDate` → `MediaCreateDate`
- `{event}` = sanitised source parent folder name; omitted when it is a drive root / empty  
  `{event}` = 來源父資料夾名（清理後）；若為碟機根目錄或空白則省略
- Videos co-locate in a `Videos\` subfolder of the **same event folder** their photos use (not a global tree); only date-less videos go to a global `Videos\NoDate\`  
  影片放進**該事件資料夾的 `Videos\` 子夾**(不再用全域樹);只有無日期的影片進全域 `Videos\NoDate\`
- `base` (Masters vs Others) follows the event's photos; the date order above resolves the filename `{date}`  
  `base`(Masters/Others)跟著事件的照片走;檔名的 `{date}` 由上方日期順序決定

---

## Database Schema / 資料庫結構

The SQLite database (`photos.db`) is the single source of truth and can be queried directly with any SQLite tool.  
SQLite 資料庫（`photos.db`）是唯一的資料來源，可直接用任何 SQLite 工具查詢。

### Key Tables / 主要資料表

#### `files` — One row per file / 每個檔案一列

| Column | Type | Description / 說明 |
|--------|------|-------------------|
| `file_id` | INTEGER PK | Auto-increment ID |
| `path` | TEXT | Absolute path (updated after move) / 絕對路徑（移動後更新）|
| `filename` | TEXT | Filename only / 純檔名 |
| `extension` | TEXT | Lowercase, no dot / 小寫，無點 |
| `size_bytes` | INTEGER | File size / 檔案大小 |
| `file_type` | TEXT | `RAW` / `CAMERA_JPEG` / `DEV_JPEG` / `RESIZED_JPEG` / `HEIC` / `VIDEO` / `UNKNOWN` |
| `datetime_original` | TEXT | EXIF DateTimeOriginal |
| `camera_make` | TEXT | EXIF Make |
| `camera_model` | TEXT | EXIF Model |
| `width` / `height` | INTEGER | Native resolution / 原始解析度 |
| `gps_lat` / `gps_lon` | REAL | GPS coordinates / GPS 座標 |
| `sha256` | TEXT | Exact-dedup hash / 精確去重雜湊 |
| `phash` | TEXT | Perceptual hash — 16-char hex `str(imagehash.phash(img))` / 感知雜湊,16 字元 hex |
| `rating` | INTEGER | 0–5 star rating from EXIF/XMP / 評分 |
| `keywords` | TEXT | JSON array of keywords / 關鍵字 JSON 陣列 |
| `description` | TEXT | EXIF/XMP description / 說明文字 |
| `label` | TEXT | XMP color label / XMP 顏色標籤 |
| `duration_seconds` | REAL | Video length in seconds (NULL for images) / 影片時長（影像為 NULL）|
| `video_codec` | TEXT | Video codec id, e.g. `avc1` (NULL for images) / 影片編碼（影像為 NULL）|
| `frame_rate` | REAL | Video frames per second (NULL for images) / 影片幀率（影像為 NULL）|
| `raw_pair_id` | INTEGER FK | Linked RAW file (if this is JPEG) / 配對的 RAW |
| `jpeg_pair_id` | INTEGER FK | Linked JPEG file (if this is RAW) / 配對的 JPEG |
| `status` | TEXT | `pending` / `scanned` / `hashed` / `flagged` / `confirmed` / `done` / `error` |

#### `duplicates` — Duplicate pairs / 重複配對

| Column | Description / 說明 |
|--------|-------------------|
| `file_id_a`, `file_id_b` | The pair (a < b always) / 配對（a 永遠 < b）|
| `dup_type` | `EXACT` or `NEAR` |
| `hamming_distance` | Hamming distance (NEAR only) / Hamming 距離（僅 NEAR）|
| `keep_file_id` | Which file to keep (set by plan/review) / 保留哪個（plan/review 設定）|
| `status` | `pending` / `reviewed` / `resolved` |

#### `operations` — Planned and executed operations / 規劃與執行的操作

| Column | Description / 說明 |
|--------|-------------------|
| `op_type` | `MOVE` / `STAGE_DELETE` / `RENAME` / `DELETE` |
| `source_path` | Original path / 原始路徑 |
| `target_path` | Destination path / 目標路徑 |
| `status` | `planned` / `confirmed` / `done` / `error` / `skipped` |

### Useful Queries / 實用查詢

```sql
-- How many files by type?
-- 各類型檔案數量
SELECT file_type, COUNT(*) FROM files GROUP BY file_type;

-- All 5-star rated photos
-- 所有 5 星評分的照片
SELECT path, camera_model, datetime_original
FROM files WHERE rating = 5 ORDER BY datetime_original;

-- Photos with a specific keyword (requires SQLite ≥ 3.38)
-- 含特定關鍵字的照片
SELECT path FROM files, json_each(files.keywords)
WHERE value = 'Japan';

-- Photos without EXIF date
-- 缺少 EXIF 日期的照片
SELECT path, size_bytes FROM files
WHERE datetime_original IS NULL AND status != 'error';

-- Exact duplicate groups
-- 精確重複群組
SELECT sha256, COUNT(*) as n, GROUP_CONCAT(path, ' | ') as paths
FROM files WHERE sha256 IS NOT NULL
GROUP BY sha256 HAVING n > 1;

-- Failed operations
-- 失敗的操作
SELECT o.op_type, o.source_path, o.error_msg
FROM operations o WHERE o.status = 'error';
```

---

## Troubleshooting / 常見問題

### `exiftool` not found / 找不到 exiftool

**Windows**: Download from https://exiftool.org, rename to `exiftool.exe`, place in a folder that's in your system `PATH` (e.g. `C:\Windows\`).  
**Windows**：從 https://exiftool.org 下載，重新命名為 `exiftool.exe`，放入系統 `PATH` 內的資料夾（例如 `C:\Windows\`）。

```bash
# Test / 測試
exiftool -ver
```

---

### `OSError: [WinError 17]` or cross-device link error

**English**: `os.rename()` cannot move files across different drives. Ensure `--target` is on the same drive letter as your `input_dirs`.  
**中文**：`os.rename()` 不能跨硬碟移動檔案。確認 `--target` 和 `input_dirs` 在同一顆硬碟（同一個磁碟代號）。

```json
// Correct / 正確: both on E:\
"input_dirs": ["E:/Photos"],
"target":     "E:/Organised"

// Wrong / 錯誤: different drives
"input_dirs": ["E:/Photos"],
"target":     "F:/Organised"   ← ✗
```

---

### Scan is very slow / 掃描很慢

- External HDD: ~1,000–2,000 files/sec is normal. 300k files ≈ 2–5 hours.  
  外接 HDD：每秒約 1,000–2,000 個檔案是正常的。30 萬個檔案約需 2–5 小時。
- External SSD: increase `workers` to 8–16.  
  外接 SSD：可以把 `workers` 提高到 8–16。
- If interrupted, just re-run the same command — it resumes from where it stopped.  
  如果中斷，重跑相同命令即可——自動從中斷處續跑。

---

### Near-duplicate pairs are too many / too few / 近似重複配對太多或太少

Adjust `hamming_threshold` in `config.json`:  
調整 `config.json` 中的 `hamming_threshold`：

```json
"hamming_threshold": 4   // Stricter / 更嚴格 — fewer pairs / 較少配對
"hamming_threshold": 12  // More permissive / 更寬鬆 — more pairs / 較多配對
```

Re-run dedup with `--force` after changing:  
更改後加 `--force` 重跑去重：

```bash
python -m photo_organizer dedup --config config.json --near-only --force
```

---

### `known_cameras` not matching / known_cameras 沒有匹配

The matching is **case-insensitive** but must be an **exact model string**.  
比對**大小寫不敏感**，但必須是**完整的 Model 字串**。

Find the correct string:  
找到正確字串的方法：

```bash
# From scan report / 從掃描報告
python -m photo_organizer report --config config.json
# (look at the Camera Models table)

# Or directly on a file / 或直接查單一檔案
exiftool -Model "E:/Photos/DSC00001.ARW"
```

---

### How to permanently delete staged files / 如何永久刪除暫存的檔案

After verifying the staged files look correct:  
確認暫存檔案無誤後：

```bash
# Windows — permanently delete _staging/to_delete/
rmdir /s /q "E:\Organised\_staging\to_delete"

# macOS/Linux
rm -rf "/Volumes/MyDrive/Organised/_staging/to_delete"
```

**Wait at least 30 days** before deleting, to ensure nothing was staged incorrectly.  
**建議等至少 30 天**再刪除，確保沒有錯誤暫存的檔案。

---

### Resuming after any phase / 任何階段中斷後如何續跑

Every phase is idempotent — simply re-run the same command:  
每個階段都是冪等的，重跑相同命令即可：

```bash
# Scan resumes from last completed directory
python -m photo_organizer scan --config config.json

# Dedup skips already-hashed files
python -m photo_organizer dedup --config config.json

# Execute skips already-completed operations
python -m photo_organizer execute --config config.json
```

Use `--force` only if you want to redo a phase from scratch.  
只有想從頭重做某個階段時才用 `--force`。

---

## Contributing / 貢獻

**English**  
Pull requests and issues are welcome. Please keep documentation bilingual (English + Traditional Chinese) to match the style of this project.

**中文**  
歡迎送 PR 或開 issue。請維持英文 + 繁體中文雙語並列的文件風格，與本專案保持一致。

---

## License / 授權

MIT
