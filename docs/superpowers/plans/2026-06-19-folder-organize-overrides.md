# Folder-organize overrides — 人工資料夾整理工具（事件名 + 日期覆寫）

## Context

去重後,`plan` 會留下兩類「歸檔品質」問題(非資料安全、皆可 `undo`):

1. **LOW 可信度日期**(存活 ~4,721,跨 173 個資料夾):EXIF 被偽造/剝除或 mtime 被複製重設 → 可能歸錯年份。
2. **無事件名資料夾**(~228 個):來源資料夾名只是日期/流水號/相機傾倒夾(`100EOS5D`、`DCIM`)→ 目標只有 `{年}/{日期}/`,沒有事件標籤。

兩者本質相同:**「這個資料夾是什麼?」的資料夾層級人工判斷**——看一眼縮圖同時就補好事件名與年份。聯集 **374 個資料夾**(review 後再縮小)。

**核心設計**:人工值存成 **override**,`plan` 讀取套用——**不改磁碟資料夾名、可重跑 plan、可 undo**。

**已確認決策**:
- UI = **網頁接觸表 + 內嵌輸入**(沿用 `webreview.py` 縮圖快取與 `folderreview.py` 分群/收合)。
- 日期覆寫 = **資料夾層級**(整夾一個日期,套到夾內 LOW/無日期檔)。

**範疇外**:逐檔日期覆寫、自動猜事件名、改磁碟資料夾名。

---

## O1 — `folder_overrides` 表 + DB helpers

**`photo_organizer/db.py`**

新增到 `SCHEMA_SQL`(都是 `CREATE TABLE IF NOT EXISTS`,既有 DB 下次開啟自動建):
```sql
CREATE TABLE IF NOT EXISTS folder_overrides (
    source_folder TEXT PRIMARY KEY,   -- 來源父資料夾絕對路徑
    event_name    TEXT,               -- 事件標籤覆寫(NULL=不覆寫)
    date_override TEXT,               -- 日期覆寫 'YYYY-MM-DD'(NULL=不覆寫)
    note          TEXT,
    updated_at    TEXT
);
```
`SCHEMA_VERSION` 4 → 5(既有 `_check_schema_version` 自動補標,無需 table-rebuild 遷移)。

DB helpers:
- `set_folder_override(source_folder, *, event_name, date_override, note, updated_at)` — UPSERT(`INSERT ... ON CONFLICT(source_folder) DO UPDATE`)。`event_name`/`date_override` 傳 None 即清該欄。
- `clear_folder_override(source_folder)` — DELETE;rowcount 0 → `KeyError`(比照 `record_folder_overlap_decision`)。
- `get_folder_overrides() -> dict[str, sqlite3.Row]` — 一次載入全部,key=source_folder(planner 與 UI 共用)。

**測試** `tests/test_folder_overrides_db.py`:set→get round-trip、UPSERT 覆蓋、clear、clear 未知 key raise KeyError、空表 get_folder_overrides 回 {}。

---

## O2 — planner 整合（事件名 + 日期覆寫）

**`photo_organizer/planner.py`** — `plan()` 開頭載入一次 `overrides = db.get_folder_overrides()`,傳進 `_build_target_path` 與 `_compute_event_groups`。

**事件名覆寫**(`_build_target_path`,照片/影片分支共用):
- 解析出檔案的來源父資料夾 `parent = str(Path(row["path"]).parent)`。
- 若 `overrides.get(parent)` 有非空 `event_name` → 直接當 `{event}` 標籤,**蓋過** `_sanitize_event(parent.name)` 與「無事件名」退路。
- subject/multi-day event_groups 邏輯不變;override 只在「決定 `{event}` 字串」這一步插隊。

**日期覆寫**(資料夾層級,安全鐵則):
- 新增 helper `_effective_date_with_override(row, overrides)`:
  - 先算既有 `_effective_date(row)`。
  - **僅當** `date_confidence ∈ ('LOW', None)` **且**該檔來源父資料夾有 `date_override` → 改用覆寫日期,標 `date_source='manual'`、confidence 視為 HIGH。
  - **HIGH 的真 EXIF 永不被覆蓋**(即使該夾有 date_override)。
- `_build_target_path` 與 `_compute_event_groups` 兩處都改用此 helper,確保「歸檔」與「跨度分群」用同一個校正後日期。

**測試** `tests/test_folder_overrides_plan.py`:
- event_name override → 檔案歸到該標籤夾(取代 no-event)。
- date_override 套到夾內 LOW 日期檔;同夾 HIGH-EXIF 檔**不受影響**(鐵則回歸)。
- 無 override → 行為完全不變(回歸)。
- 端對端 `plan()`:override 後目標路徑含事件名 + 正確年份。

---

## O3 — `review --organize` 網頁整理 UI

**新模組 `photo_organizer/folderorganize.py`**(鏡像 `folderreview.py` 結構)。

**State**(server 啟動時算一次):
- 候選資料夾 = **存活檔案**(排除 STAGE_DELETE)依來源父資料夾分組,保留滿足任一者:
  - 名稱是 no-event(`_is_unorganised_folder_name` 或 sanitize 後為空),或
  - 夾內有 `date_confidence ∈ ('LOW', None)` 的存活檔。
- 每夾算:檔數、**EXIF 日期範圍**(夾內 HIGH/MEDIUM 日期的 min/max,供使用者判斷年份)、目前目標路徑預覽、旗標(⚠ 無事件名 / ⚠ N 個 LOW 日期)、既有 override(若有)。
- 縮圖:**重用** `webreview.ThumbCache`(`_staging/.thumbs/`),每夾取數張代表縮圖,惰性產生。
- 沿用 `folderreview` 的母資料夾分群 + `<details>` 可收合;有 pending(未填 override)的群組預設展開。

**渲染**:每個資料夾卡片 = 縮圖列 + 日期範圍 + 路徑預覽 + 兩個輸入(事件名、日期 `YYYY-MM-DD`)+ save。

**POST 端點**(決策 seam,只寫 `folder_overrides`,plan 之後才套用):
- `/folder-override` — 存單一(source_folder, event_name, date_override)。
- `/folder-override-all` — 批次(大 cap,比照 `_MAX_BATCH_BYTES`)。
- `/folder-override-clear` — 清單一。
- host guard + body cap + `(KeyError/ValueError/TypeError)→400`,比照 `folderreview`。

**⚠ Key 契約(O2 review 發現,O3 必須遵守)**:`folder_overrides` 以**檔案的最內層父資料夾** `str(Path(path).parent)` 為 key;planner 也用這個 key 查。但事件標籤是由 `_resolve_event_folder` 往上爬祖先得到的。因此 **O3 寫入 override 時,必須以「受影響檔案的最內層父資料夾」為 source_folder**,不能用顯示出來的(祖先)事件夾路徑——否則 planner 查不到、靜默失效。若一個顯示群組底下有多個最內層子資料夾,O3 需對每個子資料夾各寫一筆(或讓使用者明確選層級)。

**CLI**:`cmd_review` 加 `elif getattr(args, "organize", False)` 分支 → `folderorganize.serve`;review subparser 加 `--organize` 旗標。

**測試** `tests/test_folder_organize.py`:State 候選計算(no-event ∪ LOW,排除 staged)、日期範圍、GET 渲染、POST override 存檔、POST clear、批次、CLI `--organize` wired。

---

## 驗證

```
python -m pytest tests/test_folder_overrides_db.py tests/test_folder_overrides_plan.py tests/test_folder_organize.py -v
python -m pytest -q          # 全套件回歸全綠
```

真實庫(去重後)冒煙:`review --organize` 起伺服器、確認 374 候選夾分群渲染、填幾個 override → 重跑 `plan` 確認目標路徑套用了事件名與校正年份。

---

## 執行順序與相依

- O1 → O2、O1 → O3(O2 與 O3 互相獨立)。
- 用 subagent-driven(implementer → spec → quality)逐 phase 執行。
- **本工具現在可建,但實際使用在近似重複 `review` 之後**(只處理存活檔)。
