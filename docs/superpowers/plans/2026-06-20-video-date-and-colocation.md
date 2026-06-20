# 影片日期修正 + 與事件共置 / Video date fix + event co-location

## Context

實測發現:3,975 個影片裡只有 156(4%)抓到嵌入式拍攝時間,96% 只剩 mtime;影片佔全部 LOW 日期的 51%。`exiftool` 抓取無誤(`DateTimeOriginal→CreationDate→CreateDate→MediaCreateDate`),是老 MOV/AVI 本身沒寫——重掃救不回。

兩個衍生問題:**(a) 日期不準 → 歸錯年**、**(b) 影片被丟進獨立 `Videos/{年}/` 樹 → 脫離事件脈絡、離散難分類**。

數據:LOW 影片 3,819 中,**2,639(69%)同夾有可信日期照片可借**;**1,572** 個檔名含真實拍攝日。

**已確認決策**:
- 日期:**借鄰居照片 + 信任影片檔名**(修近全部 3,819)。
- 佈局:**與事件共置**;base = **C(事件只要有任一 Masters 照片→Masters,否則 Others)**。
- folder-organize 的 low-date filter **排除影片**。

安全鐵則不變:HIGH/MEDIUM 真日期永不被覆寫。優先序 **人工 override > 借鄰居 > 影片檔名 > mtime**。

---

## V1 — folder-organize 排除影片出 low-date filter(小、獨立)

`photo_organizer/folderorganize.py`:`FolderOrganizeState` 計算候選時,`low_date_count` 只算**非 VIDEO** 檔。資料夾只因「含非影片 LOW/無日期檔」或「無事件名」上榜。更新 `tests/test_folder_organize.py`(加:純影片 LOW 的資料夾不因此上榜)。

---

## V2 — 影片日期:信任檔名 + 借鄰居照片(planner / 日期階梯)

**(a) 信任影片檔名日期** — `_resolve_date`(planner.py:168-228)加影片專屬規則:`file_type=='VIDEO'` 且無可信嵌入 EXIF 日期、但 `_parse_filename_dt(filename)` 有日期 → 採 `filename`、**MEDIUM**(影片無 EXIF 可衝突,檔名即拍攝日)。不影響照片邏輯。

**(b) 借鄰居照片日期** — 像 overrides 一樣 thread 一個 `sibling_hints: {folder: date}` 進 `_build_target_path` / `_compute_event_groups`:
- plan 先一次掃描算 `{來源父資料夾 → 該夾 HIGH/MEDIUM 照片的代表日}`(用眾數,平手取最早)。
- 擴充 `_effective_date_with_override`(或新 wrapper):VIDEO 且 confidence LOW/None 且無人工 override 時 → 用 `sibling_hints` 的日期(標 `date_source='sibling'`、視為 MEDIUM)。
- 優先序:override > sibling > (檔名已在 a 升級) > mtime。

測試:檔名升級、借鄰居、無鄰居退回、override 仍最優先、照片不受影響(回歸)。

---

## V3 — 影片與事件共置(重構 `_build_target_path` 影片分支)

**目標路徑**改為:`{base}/{年}/{事件夾}/Videos/{stem}_{seq:04d}.EXT`,事件夾結構與照片**完全一致**(單日 `{日期}_{事件}`、多日 `{起始}_{Nd}_{事件}`、subject `{事件}/{年}`)。

**重構**:把照片分支「依 group/dt/event 算事件 subdir」抽成 helper `_event_subdir(base, dt, event, group)`,照片與影片共用;影片再接 `/ "Videos"`。

**base = C**:plan 先算 `event_base: {resolved_event_folder → 'Masters'|'Others'}`(該事件任一照片相機在 known_cameras → Masters,否則 Others),thread 進 `_build_target_path`。影片分支用 `_resolve_event_folder` 取得事件、查 `event_base` 得 base;查不到事件(None)時退回 `Others/Videos/{年}/...` 或 `Videos/NoDate/`(保留現有無事件退路,只是改掛在 Others 下或維持 Videos/——擇一,實作時定，預設:無事件影片維持現行 `Videos/{年}/` 全域樹,避免硬塞)。
- NoDate 影片:`{base}/{...}/Videos/` 仍需 dt;無 dt 時 → 維持 `Videos/NoDate/`。

測試:單日/多日/subject 三種事件下影片落在對應事件夾的 `Videos/` 子夾;base=C 正確(混相機事件→Masters);無事件影片退路不變;seq 命名不撞。

---

## 驗證

```
python -m pytest tests/test_folder_organize.py tests/test_folder_overrides_plan.py -v
python -m pytest -q
```
真實庫冒煙:重跑 `plan --force`,抽查影片落點(事件夾/Videos/、正確年份),確認 LOW 影片數大幅下降。

執行順序:V1 → V2 → V3(各自 implementer → spec → quality)。
