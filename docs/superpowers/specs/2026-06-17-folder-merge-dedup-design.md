# S1 — 資料夾層級去重與合併輔助 / Folder-level dedup & merge-assist

**狀態 / Status**: 設計定案,待寫實作計畫 / Design approved, pending implementation plan
**日期 / Date**: 2026-06-17
**範疇 / Scope**: 三個重設計子系統中的第一個(S1 → S2 → S3)。本文件只涵蓋 **S1**。

---

## 1. 動機 / Motivation

15 年的照片庫裡最常見的不是「單張重複」,而是**整個資料夾被原封不動拷貝**到別的位置。
例:`D:\DCIM_Storage\Depository_aged\Album` 與
`D:\DCIM_Storage\All In Lightroom\Depository_aged\Album` —— 同一個子樹被複製了一份。

現有去重是**檔案為中心**的(`deduper.py` Pass A exact SHA-256、Pass B near pHash;
`planner.py` redundant-copy union-find)。它會把這種整夾拷貝攤成「幾百筆零散的 EXACT
配對」,使用者看不出「這整個資料夾是那個資料夾的拷貝」這個高層級事實,也無從一次決策。

S1 引入**資料夾層級**的重複偵測,並且——依使用者決策——不是「判定多餘就刪」,而是
**提供方便比對的 UI,讓使用者決定 merge 方向**(把這當成「合併輔助工具」,不是「刪除工具」)。

---

## 2. 確認的設計決策 / Confirmed decisions

| # | 決策點 | 選擇 |
|---|--------|------|
| D1 | 檔案相同的比對訊號 | **只用 exact SHA-256**。near/重新編碼的情況留給 S2 成因分析。 |
| D2 | 重疊判定門檻 | **Twin / 雙向覆蓋率**:`min(|A∩B|/|A|, |A∩B|/|B|) ≥ 門檻`(預設 95%)。兩夾必須**基本是同一個 SHA 集合**才算重複(整夾拷貝 / 兩串很接近的粽子)。**見下方 §2.1 — spike 實測修正了原本的單向「含包」語意。** |
| D3 | 資料夾粒度 | **每一層都算(父夾 = 子夾 SHA 聯集),rollup 到「兩側仍是 twin」的最高層**(整子樹拷貝收斂成一筆;一旦正版那層多出對方沒有的內容,就停在仍為 twin 的那一層)。 |
| D4 | 偵測後的動作 | **做出方便比對的 UI**,讓使用者最終決定 **merge 方向**(非自動刪除)。 |
| D5 | 非保留方獨有檔的處理 | **折進保留方**;只有「共有(SHA 相同)」的那份才 `STAGE_DELETE`。結果是一個不漏檔的合併資料夾。 |
| D6 | 「折進」發生的層次 | **邏輯折入**:A 的獨有檔歸到 B 的事件,最後一起被組織進同一個 `Masters/{event}`(避開跨碟 `os.rename`)。 |
| D7 | UI 形式 | **沿用現有 `review --web` 相片比對工具**,新增「資料夾比對」模式。 |
| D8 | 手動搬移後的路徑修復 | **新增 `relocate` 前置步驟**:使用者已手動搬移一小部分資料夾,`files.path` 變 stale 會讓 execute 失敗。用 **SHA-256 重新定位**(內容不變、雜湊不變)更新 `path`,**保留 `file_id` 與全部決策**,不重建 DB。見 §3.5。 |

---

## 2.1 Spike 實測修正(2026-06-17,28 萬張真實庫驗證)

唯讀 spike(`scripts/spike_folder_overlaps.py`)對 282,976 張的真實庫驗證偵測邏輯,
修正了一個原始 D2 的設計缺陷:

- **原始 D2「單向含包」(`max(cov_a, cov_b) ≥ 95%`)會爆炸**:任何小資料夾的檔案
  「剛好都在某個大樹裡」就成立(如 `Raw_Files\2006` 99% 落在 `DCIM_Storage` 內),
  於是被 flag against 那棵樹**及其每一層祖先** → 實測 3,208 組、爬滿祖先階梯,全是雜訊。
- **改用 Twin「雙向覆蓋率」(`min(cov_a, cov_b) ≥ 95%`)後乾淨**:實測 **903 組 →
  subtree-union rollup 後 495 組**;樹自動收斂到「兩側仍是同一集合」的最高層
  (`All In Lightroom\…\Album\2003` ↔ `Depository_aged\Album\2003`),keeper 判定正確
  (fold 掉 `All In Lightroom`/`RawTank`/`RAW.old`,保留 `DCIM_Working`/`Depository_aged`)。
- **Twin 漏掉的「小夾完整埋在大傾倒夾」並未真的漏**:那些檔案在**檔案級 exact 去重**
  已被當 EXACT 重複抓到並 stage;資料夾級只負責「整夾雙胞胎」的高層視圖。
- **跨名稱雙胞胎**正確浮現(SHA 不看名字):`Yokohama`↔`橫濱`、`Angkor`↔`吳哥窟`、
  `Sweden_Fx5`↔`Sweden_Tx5`、`Photos\2009`↔`婚照片` —— 這也預告 S3(名稱/日期不符)。
- **keeper 啟發式**(spike 驗證可用):懲罰路徑中含 `All In Lightroom`/`depository`/
  `rawtank`/`jpegtank`/`raw.old`/`.out`/`待整理`/`- copy`/`backup`/`temp`/`.old`/`.bak`
  等非典藏標記的那一側,取標記較少者為 keeper;同分再比路徑長度。

`scripts/spike_folder_overlaps.py` 是**唯讀驗證腳本**(不建表、不動檔、不改 DB),
保留供日後重跑;正式實作會把同一套邏輯做進 `folder-merge` pass。

---

## 3. 架構 / Architecture

S1 嚴格遵守專案既有的「**決策層 / 執行層分離**」接縫:偵測與使用者決策只寫進 DB,
實際搬移/刪除一律由 `plan` → `execute` 產生與落地。

```
relocate       ── (前置,必要時)手動搬移後用 sha256 重新定位 stale 路徑,保留決策
        │
        ▼
dedup (Pass A, sha256 就緒)
        │
        ▼
folder-merge   ── 純算 DB 的新分析 pass:建反向索引、算覆蓋率、寫 folder_overlaps
        │
        ▼
review --web --folders ── 現有 web review 的新模式:並排比對,寫 keeper 決策
        │
        ▼
plan           ── 讀已 review 的 folder_overlaps:共有檔→STAGE_DELETE;獨有檔→邏輯歸到 keeper 事件
        │
        ▼
execute        ── 落地(MOVE / STAGE_DELETE),最終成一個合併好的事件
```

### 3.1 偵測演算法 — 反向索引(避免 O(n²) 兩兩比對)

1. 前置條件:`dedup` Pass A 完成,所有非 error 檔有 `sha256`。
2. 建反向索引 `sha256 → {含此 sha 的資料夾路徑集合}`(純查 DB,不讀碟)。
3. 每個資料夾的 **SHA 指紋集合** = 其直接檔案 + 所有子孫檔案的 sha 聯集(對應 D3「每層都算」)。
   往上累加時**停在 scan root**(不越過來源輸入根,以免在碟根把無關的樹併在一起)。
4. **候選配對**只來自反向索引中「共享 ≥ `MIN_SHARED` 個 sha」的資料夾對 —— 零交集的配對
   永遠不進入計算,避開 O(folders²)。**跳過同樹的祖先/子孫配對**(一方是另一方路徑前綴)
   —— 那是單樹內的含包,不是重複。
5. 對候選配對算覆蓋率;`coverage_a = |A∩B| / |A|`、`coverage_b = |A∩B| / |B|`。
   **`min(coverage_a, coverage_b) ≥ COVERAGE_THRESHOLD`(預設 0.95,Twin 語意)**
   → 記成一筆 `folder_overlaps`。(單向含包**不**列入——見 §2.1。)
6. **rollup 到最高 twin 層**:lockstep 往上走雙方祖先,若某層祖先配對也是 flagged twin →
   壓掉子層那筆,只留最高層(整子樹拷貝收斂成一筆;一旦正版那層多出對方沒有的內容、
   不再是 twin,就停在仍為 twin 的那層)。實測 903 → 495。

### 3.2 資料模型 — 新表 `folder_overlaps`(schema v3 → v4)

仿 `duplicates` 表風格。遷移用既有的 idempotent `ALTER`/建表模式
(見 `db.py` `_apply_migrations` / `_migrate_filetype_check` 的 table-rebuild 範例)。

| 欄位 | 型別 | 說明 |
|------|------|------|
| `overlap_id` | INTEGER PK | AUTOINCREMENT |
| `folder_a` | TEXT | 正規化資料夾路徑(`_norm_path`) |
| `folder_b` | TEXT | 正規化資料夾路徑;規範化使 a < b 以去重 |
| `shared_count` | INTEGER | 共有檔數(SHA 相同) |
| `a_only_count` | INTEGER | A 獨有檔數 |
| `b_only_count` | INTEGER | B 獨有檔數 |
| `coverage_a` | REAL | `shared / |A|` |
| `coverage_b` | REAL | `shared / |B|` |
| `keeper` | TEXT | 使用者決策:`'a'` / `'b'` / NULL(未決) |
| `status` | TEXT CHECK | `'pending'` / `'reviewed'` |
| `reviewed_at` | TEXT | 決策時間戳 |
| | | `UNIQUE(folder_a, folder_b)` |

`schema_version` 升到 **4**;舊 DB 開啟時自動建此表(idempotent)。

### 3.3 比對 UI — 擴充 `webreview.py`

沿用現有本機 stdlib server(綁 127.0.0.1、縮圖快取 `_staging/.thumbs/{file_id}.jpg`、
Pillow 邊緣方差銳利度)。新增「資料夾比對」模式:

- 逐組呈現 `folder_overlaps` 中 `status='pending'` 的配對。
- **左右並排兩欄(A | B)**,分三堆:
  - **共有(SHA 相同)**:預設摺疊/淡化——merge 後只留保留方那份。
  - **只在 A** / **只在 B**:出縮圖 + 解析度 / 檔案大小 / 相機 / 路徑。
- **保留方向切換**:`[保留 A ← 把 B 折進來]` / `[保留 B ← 把 A 折進來]`;預設幫使用者
  選好一邊,可一鍵翻轉。
- **預設 keeper 啟發式**:優先選**不在**備份/容器型父夾下的那個(另一邊在
  `All In Lightroom\`、`backup\`、`temp\` 或 `planner._CONTAINER_NAMES` 內 → 當非保留方)、
  路徑較短、位置較「主」。
- **頂部統計**:覆蓋率 %、共有 / A 獨有 / B 獨有 檔數;若某夾有未雜湊檔則標警示。
- `POST /folder-decision` → 寫 `folder_overlaps.keeper` + `status='reviewed'`。

TUI 路徑與現有相片/連拍審查**完全不動**,這只是多一個前端模式。

### 3.4 merge 決策 → plan → execute

`plan` 讀取 `status='reviewed'` 的 `folder_overlaps`。每筆(假設 keeper = B):

- **非保留方 A 的「共有檔」**(sha 在 B 中存在)→ `STAGE_DELETE`(B 已有同一份)。
- **非保留方 A 的「獨有檔」** → **邏輯折入**:plan 解析事件時,若某檔來源夾是某筆已 review
  配對的**非保留方** → 改用**保留方資料夾**去跑 `_resolve_event_folder`。如此 A 的獨有檔
  與 B 解析到同一事件,最終一起被組織進同一個 `Masters/{event}`。
- 之後照常 `execute`。

### 3.5 `relocate` — 手動搬移後用 SHA-256 重新定位(前置步驟)

**問題**:使用者已手動搬移一小部分來源資料夾,DB 裡那些 row 的 `path` 變成 stale。
executor 是讀 `files.path` 去 `os.rename`,來源不在 → execute 對那些 row 失敗。
`reconcile --verify-disk` 只能**偵測**(標 LOST FILE),不能**修復**。

**做法**(不重建 DB,保留決策帳本):

1. 增量重掃來源樹(scanner 既有的「跳過已掃路徑」行為,只看新位置)。
2. 找出 DB 中 `path` 在磁碟上已不存在的 row(stale rows)。
3. 對新發現的路徑算 SHA-256,用 **sha256 比對**把 stale row 對應到它的新位置
   (內容不變 → 雜湊不變,這是可靠的身分證)。
4. **只 UPDATE `path`(必要時連 `mtime`)**;`file_id` 與所有 `duplicates` / `operations` /
   `date_source` / review 決策原封不動。
5. 寫進 `run_log`(`phase='relocate'`):重新定位幾筆、仍找不到幾筆(真正遺失)。

**為何不重建**:28 萬張重 scan+hash+dedup 要數小時,且 DB 是「整理決策的永久帳本」
(去重勝敗、近似審查、日期鑑識、資料夾 merge 的 keeper 決策),重建即全部歸零。
`relocate` 只碰被搬的那一小撮,秒~分鐘級,決策完整保留。

**邊界**:
- 同一個 sha256 有多個 stale row(內容相同的多份)→ 用「原 path 與新 path 的最長共同路徑
  / 檔名」做最佳配對,避免張冠李戴;無法明確配對者留 LOST 不亂猜。
- 新位置出現「DB 沒見過的全新檔」→ 那是真的新檔,交給既有增量 `add`,不屬 relocate。
- relocate **不移動、不刪除任何檔**,只更新 DB 的 `path`。

---

## 4. 邊界與地雷 / Edge cases & landmines

- **多方覆蓋**:A 的檔散在 B 與 C 之間 → 只呈現覆蓋率最高的那組,UI 註記「另有其他來源」。
- **RAW / VIDEO**:exact SHA 涵蓋所有型別,整夾 RAW 拷貝也抓得到;但 RAW 主檔的刪除一樣
  只在「共有」且保留方確有同一 SHA 時才 stage。
- **鏈式覆寫**:A→B、B→C 的傳遞,plan 解析事件時須**收斂到最終 keeper**(避免折一半)。
- **安全網**:沿用「唯一檔永不刪」鐵則——只有「保留方確實存在同一個 SHA」的共有檔才會被
  stage;A 的獨有檔永遠不會被刪,只會被邏輯折入。
- **NULL sha256 / status='error'**:跳過未雜湊或錯誤檔;若某夾有 N 個未雜湊檔,UI 標示
  「覆蓋率可能低估」。`folder-merge` 必須在 `dedup` Pass A 之後跑。
- **正規化**:`folder_a`/`folder_b` 用 `planner._norm_path` 正規化,與既有 `scan_roots`
  比對一致(大小寫 / 斜線)。

---

## 5. 不在 S1 範疇 / Out of scope

- near(pHash)層級的整夾比對(重新編碼的拷貝)→ 留給 **S2**(exact 重複成因分析)。
- 事件日期不符、事件夾誤嵌、quarantine 隔離清單 → **S3**(資料夾結構診斷)。
- 自動決定 merge 方向(無人工)→ 本設計刻意要求人工經 UI 決策。

---

## 6. 測試策略(概要,細節留待實作計畫)

- **relocate**:DB 級測試——搬移後 stale row 用 sha256 重新定位、`file_id`/決策保留、
  多份相同 sha 的最佳配對、真正遺失者標 LOST、relocate 不動磁碟檔。
- **偵測**:純函式 / DB 級測試——反向索引、Twin 雙向覆蓋率、subtree 聯集、最高 twin 層
  rollup、`MIN_SHARED` 門檻、同樹祖孫配對排除、ubiquitous-sha 排除、NULL sha256 跳過。
- **決策→plan**:keeper 決策正確產生 STAGE_DELETE(共有)+ 邏輯折入(獨有歸到 keeper 事件);
  唯一檔不被刪;安全網。
- **UI**:`POST /folder-decision` 寫入正確;預設 keeper 啟發式;TUI 路徑非回歸。
- 全套件維持綠燈(目前 156 passed)。
