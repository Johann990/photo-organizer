# Photo Organizer

讀 SKILL.md 了解完整需求和架構。

## 實作狀態

| Phase | 模組 | 狀態 |
|-------|------|------|
| 1 — Scan & Index | `scanner.py` `exiftool.py` `classifier.py` | ✅ 完成 |
| 2 — Scan Report  | `reporter.py` | ✅ 完成 |
| 3 — Dedup        | `deduper.py` | ✅ 完成 |
| 3B review        | `reviewer.py` | ✅ 完成 |
| 4 — Plan         | `planner.py` | ✅ 完成 |
| 5 — Execute      | `executor.py` | ✅ 完成 |

## 確認的用戶決策

- **RAW 轉出 JPEG**：保留（歸類為 `CAMERA_JPEG`，整理進 Masters/）
- **評分 / 標籤**：掃描時從 EXIF/XMP/IPTC 讀取，存入 DB（`rating`, `keywords`, `description`, `label` 欄位）
- **主平台**：Windows 優先，未來可能遷移 macOS（`pathlib` 全程跨平台）
- **目標儲存**：整理完放 2-Bay external；4×12TB 用途待定

## 照片整理目錄 / Photo output tree

- 單日活動 / single-day：`{target}/Masters|Others/{YYYY}/{YYYY-MM-DD}_{event}/{原始檔名}`
- 多天活動 / multi-day（2–30 天）：`.../{YYYY}/{起始日}_{N}d_{event}/{原始檔名}`
  - 同一來源資料夾橫跨多天 → 整個活動收進**一個**資料夾，年份用起始日
    / whole event in one folder, named by start date + day count (e.g. `2023-06-15_3d_Kyoto`)
  - 跨度 > 30 天（多半是手機傾倒）→ 退回每日制 `{YYYY-MM-DD}_{event}/` 並寫 WARN 到 run_log
  - 跨度由各來源資料夾內照片的 EXIF 最早/最晚日期自動計算 / span auto-computed from EXIF dates
  - `{event}` = 來源父資料夾名（清理後）；取不到時省略 → `{YYYY-MM-DD}/` 或 `{起始日}_{N}d/`
  - **保留原始檔名**（不重命名）→ RAW+JPEG 自然同 stem / original filenames kept, pairs share stem
  - `Masters` = known_cameras 內的相機；`Others` = 其他；`NoDate/` = 連 mtime 退路都取不到日期
  - 同名碰撞由 executor 處理（加 `_conflict_N` 並寫 WARN 到 run_log）/ collisions logged + suffixed
- **日期退路 / Date fallback**：無 EXIF 日期時，改用檔案系統 `mtime` 當日期（相片與影片皆適用），減少進 `NoDate/` 的數量；mtime 可能因複製而失準，故 plan 會寫 WARN 到 run_log。仍無日期才進 `NoDate/`。
  / when a file has no EXIF date, fall back to filesystem mtime (logged as WARN); only truly date-less files go to `NoDate/`
- **無 event 資料夾清單 / No-event folder list**：`plan` 會列出**名稱只是日期或流水號**的來源資料夾（空白、`2023-06-15`、`20230615`、純數字、`100CANON`、`DCIM`、`IMG_1234` 等相機傾倒夾）——多半是當初沒整理的——並寫進 `run_log`（`phase='review'`）。有真正事件名的（如 `Kyoto`、`京都`、`2023 Summer`）不列入。日後查：
  `SELECT path, message FROM run_log WHERE phase='review' AND message LIKE 'No-event%';`

## 影片支援 / Video support

- **掃描 / Scan**：mp4/mov/m4v/avi/mkv/wmv/flv/webm/mts/m2ts/3gp/3g2/mpg/mpeg → `file_type = VIDEO`
- **整理目錄（與照片分開）/ Output tree (separate from photos)**：
  `{target}/Videos/{YYYY}/{YYYY-MM-DD}_{event}_{seq:04d}.EXT`
  - `{event}` = 來源檔案的**父資料夾名**（清理後）；取不到時省略該段
    / sanitized parent folder name; omitted when unusable
  - 無 EXIF 日期 → `Videos/NoDate/` / no date → `Videos/NoDate/`
- **日期來源 / Date source**：優先 `CreationDate`（Apple，含時區）→ `CreateDate` → `MediaCreateDate`
- **中繼資料 / Metadata**：`duration_seconds` / `video_codec` / `frame_rate`（report 會顯示）
- **去重 / Dedup**：影片只做 **exact SHA-256**；near-dupe（pHash）為影像專用，影片不適用
  （未來可用 ffmpeg 抽 keyframe / future: ffmpeg keyframe extraction）

## 重要 schema 欄位（`files` 表）

- `file_type`：`RAW | CAMERA_JPEG | DEV_JPEG | RESIZED_JPEG | HEIC | VIDEO | UNKNOWN`
  - `DEV_JPEG` = RAW 沖出 / 軟體編輯過的 JPEG（用 Software 標籤辨識:Lightroom、Capture One、darktable、Photoshop…）；搬移目的地同 CAMERA_JPEG（Masters/Others），只是分類分開好檢視
    / developed-from-RAW or edited JPEG; same destination as CAMERA_JPEG, separate label for reporting
  - `HEIC` = iPhone 預設格式（含 heic/heif）；有完整 EXIF（日期/機型），**比照相片流程**整理進 Masters/Others（不是留在原地）
    / iPhone default (heic/heif); full EXIF, organized like a photo into Masters/Others
- `rating`：INTEGER 0–5（NULL = 未評分）
- `keywords`：JSON 陣列字串
- `description`：文字描述
- `label`：XMP 顏色標籤
- `duration_seconds` / `video_codec` / `frame_rate`：影片專屬（影像為 NULL）
- `phash`：感知雜湊，存成 **16 字元 hex TEXT**（`str(imagehash.phash(img))` 原生輸出）。
  早期版本存成 signed INTEGER（unsigned 64-bit 會溢位 SQLite 的 signed INTEGER，故當時用位元重解法繞過）。
  / perceptual hash stored as 16-char hex TEXT; older DBs used a signed INTEGER workaround.
  - **舊 DB 遷移 / Migrating an existing DB**：擇一 / pick one —
    1. `python -m photo_organizer dedup --force --db C:\photos.db` → 直接重算（讀碟）/ re-hash from disk, or
    2. `python scripts\migrate_phash_to_hex.py C:\photos.db` → 純改 DB，把舊的 signed INTEGER 還原為 unsigned 並轉成 `%016x`（idempotent，不讀碟）/ DB-only, converts signed INTEGER back to unsigned hex, no disk read.

## 完整使用流程

```bat
python -m photo_organizer scan    E:\Photos --db C:\photos.db
python -m photo_organizer report  --db C:\photos.db
python -m photo_organizer dedup   --db C:\photos.db
python -m photo_organizer review  --db C:\photos.db        # near-dupe 審查（可選）
python -m photo_organizer plan    --db C:\photos.db --target E:\Organised
python -m photo_organizer execute --db C:\photos.db
```

- **near-dupe review 與 plan 的關係 / review × plan**：`review` 只把決策寫進 `duplicates` 表（`status='reviewed'` + `keep_file_id`），**不直接建搬移操作**。實際的 `STAGE_DELETE` 由 `plan` 讀取這些決策後建立 → 因此**請在 `plan` 之前跑 `review`**（或事後重跑 `plan`）。如此 `plan --force` 重建計畫時也不會清掉人工審查結果，敗者也不會同時被建 MOVE。
  / `review` records decisions only; `plan` creates the STAGE_DELETE for each loser. Run `review` before `plan` (or re-run `plan`). `plan --force` no longer wipes review decisions.

### 無相機型號分佈 / Unknown-camera distribution（只算 DB / DB-only）

```bat
python -m photo_organizer unknown-cameras --db C:\photos.db
```
- 列出 `camera_model` 為空的檔案分佈:依**類型 / 年份 / 廠牌 / 軟體 / 來源資料夾**
- 純算 DB、不讀碟、不動檔案;這些檔案會進 `Others/`(除非把型號加進 known_cameras)

### 重新分類 / Reclassify（只算 DB,不讀碟 / DB-only, no disk read）

```bat
python -m photo_organizer reclassify --db C:\photos.db
```
- 用掃描時已存的 `software`/`width` 重跑 classify,只處理 `CAMERA_JPEG`/`DEV_JPEG` 列
- **零磁碟讀取、零 ExifTool** → 數十萬列僅數秒~數十秒(原 scan 是數十分鐘~數小時)
- 套用 DEV_JPEG 分類時不必整碟重掃;idempotent,可重複跑
- 沿用 `use_secondary` 設定(預設關),可加 `--secondary`

### 選擇性搬移 / Selective execute（可分批,未搬的留待下次 / batch by filter, rest stays confirmed）

```bat
python -m photo_organizer execute --db C:\photos.db --year 2023
python -m photo_organizer execute --db C:\photos.db --camera ILCE-7RM2
python -m photo_organizer execute --db C:\photos.db --software Lightroom
python -m photo_organizer execute --db C:\photos.db --type DEV_JPEG
```
- 過濾條件可單獨或組合;只搬符合的,其餘維持 `confirmed`,之後再跑 `execute` 接續
- 全部搬完前 `execute` 階段不標記 complete → 可重複跑不需 `--force`

### 回復 / Undo（把已搬的檔案搬回原位 / move everything back）

```bat
python -m photo_organizer undo --db C:\photos.db
```
- 依 `operations`（`source_path`）+ `files.path`（現位置,含 `_conflict_N`）逆向 `os.rename` 還原
- **絕不覆蓋**:原位置若已被占用則跳過並寫 `run_log`(`phase='undo'`)
- 還原成功後 `execute` 階段重置為 `pending`,可重新 plan/execute

## 注意

- `--target` 必須與照片來源在同一碟機（`os.rename()` 不跨碟）
- ExifTool for Windows：https://exiftool.org → 重新命名為 `exiftool.exe` 加入 PATH
