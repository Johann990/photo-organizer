---
title: "SQLite LIKE Scope Queries Must Escape Literal _ % \\ in Folder Paths (ESCAPE clause)"
date: 2026-06-22
category: docs/solutions/database-issues
module: sync
problem_type: database_issue
component: tooling
severity: high
symptoms:
  - "A path-prefix scope query silently matches an unrelated sibling folder"
  - "sync rename/move rewrites files.path under a folder whose name happens to share a prefix"
  - "Folder names containing literal underscores behave as if the _ were a wildcard"
root_cause: missing_validation
resolution_type: code_fix
related_components:
  - relocate
  - db
tags:
  - sqlite
  - like-escape
  - wildcard
  - path-prefix
  - sync
  - data-integrity
---

# SQLite LIKE Scope Queries Must Escape Literal `_` `%` `\` in Folder Paths

## Problem

`sync`'s scope query selects every file under a folder with
`path = ? OR path LIKE ? ESCAPE '\'`. SQLite's `LIKE` treats `_` as "any single character"
and `%` as "any sequence". This library's real folder names are full of literal underscores
(e.g. `三貂嶺_小蜂`), so an unescaped pattern silently treats those `_` as wildcards and can
match an unrelated sibling folder.

## Symptoms

- A `sync rename` / `sync move` scoped to one folder rewrites `files.path` for files in a
  *different* folder whose name matches the (wildcard-interpreted) pattern.
- The bug is data-silent: no error, just rows quietly re-pointed under the wrong prefix.

## Solution

Escape `\`, `%`, AND `_` in the path before building the `LIKE` pattern, and declare the
escape character with `ESCAPE '\'`:

```python
# escape order matters: backslash first, then the two wildcard metachars
def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

cur.execute(
    "SELECT ... FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\'",
    (folder, _like_escape(folder) + "%"),
)
```

Covered by `test_relocate_path_does_not_match_unrelated_sibling`.

## Why This Works

With every `_`/`%` in the literal folder name escaped, `LIKE` matches only the intended
prefix; the trailing `%` is the sole remaining wildcard. Escaping `\` first prevents the
escape character itself from being doubled-up incorrectly by the later replacements.

## Prevention

- Any path-prefix `LIKE` query in this project must run the path through the escape helper
  and declare `ESCAPE '\'`. Folder names here routinely contain literal `_`.
- Where a whole-library pass can avoid `LIKE` entirely, prefer the bulk-load + in-memory
  ancestor-chain match instead (see the `sqlite-like-no-index-perf` lesson) — it sidesteps
  both this wildcard hazard and the no-index full-scan cost.

## Related Issues

- `photo_organizer/sync.py` — `relocate_path()` scope query
- `tests/test_sync.py` — `test_relocate_path_does_not_match_unrelated_sibling`
- Auto-memory: `project-trust-pillars` (Pillar 2) and `sqlite-like-no-index-perf`
