# 設計:多日事件依日分夾 + Opt-in 結構 / Multi-day per-day subfolders

**Date:** 2026-06-21
**Status:** Approved (design), pending implementation plan

## 問題 / Problem

使用者的 15 年照片庫裡,許多「多日活動」資料夾已經**按日整理成子資料夾**
(例:`...\20050814 蒙古\0808\`、`\0809\`…0808~0813 是每天一夾)。

目前 `plan` 把多日事件**攤平**成單一資料夾依日期歸檔
(`Masters\2005\2005-08-06_9d_蒙古\` — 所有天混在一起),**丟掉了使用者已建立的每日結構**。

使用者要:
1. 多日事件在 `D:\Media` 裡**依日切成每日子夾**(而非攤平)。
2. 只對**自己選定**的事件套用(opt-in),其餘維持現有自動行為。
3. 工具**自動偵測**「已按日分夾的多日活動」並建議套用(找規則)。
4. 真正需要重新切分的混日傾倒夾,使用者**自己在檔案總管拆**,工具只標記/偵測/保留(工具不動磁碟)。

確認的決策(來自 brainstorm Q&A):
- 範疇 = **Opt-in**(非全庫;非全自動)。
- `{mmdd}` 子夾名 = **每個檔案自己的日期**算出(離群檔自動歸正確日),非照抄來源子夾名。
- 多日事件夾名 = **全域改成**新格式(見 §3)。
- 實體拆分由使用者在總管做;工具不碰磁碟。

## 非目標 / Non-goals

- 工具**不**做磁碟改名 / 拆分 / 合併(使用者在檔案總管做,再 `relocate` 同步)。
- **不**逐字保留來源任意子夾名(改用 `{mmdd}` 正規化)。
- **不**改動去重邏輯(exact / near / folder-merge / redundant 不變)。
- **不**改單日 / subject / 無事件 的落點(除 §3 的多日改名外)。

---

## 設計 / Design

### 1. 資料模型 / Data model

`folder_overrides` 表新增欄位:
```sql
per_day_split INTEGER NOT NULL DEFAULT 0   -- 1 = 此事件依日切成 {mmdd}/ 子夾
```
schema_version 5 → 6(加法式,`CREATE TABLE IF NOT EXISTS` + 既有 `_check_schema_version` 自動補欄;
既有 v5 DB 開啟時 `ALTER TABLE ... ADD COLUMN per_day_split` 經一個 idempotent 遷移補上)。

> **遷移注意**:新增欄位到既有表需要 `ALTER TABLE ADD COLUMN`(不是純 `CREATE TABLE IF NOT EXISTS`,
> 因為表已存在)。需寫一個 idempotent 遷移:偵測欄位不存在才 ADD。沿用專案既有遷移風格
> (見 `_migrate_*` 函式;此為單純 ADD COLUMN,不需 table rebuild)。

DB helper 更新:
- `set_folder_override(...)` 增加 `per_day_split` 參數(預設保留現值或 0)。
- `get_folder_overrides()` 回傳列含 `per_day_split`。

key 契約不變:`source_folder` = 事件根資料夾的路徑(使用者標記的那一夾)。

### 2. 偵測規則 / Detection: "已按日分夾的多日活動"

純算 DB、不讀碟。對每個能 `_resolve_event_folder` 解析出事件名的資料夾 E:

判定為「多日依日分夾候選」當且僅當:
- E 底下有 **≥2 個含照片的直接子資料夾**;且
- **每個**這類子夾,其有信心日期(HIGH/MEDIUM)的照片中 **≥90%** 落在**單一日曆日**;且
- 各子夾的「代表日」合起來 **跨 ≥2 個不同日**,且**大致連續**(相鄰代表日間隔 ≤ `_EVENT_DAY_GAP` 天,預設 3;避免把無關堆積誤判成一趟活動);且
- 跨度 ≤ `MAX_EVENT_SPAN_DAYS`(沿用既有多日上限;超過屬 subject)。

輸出:`detect_per_day_events(db, scan_roots) -> list[{event_folder, days, span, subfolders}]`,
供 review --organize 顯示建議。

附帶偵測(唯讀提醒,不自動處理):**「多日但無每日子夾」** 的事件
(E 直接含照片、跨多日、無單日子夾)→ 列為「建議去總管拆成每日子夾」。

### 3. 全域多日事件夾改名 / Multi-day folder rename (all multi-day events)

`_event_subdir` 的多日(`kind=="event"`)分支,資料夾命名格式改為:
```
舊 / old:  {start.isoformat()}_{span}d_{event}      → 2005-08-06_9d_蒙古
新 / new:  {start:%Y%m%d}-{span}d {event}           → 20050806-9d 蒙古
```
- 起始日 ISO(含 `-`)→ 緊湊 `yyyymmdd`;`_{N}d` → `-{N}d`;`_{event}` → ` {event}`(空格)。
- 無 event 名時 → `{yyyymmdd}-{N}d`(省略事件段)。
- **影響所有多日事件**(標記與未標記皆然)。
- 單日維持 `{YYYY-MM-DD}_{event}`(本次不改;spec review 時可再議)。
- 既有測試(`test_subject_collection` 等)斷言舊格式者需更新為新格式(屬刻意行為變更)。

### 4. 落點 / Placement

`_build_target_path`(及共用的 `_event_subdir`)依以下規則(`overrides[event_root]` 提供 `per_day_split`):

| 情況 | 落點 |
|------|------|
| **多日 + per_day_split=1** | `{base}/{年}/{yyyymmdd}-{N}d {event}/{mmdd}/{原檔名}` |
| 多日(未標記) | `{base}/{年}/{yyyymmdd}-{N}d {event}/{原檔名}`(扁平,新夾名) |
| 單日 | `{base}/{年}/{YYYY-MM-DD}_{event}/{原檔名}`(不變) |
| subject | `{base}/{event}/{年}/{原檔名}`(不變) |
| 無事件 / 無日期 | 不變 |

- `{年}` = 事件起始年;`{yyyymmdd}`/`{N}d`/`{event}` = 事件起始日 / 跨度 / 解析名(可被 `event_name` override)。
- `{mmdd}` = **該檔案自己的有效日期**(經 `_effective_date_with_override` → 含 override / 借鄰居 / 檔名 / mtime 的結果)的月日。離群檔自動歸到正確那天。
- `{base}` = 規則 C(`_event_base_map`:事件任一已知相機照片 → Masters,否則 Others)。
- per_day_split 的查表 key = 檔案所屬事件根。事件根 = `_resolve_event_folder(path)`;
  `overrides` 以 source_folder 為 key,需確認標記時存的是**事件根**(= resolved 事件夾),
  而非檔案最內層父夾。**這與既有 override(以最內層父夾為 key)不同** → 見 §7 開放問題。

**安全網**:`{mmdd}` 需要該檔有有效日期。多日事件成員理論上都有日期(否則不會被歸進該事件);
若某成員無日期 → 退回該事件扁平夾(不建 `{mmdd}`,避免 `NoDate` 散落),並寫 WARN 到 run_log。

### 5. review --organize UI

- 候選卡新增旗標分類:**「📂 多日活動已按日分夾(N 天)— 建議依日分夾」**,
  附 `per_day_split` 切換鈕,偵測到的**預設開**,但需 save 才寫入(opt-in,沿用現有 save 模型)。
- 既有「無事件名 / 含(非影片)LOW 日期」候選維持。
- 新增**唯讀提醒區**:「多日但無每日子夾」的夾 → 列出,提示去總管拆(工具不動手)。
- POST 端點 `/folder-override` 增加 `per_day_split` 欄位傳遞。

### 6. 工作流 / Workflow

```
1. review --organize  → 採納偵測到的「依日分夾」事件;混日傾倒夾被點名 → 去總管手動拆
2. relocate           → 把總管裡的搬移用 SHA-256 重新同步 DB 路徑
3. plan --force       → 標記事件走依日分夾;多日改新夾名;dedup 照常;其餘自動
4. execute
```

### 7. 開放問題 / Open questions(實作前需定）

1. **per_day_split 的 key 粒度**:標記時 `source_folder` 應為**事件根**(resolved 事件夾),
   但既有 `event_name`/`date_override` override 以**最內層父夾**為 key(O2 契約)。
   兩者 key 不同會混淆。**建議**:per_day_split 也以事件根為 key,planner 查 per_day_split 時
   用 `str(_resolve_event_folder(path))` 查,查 event_name/date 時用最內層父夾查。
   實作時需在 UI 明確標示「此設定套用於整個事件」。
   → **本問題在 writing-plans 階段定案。**
2. 單日事件是否也改新日期格式(`yyyymmdd 事件`)?本設計暫不改。

---

## 測試策略 / Testing

- 偵測規則單元測試:正例(蒙古式 0808~0813,90% 同日、連續)、反例(只 1 子夾、子夾混日 <90%、跨日不連續、超過 subject 上限)。
- `_event_subdir` 多日新格式(全域)+ 既有測試更新。
- per_day_split 落點:`{base}/{年}/{yyyymmdd}-{N}d {event}/{mmdd}/`;離群檔歸正確日;無日期成員退回扁平。
- DB 遷移:v5 → v6 ADD COLUMN idempotent;舊 DB 開啟不壞。
- review --organize:偵測旗標渲染、per_day_split toggle save、唯讀提醒區。
- 回歸:未標記多日仍扁平(新名);單日/subject/無事件不變;dedup 不受影響。

## 受影響檔案 / Affected files

- `photo_organizer/db.py`(欄位 + 遷移 + helper)
- `photo_organizer/planner.py`(`_event_subdir` 改名 + per_day_split 落點 + `detect_per_day_events`)
- `photo_organizer/folderorganize.py`(偵測旗標 + toggle + 提醒區 + POST)
- `photo_organizer/__main__.py`(若 review --organize 需新參數;多半不需)
- 既有測試更新(多日格式)+ 新測試
- `CLAUDE.md`(多日輸出樹文件)
