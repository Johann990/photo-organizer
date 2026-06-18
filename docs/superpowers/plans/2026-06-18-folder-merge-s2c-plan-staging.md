# S2c — Folder-merge: plan/execute 整合（STAGE_DELETE for loser folders）

## Context

`folder_overlaps`（schema v4）已有 `status='reviewed'` + `keeper IN ('a','b')` 的決策紀錄（由 S2b `review --folders` 寫入）。S2c 讓 `plan` 讀這些決策，把「輸家資料夾」中有 SHA-256 對應到「贏家資料夾」的檔案加進 `stage_ids`，讓既有 STAGE_DELETE 迴圈自動處理。

**Unique-to-loser 策略（已確認）**：SHA 不在贏家中的孤兒檔案 **不 stage**，走正常 MOVE pipeline，plan 預覽顯示計數警告。

**不動的檔案**：`executor.py`、`tests/test_undo_noninteractive.py`、`tests/test_redundant_copies.py`（另一 session 工作）。

---

## Task 1 — `_folder_merge_loser_ids()` 函式 + 單元測試

### 位置

`photo_organizer/planner.py`，放在 `redundant_copy_ids` 之後（約 line 680+）。

### 規格

```python
def _folder_merge_loser_ids(db: Database) -> tuple[set[int], int]:
    """Return (loser_file_ids_to_stage, unique_to_loser_count).

    For each reviewed folder_overlap with a keeper:
    - Files in the loser subtree whose SHA-256 exists in the keeper subtree
      → returned in loser_ids (to be STAGE_DELETE'd by plan).
    - Files unique to the loser (no SHA match, or sha=NULL)
      → counted in unique_count; NOT staged; move through normal pipeline.
    - Files with status='done' or status='error' → skipped entirely.
    - keeper=NULL ('both') → no staging for that pair.

    DB-only, no disk reads, idempotent.
    """
    reviewed = db.conn.execute(
        "SELECT folder_a, folder_b, keeper "
        "FROM folder_overlaps "
        "WHERE status='reviewed' AND keeper IN ('a','b')"
    ).fetchall()

    if not reviewed:
        return set(), 0

    loser_ids: set[int] = set()
    unique_count = 0

    for row in reviewed:
        if row["keeper"] == "a":
            keeper_folder, loser_folder = row["folder_a"], row["folder_b"]
        else:
            keeper_folder, loser_folder = row["folder_b"], row["folder_a"]

        # SHA-256s present anywhere in the keeper subtree
        keeper_shas: set[str] = {
            r["sha256"]
            for r in db.conn.execute(
                "SELECT sha256 FROM files "
                "WHERE path LIKE ? AND sha256 IS NOT NULL AND status != 'error'",
                (keeper_folder + "\\%",),
            )
        }

        # Files in the loser subtree (exclude done/error)
        for f in db.conn.execute(
            "SELECT file_id, sha256 FROM files "
            "WHERE path LIKE ? AND status NOT IN ('error', 'done')",
            (loser_folder + "\\%",),
        ):
            if f["sha256"] is None or f["sha256"] not in keeper_shas:
                unique_count += 1
            else:
                loser_ids.add(f["file_id"])

    return loser_ids, unique_count
```

**注意**：LIKE pattern `folder + "\\%"` 在 Python 字串中為 `folder\%`；SQLite LIKE 的 `\` 是普通字元，`%` 是萬用字，因此正確比對 `folder\` 開頭的所有子樹路徑。

### 新增測試：`tests/test_planner_folder_merge.py`

用最小 DB fixture（`_add_file` helper 類似 `test_add.py`），**不呼叫完整 `plan()`**：

```python
import pytest
from photo_organizer.db import Database
from photo_organizer.planner import _folder_merge_loser_ids

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64  # unique to loser

def _add_file(db, path, sha=None, status="scanned", file_type="CAMERA_JPEG"):
    db.conn.execute(
        "INSERT INTO files (path, filename, sha256, status, file_type, size_bytes) "
        "VALUES (?, ?, ?, ?, ?, 1000)",
        (path, path.split("\\")[-1], sha, status, file_type),
    )
    db.commit()
    return db.conn.execute(
        "SELECT file_id FROM files WHERE path=?", (path,)
    ).fetchone()[0]

def _add_overlap(db, folder_a, folder_b, keeper):
    db.insert_folder_overlap(
        folder_a=folder_a, folder_b=folder_b,
        shared_count=5, a_only_count=0, b_only_count=1,
        coverage_a=1.0, coverage_b=0.833, keeper=keeper,
    )
    db.conn.execute(
        "UPDATE folder_overlaps SET status='reviewed' "
        "WHERE folder_a=? AND folder_b=?", (folder_a, folder_b)
    )
    db.commit()
```

Tests:

1. **`test_loser_staged_when_sha_in_keeper`**
   - Keeper folder A: file with SHA_A
   - Loser folder B: file with SHA_A (same SHA → match)
   - `_folder_merge_loser_ids(db)` → loser file_id in result set, unique_count=0

2. **`test_unique_to_loser_not_staged`**
   - Keeper folder A: file with SHA_A
   - Loser folder B: file with SHA_C (SHA not in A)
   - → empty set, unique_count=1

3. **`test_both_keeper_skipped`**
   - Pair with `keeper=None` (both) → even after `status='reviewed'`, not in the query range
   - → empty set, unique_count=0
   - *Implementation note*: `_add_overlap` with `keeper=None` then `status='reviewed'`; the SELECT filters `keeper IN ('a','b')` so this pair is skipped.

4. **`test_null_sha_not_staged`**
   - Loser file has `sha256=NULL`
   - → not staged, unique_count=1

5. **`test_done_status_skipped`**
   - Loser file has `status='done'`
   - → not in result (skipped by `status NOT IN ('error','done')`)

6. **`test_multiple_pairs`**
   - Two independent reviewed pairs
   - Pair 1: loser B1 has one match → staged
   - Pair 2: loser B2 has one match + one unique → staged 1, unique_count 1
   - → total loser_ids = 2, unique_count = 1

### TDD

先跑（應全失敗）→ 實作 → 再跑（應全通過）。

---

## Task 2 — 整合進 `plan()` + 預覽表 + 端對端測試

### 整合位置（`planner.py`）

緊接在 `1c-bis. Redundant copies` 之後、現有 `1d. Safety net` 之前，插入新 step 1d（現有 1d 順移為 1e）：

```python
    # 1d. Folder-merge loser staging — files in reviewed "loser" folders whose
    # SHA-256 is confirmed present in the "keeper" folder subtree.
    with console.status("Staging folder-merge losers…"):
        folder_merge_losers, fm_unique_count = _folder_merge_loser_ids(db)
    stage_ids.update(folder_merge_losers)
    # NOT added to content_safe_stage: keeper has same SHA, so the 1e safety
    # net correctly protects the keeper copy without exemption.
```

### 預覽表（plan summary section）

在 near_dup_losers 那行之後，加兩行（條件顯示）：

```python
    if folder_merge_losers:
        t.add_row(
            "[red]Stage for deletion — Folder-merge losers[/red]",
            f"{len(folder_merge_losers):,}",
            "redundant copies in folded-in folder; keeper has all SHA-256s",
        )
    if fm_unique_count:
        t.add_row(
            "[yellow]Unique files in loser folder(s)[/yellow]",
            f"{fm_unique_count:,}",
            "no SHA match in keeper — moved to library normally",
        )
```

### 端對端測試（追加至 `tests/test_planner_folder_merge.py`）

**`test_plan_creates_stage_delete_for_loser`**

呼叫完整 `plan(db, target_root)`，驗證 operations 表中有正確的 STAGE_DELETE：

```python
def test_plan_creates_stage_delete_for_loser(tmp_path):
    from photo_organizer.planner import plan

    db_path = tmp_path / ".photo_organizer" / "library.db"
    target = tmp_path / "Organised"
    target.mkdir()

    with Database(db_path) as db:
        # Keeper folder: one file
        keeper_id = _add_file(db, r"D:\A\img001.jpg", sha=SHA_A)
        # Loser folder: one file with same SHA (duplicate) + one unique
        loser_dup_id = _add_file(db, r"D:\B\img001.jpg", sha=SHA_A)
        loser_uniq_id = _add_file(db, r"D:\B\img002.jpg", sha=SHA_C)
        _add_overlap(db, r"D:\A", r"D:\B", keeper="a")

        plan(db, target, assume_yes=True)

        ops = {
            r["file_id"]: r["op_type"]
            for r in db.conn.execute("SELECT file_id, op_type FROM operations")
        }
        assert ops[loser_dup_id] == "STAGE_DELETE"
        assert ops.get(loser_uniq_id) != "STAGE_DELETE"  # unique file moved normally
        assert ops[keeper_id] == "MOVE"  # keeper untouched
```

---

## 驗證

```
python -m pytest tests/test_planner_folder_merge.py -v
python -m pytest -q
```

全綠後 commit。
