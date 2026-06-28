# Photo Organizer



## 實作狀態 / 確認的用戶決策 / Status & confirmed decisions

所有 pipeline phase 均已完成。進度與已確認決策(RAW→JPEG 保留、評分/標籤入 DB、
Windows 優先、目標儲存 2-Bay external)記於 auto-memory(`project-impl-status`、
`project-user-decisions`,每個 session 自動載入),不在此重複以免雙處維護。


## 資料庫即決策紀錄 / The DB is your decision record

這個 DB 不是可丟棄的暫存檔——它是整個整理過程所有決策的**永久帳本**:日期判定、
去重的勝/敗、近似重複的人工審查、每一筆搬移操作與其結果都存在裡面。
**備份這個 DB＝備份你的整理決策。** 弄丟了就只能對著已整理好的資料夾重新猜當初為什麼這樣分。

- **預設位置跟著照片庫走 / Default location**:有 `--target` 但沒給 `--db` 時,DB 預設落在
  `{target}/.photo_organizer/library.db`。明確的 `--db`(含 config 裡的 `db`)永遠優先。
- **版本守門 / Schema-version guard**:`schema_version`(`meta` 表)讓較舊的 DB 自動跑 idempotent
  遷移;若 DB 是**比目前程式還新**的版本寫的,會拒絕開啟以免破壞看不懂的資料。

## 文件分工

- **指令怎麼用**(18 個指令、旗標、情境教程、`--force` 語意、日期鑑識解析階梯)→ `docs/COMMANDS.md`
- **架構/需求/schema/輸出目錄/影片規則/redundant-copy/near-review 過濾** → `SKILL.md`
- **已記錄的 bug、最佳實踐、架構決策**任何執行時發現的非顯而易見的限制或錯誤，修完後必須更新(YAML frontmatter:`module`/`tags`/`problem_type`)→ `docs/solutions/`
- **領域詞彙的精確定義** → `CONCEPTS.md`
- **產品策略/方向** 規劃新功能前先讀 STRATEGY.md 確認方向一致→ `STRATEGY.md`

## 環境需求

- `--target` 必須與照片來源在同一碟機(`os.rename()` 不跨碟)
- ExifTool for Windows:https://exiftool.org → 重新命名為 `exiftool.exe` 加入 PATH
- **HEIC 近似去重需 `pillow-heif`**:`pip install pillow-heif`,否則 iPhone `.heic`/`.heif` 在 `dedup` 近似比對會逐張失敗(記成 pHash error,非致命);掃描/搬移不受影響。
