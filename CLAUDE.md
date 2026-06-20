# Photo Organizer

讀 SKILL.md 了解完整需求和架構。

## Session 工作流程 / Session workflow

本專案的工作刻意拆成兩個獨立 session,各司其職:

- **「SCM GitHub」session** — 只負責版本控制／GitHub 操作:`git`、`gh`、commit、push、PR、merge、分支清理。所有這類動作都**移到該 session 執行**(可由本 session 透過 subagent 代為觸發,但決策歸 SCM session)。
- **「Run Phase commands」session** — 只負責跑 pipeline 指令:`scan` / `report` / `dedup` / `review` / `plan` / `execute` / `add` / `clone` 等,以及實際庫的整理動作。**執行相關的動作留在這個 session。**

**絕對規則 / Hard rule:**

> **🚫 永遠不要自動 archive 任何 session。** 即使任務看似完成、session 看似閒置,也**絕不**主動呼叫 archive(`mcp__ccd_session_mgmt__archive_session` 或任何等效操作)。Archive 只能由使用者**明確指示**時才執行。
> / **NEVER auto-archive a session.** Even when a task looks done or a session looks idle, do not call archive on your own initiative. Archive only on the user's explicit instruction.

## 實作狀態

| Phase | 模組 | 狀態 |
|-------|------|------|
| 1 — Scan & Index | `scanner.py` `exiftool.py` `classifier.py` | ✅ 完成 |
| 2 — Scan Report  | `reporter.py` | ✅ 完成 |
| 3 — Dedup        | `deduper.py` | ✅ 完成 |
| 3B review        | `reviewer.py` | ✅ 完成 |
| 4 — Plan         | `planner.py` | ✅ 完成 |
| 5 — Execute      | `executor.py` | ✅ 完成 |
| 增量維護 / Incremental add | `adder.py` | ✅ 完成 |

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
- **整理目錄（與事件共置）/ Output tree (co-located with the event)**：影片放進**該事件資料夾的 `Videos/` 子夾**,和同一場合的照片在一起(不再丟到全域 `Videos/` 樹散落)。
  / a video lands in a `Videos/` subfolder of the SAME event folder its photos use, instead of a flat global tree.
  - 單日 / single-day：`{base}/{YYYY}/{YYYY-MM-DD}_{event}/Videos/{YYYY-MM-DD}_{seq:04d}.EXT`
  - 多日 / multi-day：`{base}/{起始YYYY}/{起始}_{N}d_{event}/Videos/{日期}_{seq:04d}.EXT`
  - 主題 / subject collection：`{base}/{event}/{YYYY}/Videos/{日期}_{seq:04d}.EXT`
  - 無事件名的相機傾倒夾(resolve 不到事件)但有日期 → `{base}/{YYYY}/{YYYY-MM-DD}/Videos/...`
  - **`base`(Masters vs Others)= 規則 C**:該事件只要有**任一張已知相機照片** → `Masters`,否則 `Others`(`_event_base_map`);resolve 不到事件時改依**影片自身相機**。如此影片永遠和該事件「大宗」照片同一棵樹。/ base follows the event's photos (any known-camera photo → Masters, else Others); falls back to the video's own camera when no event resolves.
  - 無日期 / no date → 維持獨立 `Videos/NoDate/video_{seq:04d}.EXT`(無日期無法共置)。
- **影片日期 / Video date**:96% 老影片無嵌入拍攝時間。優先序 **人工 override > 影片檔名日期(自身,視為 MEDIUM)> 借同夾照片日期(`_sibling_date_hints`)> mtime**。借日期只填仍 LOW/無日期的影片,HIGH/MEDIUM 真日期永不被覆寫。/ filename date trusted (MEDIUM), else borrow the folder's photo date; never overrides HIGH/MEDIUM.
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
- `date_source`：日期最終取自哪個訊號 / which signal the date came from
  ∈ `exif_original | filename | exif_digitized | mtime | none`
- `date_confidence`：該日期的可信度 / confidence in that date ∈ `HIGH | MEDIUM | LOW`（無日期時為 NULL）
- `phash`：感知雜湊，存成 **16 字元 hex TEXT**（`str(imagehash.phash(img))` 原生輸出）。
  早期版本存成 signed INTEGER（unsigned 64-bit 會溢位 SQLite 的 signed INTEGER，故當時用位元重解法繞過）。
  / perceptual hash stored as 16-char hex TEXT; older DBs used a signed INTEGER workaround.
  - **舊 DB 遷移 / Migrating an existing DB**：擇一 / pick one —
    1. `python -m photo_organizer dedup --force --db C:\photos.db` → 直接重算（讀碟）/ re-hash from disk, or
    2. `python scripts\migrate_phash_to_hex.py C:\photos.db` → 純改 DB，把舊的 signed INTEGER 還原為 unsigned 並轉成 `%016x`（idempotent，不讀碟）/ DB-only, converts signed INTEGER back to unsigned hex, no disk read.

## 資料庫即決策紀錄 / The DB is your decision record

**這個 DB 不是可丟棄的暫存檔——它是整個整理過程所有決策的「永久帳本」**：每個檔案的日期判定、
去重的勝/敗、近似重複的人工審查、每一筆搬移操作與其結果都存在裡面。**備份這個 DB＝備份你的整理決策。**
弄丟了就只能對著已整理好的資料夾重新猜當初為什麼這樣分。
/ The DB is the durable record of every organizing decision — date forensics, dedup
winners/losers, near-dupe review choices, and every move operation with its outcome.
**Backing it up = backing up your organizing decisions.** Lose it and you're reverse-engineering
your own library.

- **預設位置跟著照片庫走 / Default location lives with the library**：有 `--target` 但**沒給** `--db` 時，
  DB 預設落在 `{target}/.photo_organizer/library.db`，跟它描述的照片庫放一起。
  **明確給 `--db` 永遠優先**；config 裡的 `db` 也算明確指定。既有的 `--db` 用法完全不受影響。
  / when a `--target` exists and no `--db` is passed, the DB defaults to
  `{target}/.photo_organizer/library.db`. Explicit `--db` (and a config `db`) always wins.
- **版本守門 / Schema-version guard**：DB 存了一個 `schema_version`（`meta` 表）。較舊的 DB 開啟時會自動跑
  idempotent 遷移並把版本標記補上；若 DB 是**比目前程式還新**的版本寫的，會**拒絕開啟**以免破壞看不懂的資料。
  / a `schema_version` marker (in the `meta` table) lets an older DB migrate automatically and
  makes a newer-than-supported DB refuse to open, protecting data the build can't fully interpret.

## 完整使用流程

```bat
python -m photo_organizer scan    E:\Photos --db C:\photos.db
python -m photo_organizer report  --db C:\photos.db
python -m photo_organizer dedup   --db C:\photos.db
python -m photo_organizer review  --db C:\photos.db        # near-dupe 審查（可選）
python -m photo_organizer plan    --db C:\photos.db --target E:\Organised
python -m photo_organizer execute --db C:\photos.db
```

> DB 路徑也可省略 `--db` 改靠 `--target` 預設：`plan --target E:\Organised` → DB 落在
> `E:\Organised\.photo_organizer\library.db`。
> / `--db` may be omitted when `--target` is given; the DB defaults under the library.

- **near-dupe review 與 plan 的關係 / review × plan**：`review` 只把決策寫進 `duplicates` 表（`status='reviewed'` + `keep_file_id`），**不直接建搬移操作**。實際的 `STAGE_DELETE` 由 `plan` 讀取這些決策後建立 → 因此**請在 `plan` 之前跑 `review`**（或事後重跑 `plan`）。如此 `plan --force` 重建計畫時也不會清掉人工審查結果，敗者也不會同時被建 MOVE。
  / `review` records decisions only; `plan` creates the STAGE_DELETE for each loser. Run `review` before `plan` (or re-run `plan`). `plan --force` no longer wipes review decisions.

- **重複副本自動清除 / Redundant-copy auto-staging**（`planner.redundant_copy_ids`）：同一張照片常有多個版本——重新存過的 JPEG（**位元組不同** EXACT SHA-256 抓不到）、分享匯出、縮圖（**改了檔名**或縮太多 **pHash 漂走** near 也配不上）→ 漏網污染 review。`plan` 用 **union-find** 把「同一張」的所有副本連成一個連通分量，每個分量**只留 keep_score 最佳版**、其餘全標 `STAGE_DELETE`（預覽列「Redundant copies (re-encodes + resizes)」）。兩種「同一張」連結各自安全：
  - **連結 1 — 同檔名 + 同時間 + 同長寬比**：同（檔名 stem + EXIF `datetime_original`）且長寬比相符（容差 `_RESIZE_ASPECT_TOL`）→ 同一張相機影格被重存／縮放（**任何尺寸**,連 pHash 漂走的小縮圖也算）。長寬比不同（**裁切**或旋轉）**不連** → 保留。
  - **連結 2 — 同時間 + 同 pHash（非垃圾）**：認出**改名的副本**（如 `image00017.jpg`）。但**只連「非相機原始檔名」的匯出副本**到該張的最佳相機原檔；**兩個不同的相機原始檔名**（`IMG_9606` vs `IMG_9607`，`_is_camera_original_name`）是相機各自配號的**不同影格**，即使 pHash 完全相同（Hamming 0）也**不連** → 連拍兩格都保留、留進 near review。（相機每按一次快門配一個流水號,故不同號=不同張;同號的副本走連結 1 的同 stem。）
  - **資料夾規則 — 衍生匯出夾**：位於 `share`／`resize`／`resize+crop`／`export`／`web`／`thumb`… 等衍生夾（token 比對，`Jpeg` 不算）的 JPEG，只要**同事件**裡有同檔名主檔存在 → 直接收。縮小+裁切的衍生檔會讓 **pHash 失效（不同張縮小後會撞同一個雜湊）**、EXIF 時間也常被剝掉，故改用**資料夾名稱**這個可靠訊號;主檔（與 RAW）在別處保住內容。
  - **為何不再用「誰最佳」的成對比較**：改用「依『張』分群」後沒有 keeper-vs-keeper 衝突——每個分量固定留一個存活者，所以**有更大版本存在時，小縮圖一定會被收**（修掉早期 `protected` 防護把漂走縮圖誤救回的 bug）。
  - **鐵則**：唯一的一張（單一分量）永不刪。**RAW／VIDEO 不在範圍**，RAW 主檔永遠保留。無 `datetime_original` 不比對。**真正的連拍**（不同相機原始檔名的連續影格,**無論 pHash 是否相同**）**不會被連** → 留在 near review。
  - **垃圾雜湊排除 / junk-phash excluded**：同一 pHash 被 ≥`JUNK_PHASH_MIN_FILES`（8，與 near 共用單一來源）個檔共用 → 不據以連結。
  - **安全網豁免 / safety-net exemption**：多餘副本的內容由**不同 sha 的更佳版**保住，故 `plan` 1d 安全網會**豁免** RESIZED／重複副本（否則單檔 sha 的副本會被誤救回）。也**排除出 near review** → 大幅減少人工審查。純算 DB、不讀碟、idempotent。
  / `plan` links every copy of one shot into a connected component (union-find) and keeps only the keep_score best of each, staging the rest. Two safe edges: (1) same (filename stem + EXIF capture time) with matching aspect ratio — the same frame re-saved/downscaled at any size, even a pHash-drifted thumbnail; a different aspect (crop/rotation) is NOT linked, so crops are kept; (2) same (capture time + identical non-junk pHash) — catches renamed exports (image00017.jpg). Plus a folder rule: a JPEG in a derivative-export folder (share/resize/crop/…; "Jpeg" excluded) is staged when a same-stem master exists in the same event — downscaled+cropped exports collide in pHash across different shots and often have their date stripped, so the folder name is the reliable signal. Grouping by shot (not by which copy is "best") removes the keeper-vs-keeper conflict, so a thumbnail is always staged when a larger sibling exists. Unique shots are never staged; RAW/VIDEO out of scope (RAW masters kept); genuine bursts (two different camera-original filenames are distinct shutter actuations even with an identical pHash) stay in near review — Edge 2 only links a non-camera-original export name to a shot, never camera-original ↔ camera-original. Exempt from the 1d byte-survival net and excluded from near review.

### 增量維護 / Incremental add（`add`）

照片庫整理好之後，下個月又拍了 800 張——丟進一個新資料夾，跑一次 `add` 就好：
新照片會**插進**已整理好的 30 萬張旁邊、對**整個照片庫**去重，**完全不動**已整理好的東西。

```bat
python -m photo_organizer add E:\Photos\2024-07 --target E:\Organised --db C:\photos.db
python -m photo_organizer add --config config.json            # 來源取自 input_dirs
python -m photo_organizer add E:\New --target E:\Organised --yes --no-execute
```

- **只處理新檔 / new files only**：沿用增量掃描（自動跳過已掃過的路徑）→ 算 SHA-256／pHash → 規劃 → 搬移。
- **對整庫去重 / deduped against the whole library**：
  - **完全相同（SHA-256）**：新檔的雜湊若已存在於**已整理**的檔案中 → 記成 EXACT 重複並**搬進 `_staging/to_delete/`**，不會放第二份。
  - **近似（pHash）**：每張新圖對「既有照片庫的 pHash 索引」查詢（優先用 `bktree.BKTree`，無此模組則退回暴力比對）→ 記成 NEAR 供人工審查，**不自動刪**，新檔照常放入。
- **凍結事件、絕不重排 / frozen events, never reshuffle**（最關鍵的不變量）：
  - 已搬移（`done`）的檔案是**不可變**的——永不再搬、其事件資料夾名是**既成事實**，不重算。
  - **不**重算整庫的事件跨度；新檔的跨度只在**新檔之間**計算。
  - 新檔落點規則：
    - 屬於某個**既有事件**（同來源資料夾血緣，或日期落在該事件既有日期範圍內）→ 放進那個事件資料夾，**沿用原資料夾名**（不改名）。
    - 否則 → 依現有規則（單日／多日）建**新**事件資料夾（只用新檔計算）。
  - 事件若合理延長（如 `_3d`→`_4d`）是否改名 → **超出範圍**；預設保留既成名稱，且永不重搬已放好的檔案。
- `--yes` 跳過確認；`--no-execute` 只建計畫不搬（之後再 `execute`）。
- DB 即決策紀錄，見上方〈資料庫即決策紀錄〉。
  / A photographer drops next month's folder and runs `add`: only the new files are scanned,
  deduped against the WHOLE library (exact → staged, near → recorded for review), and placed —
  into an existing event folder (by its frozen name) when lineage/date matches, else a new folder
  computed over the new files only. Already-`done` files are NEVER moved, renamed, or recomputed.

### HTML 隱形接觸表審查 / Contact-sheet review（`review --web`）

```bat
python -m photo_organizer review --web --all --db C:\photos.db   # 含連拍叢集 / include burst clusters
python -m photo_organizer review --web --port 8765 --db C:\photos.db
```
- 用 stdlib `http.server` 起一個**本機**伺服器（只綁 127.0.0.1，印出網址），把同一批叢集渲染成**一頁可捲動的縮圖接觸表**：每張候選圖顯示縮圖 + 解析度／檔案大小／相機／路徑 + 叢集 Hamming 距離。取代終端機逐張 alt-tab。
  / a tiny local stdlib server renders the SAME clusters as one scrollable thumbnail contact sheet — replaces the alt-tab-per-image TUI.
- **縮圖 / Thumbnails**：~256px，用 Pillow 產生，快取在 `{db 同目錄}/_staging/.thumbs/{file_id}.jpg`；惰性產生、只算審查子集，重跑即時。
  / cached by file_id under `_staging/.thumbs/`, generated lazily, instant on re-run.
- **對焦訊號 / Sharpness**：解碼縮圖時順手算一個 Pillow 邊緣方差銳利度（不引入 cv2）；用**叢集內相對**排名標出「⚠ soft focus」軟焦影格。
  / a no-new-dep PIL edge-variance sharpness proxy, flagged by WITHIN-cluster relative ranking.
- **預設選取 / Default selection**（只是建議，永不自動刪）：縮放／同名複本叢集 → 預選最高解析度；連拍叢集 → 預留所有對焦清楚的影格、只把軟焦的預標為刪。點縮圖可覆寫，每叢集有「keep all」鈕。
  / resized/copy clusters pre-keep the highest resolution; burst clusters pre-keep every in-focus frame and pre-drop only the soft ones. Click to override; per-cluster "keep all".
- **決策層不變 / Same decision seam**：`POST /decision` 仍走 `reviewer._record_decision`，只寫 `status='reviewed'` + `keep_file_id`；實際刪除一樣等 `plan` 建 `STAGE_DELETE`。TUI 審查完整保留，`--web` 只是多一個前端。
  / decisions still flow through the unchanged `_record_decision`; the TUI path is untouched.

### 異地備份 / Clone — verified incremental backup to another volume（`clone`）

整理好之後在**原碟內**就地完成（`os.rename`，瞬間）。`clone` 是**另一回事**：把已整理好的照片庫**複製**到**另一顆碟／NAS**，讓你有**兩份**（資料安全）。**只複製、絕不搬移或刪除來源**——這刻意取代了早期「整理時跨碟搬移」的想法。

```bat
python -m photo_organizer clone E:\Backup --target E:\Organised --db C:\photos.db
python -m photo_organizer clone \\NAS\photos --config config.json
python -m photo_organizer clone E:\Backup --target E:\Organised --verify-all   # 偏執:重算每個目的檔
python -m photo_organizer clone E:\Backup --target E:\Organised --prune         # 移除已不在庫內的目的檔
```

- **增量跳過（快）/ incremental skip**：目的地已有同檔（大小＋mtime 相符）→ 跳過，不重複、不重算雜湊。多 TB 照片庫的第二次備份只碰**有變動**的檔案。
- **可攜複製 / portable copy**：一律用 Python `shutil.copy2`（保留 mtime）——**單一跨平台路徑**，不外呼 robocopy/rsync（Windows-only / Unix-only 會變兩套後端、破壞跨平台）。
- **只驗新檔（預設）/ verify new copies only**：複製完讀回算 SHA-256，比對 DB 既存的 `files.sha256`（已知良好）。不符 → **重複製一次**，仍不符 → **大聲標記為備份錯誤**（不刪任何東西）。驗證是相對「笨複製」唯一的顯著成本（讀回 ≈ +50% I/O），故預設**只驗剛複製的檔**；`--verify-all` 才全庫重算。
- **自描述複本 / self-describing replica**：帳本 DB 一併複製到 `{dest}\.photo_organizer\{db 檔名}`，複本可獨立使用（備份照片＝備份整理決策）。
- **絕不刪來源 / never deletes the source**：清掉目的端「已不在庫內」的檔案是 `--prune` **明確開啟**才做，預設關，且**先警告＋列出**要刪什麼。預設是純加法鏡像。
- **可續傳 / resumable**：每檔先寫 `.tmp` 再原子改名就位，中斷的複製永不留下被誤認完整的截斷檔；殘留 `.tmp` 會被清掉。
- **完整性報告 / completeness report**：結尾印出資產負債表（庫內檔數／目的端現存／本次複製／已跳過／已驗證／錯誤／傳輸位元組），並寫進 `run_log`（`phase='clone'`）。錯誤或來源遺失時 exit code 非零。
  / After organizing in place on the source drive, `clone E:\Backup` makes a verified, self-describing second copy on another volume. Repeat runs copy only what's new and re-verify only new files (fast), proving the backup intact against the library's own known-good hashes. Copy-only — the source is never moved or deleted.

### 無相機型號分佈 / Unknown-camera distribution（只算 DB / DB-only）

```bat
python -m photo_organizer unknown-cameras --db C:\photos.db
```
- 列出 `camera_model` 為空的檔案分佈:依**類型 / 年份 / 廠牌 / 軟體 / 來源資料夾**
- 純算 DB、不讀碟、不動檔案;這些檔案會進 `Others/`(除非把型號加進 known_cameras)

### 執行時間統計 / Command timings（只算 DB / DB-only）

每個指令跑完都會在結尾印出牆鐘耗時（`⏱ dedup finished in 12m 34s`），並**記進 DB**
（`command_runs` 表，累積不覆蓋）。想知道哪一步最慢、上次跑多久、平均多久:
/ every command prints its wall-clock time when it finishes and records it in the DB;
`timings` shows the accumulated history so you know which step is slow and how long it usually takes.

```bat
python -m photo_organizer timings --db C:\photos.db
```
- 一張表列出每個指令的:**執行次數 / 上次 / 平均 / 最短 / 最長 / 上次執行時間**,依最近執行排序
  / a table of runs / last / avg / min / max per command, most-recent first
- 中斷（Ctrl-C）或出錯的執行也會記錄並標 `[interrupted]` / `[error]`,故耗時統計不會被漏記
  / interrupted or errored runs are still recorded (tagged), so timing history has no gaps
- `validate` 與 `timings` 本身不計入 / `validate` and `timings` itself are not recorded

### 重新分類 / Reclassify（只算 DB,不讀碟 / DB-only, no disk read）

```bat
python -m photo_organizer reclassify --db C:\photos.db
```
- 用掃描時已存的 `software`/`width` 重跑 classify,只處理 `CAMERA_JPEG`/`DEV_JPEG` 列
- **零磁碟讀取、零 ExifTool** → 數十萬列僅數秒~數十秒(原 scan 是數十分鐘~數小時)
- 套用 DEV_JPEG 分類時不必整碟重掃;idempotent,可重複跑
- 沿用 `use_secondary` 設定(預設關),可加 `--secondary`

### 日期鑑識 / Date forensics（只算 DB,不讀碟 / DB-only, no disk read）

15 年的照片庫裡 EXIF 日期常常不可靠:Android 截圖會塞**假的** DateTimeOriginal、
WhatsApp/Telegram 會**剝掉** EXIF(只剩不準的 mtime)、FAT32/exFAT 複製會把 mtime
重設成複製當天。`plan` 因此可能把 2015 的照片靜悄悄歸到 2023/ — 比 `NoDate/` 更糟,
因為使用者根本不知道要去查。日期鑑識用**已存在 DB 的訊號**交叉比對,給每個日期標上
可信度與來源,並把可疑的列出來讓使用者在 `execute` 前檢查。
/ In a 15-year library EXIF dates are often faked (Android screenshots), stripped
(WhatsApp/Telegram → unreliable mtime) or mtime is copy-reset. This cross-checks the
signals already in the DB and flags suspicious dates before a file is mis-filed.

```bat
python -m photo_organizer plan --dates-only --db C:\photos.db
```
- **只跑日期鑑識**:重算 `date_source` / `date_confidence`,不建計畫、不需 `--target`、不提示
  / audit only — repopulates the two columns, builds no plan, needs no target
- 一般的 `plan` 也會自動先跑一次鑑識 / a normal `plan` also runs this audit first
- 零磁碟讀取、零 ExifTool、idempotent 可重複跑(每次重跑會先清掉自己上次的 run_log 紀錄)
  / DB-only, idempotent (clears its own prior run_log entries before re-logging)

**解析階梯 / Resolution ladder**（先符合者勝 / first applicable wins）:
1. 有相機型號 + DateTimeOriginal 合理 → `exif_original`, **HIGH**(真相機不會偽造 DateTimeOriginal)
2. DateTimeOriginal 與**檔名日期**相差 ≤ ~1 天 → `exif_original`, **HIGH**
3. 檔名日期合理但與 DateTimeOriginal 相差 > ~2 天 → `filename`, **LOW**(EXIF 多半被偽造/剝除,改信拍攝 App 寫進檔名的日期)
4. DateTimeOriginal 合理但無佐證、無相機 → `exif_original`, **MEDIUM**
5. DateTimeDigitized 合理 → `exif_digitized`, **MEDIUM**
6. 退回 mtime → `mtime`, **LOW**(可能是複製日期)
7. 都沒有 → `none`(進 `NoDate/`)
- **合理性界線 / sanity bounds**(任一來源踩到都降為 LOW / 列入可疑):未來日期、年份 < 1990、
  已知假哨兵值 `1980-01-01`、`2000-01-01`。
- 檔名日期解析涵蓋:`IMG_YYYYMMDD[_HHMMSS]`、`PXL_YYYYMMDD_HHMMSSsss`、`VID_…`、
  `Screenshot_…`(Android)、`IMG-YYYYMMDD-WA####`(WhatsApp)、`Signal-YYYY-MM-DD-…`、
  裸 `YYYYMMDD_######`。

**可疑日期清單 / Suspicious-date list**(寫進 `run_log`,phase=`review`):
```sql
SELECT path, message FROM run_log WHERE phase='review' AND message LIKE 'Suspicious-date%';
```
每筆會列出競爭的候選日期,例如 / each entry shows the competing candidates, e.g.
`Suspicious-date [LOW] …/IMG_20150402_120000.jpg: EXIF=2023-06-15 filename=2015-04-02 → chose filename (2015-04-02)`

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
- **HEIC 近似去重需 `pillow-heif`**：iPhone `.heic`/`.heif` 的感知雜湊（pHash）需要 `pip install pillow-heif`，否則這些檔案在 `dedup` 的近似比對會逐張失敗（記成 pHash error，不會中斷整體流程）。掃描／搬移不受影響（日期與分類走 ExifTool）。/ HEIC perceptual hashing needs `pillow-heif`; without it, HEIC files fail per-file in `dedup` near-match (logged, non-fatal). Scan/move are unaffected (date & classify use ExifTool).
- **近似配對搜尋是平行的 / Near-dup pair search is parallel**：`dedup` 的 phase 3B 配對搜尋（BK-tree 查詢）為 CPU-bound，會用 `multiprocessing`（核心數−1 個 process）平行跑，對大型圖庫（十萬張級）是數量級的加速；與單執行緒結果**完全相同（無損）**。資料量小於門檻時自動退回單執行緒避免 spawn 開銷。`hamming_threshold`（config 或 `--hamming`）越低搜尋越快：`0–2` 幾乎相同、`3–4` 小編輯、`5–6` 中等相似、`7–8` 連拍/同場景。/ phase 3B pair search is parallelised across processes (lossless vs serial); lower `hamming_threshold` = faster and tighter matches.
- **近似審查叢集的三道過濾 / Near-review cluster filters**（`reviewer._build_near_clusters`）：`dedup` 記錄**所有**門檻內的近似配對，但 `review`／`review --web` 把配對組成「審查叢集」時會再過濾三層，避免叢集被雜訊汙染（曾出現 111 張不相干照片串成一叢、頭尾 pHash 差 36 bits）：
  1. **排除 EXACT 敗者**：位元組完全相同的複本（同 sha256）已由 `plan` stage，叢集只保留 `keep_score` 勝者，不重複列出、也不會與 EXACT 決策打架。
  2. **排除垃圾 pHash**：同一個 pHash 被 ≥`_JUNK_PHASH_MIN_FILES`（預設 8）個檔案共用 → 屬低資訊量影像（夜景／暗／平淡背景）的假碰撞（例：月亮 == 筆記本），整批排除。
  3. **緊門檻分群（`_CLUSTER_HAMMING`，預設 2）**：union-find 是單鏈接，會把 A~B~C…~Z 串起來；用比偵測門檻更緊的距離分群，才不會把不相干的 look-alike 連成巨叢。真正的連拍（同資料夾、秒級）仍會因每個**連結**都 ≤2 而正確聚在一起。
  / `dedup` records every pair within threshold, but `review` filters three ways when forming review clusters: (1) exclude EXACT-dupe losers (already staged by plan), (2) exclude junk pHashes shared by ≥8 files (low-information collisions), (3) group only on pairs ≤`_CLUSTER_HAMMING` so single-linkage union-find can't chain unrelated look-alikes into a giant blob.
- **知識庫 / Knowledge store**：`docs/solutions/` — 已記錄的 bugs、最佳實踐與架構決策（含 YAML frontmatter：`module`、`tags`、`problem_type`），實作或除錯時可參考。/ documented solutions organized by category with YAML frontmatter — relevant when implementing or debugging in documented areas.
- **領域詞彙 / Domain vocabulary**：`CONCEPTS.md` — 專案特有術語的精確定義，新進工程師入門或閱讀 docs/solutions/ 時可查。/ precise definitions for project-specific terms; consult when reading docs/solutions/ or onboarding.

## 已知限制 / Known Limitations

- **Terminal 進度條視覺混亂（調整視窗大小時）/ Progress bar display glitch on terminal resize**: Rich 進度條根據終端寬度佈局；改變視窗大小時會出現重影/混亂。進度條會自動恢復、數據完全準確、不影響實際運行。建議跑長期進程時不改窗口大小，或接受過渡期的視覺混亂。/ Rich progress bars reflow on terminal width changes, causing visual glitches during resize. Data and performance unaffected; bars self-recover. Avoid resizing during long operations, or tolerate transient display noise.
