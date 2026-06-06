---
name: run-phase
description: >
  Run a specific phase of the Photo Organizer pipeline.
  Use when the user says /run-phase <phase>, or asks to
  run scan / report / dedup / review / plan / execute / validate.
disable-model-invocation: true
---

# run-phase Skill

Execute one phase of the photo-organizer pipeline using the project's
`config.json`. Always use `--config` so paths are consistent.

## Config location

```
C:/Projects/PhotoOrganizer/config.json
```

DB path (from config): `C:/PhotoTestZone/photos.db`

---

## Phase commands

Run from the project root: `C:/Projects/PhotoOrganizer`

| Phase | Command |
|-------|---------|
| validate | `python -m photo_organizer validate --config config.json` |
| scan | `python -m photo_organizer scan --config config.json` |
| report | `python -m photo_organizer report --config config.json` |
| dedup | `python -m photo_organizer dedup --config config.json` |
| review | `python -m photo_organizer review --config config.json` |
| plan | `python -m photo_organizer plan --config config.json` |
| execute | `python -m photo_organizer execute --config config.json` |

### Optional flags

- `--force` — re-run a phase even if already marked complete
- `--workers N` — override parallel threads (scan / dedup only)
- `--exact-only` / `--near-only` — dedup only (run one half)
- `--hamming N` — near-dupe threshold (dedup only, default 8)

---

## Recommended order

```
validate → scan → report → dedup → review → plan → execute
```

- Always run **validate** first on a new config.
- Run **report** after scan to check file-type breakdown before dedup.
- Run **review** after dedup to approve near-dupe deletions (interactive).
- Run **plan** before execute — review the planned operations first.
- **execute** touches real files. Confirm the plan output looks correct.

---

## Usage examples

User types `/run-phase scan` → run:
```
python -m photo_organizer scan --config config.json
```

User types `/run-phase dedup --exact-only` → run:
```
python -m photo_organizer dedup --config config.json --exact-only
```

User types `/run-phase scan --force --workers 8` → run:
```
python -m photo_organizer scan --config config.json --force --workers 8
```
