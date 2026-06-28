---
title: "A New DB-Quality Check Must Itself Be Verified — Against the Writing Code Path and an Existing Command's Own Counts"
date: 2026-06-22
category: docs/solutions/database-issues
module: auditor
problem_type: database_issue
component: tooling
severity: medium
symptoms:
  - "A brand-new audit/consistency check reports thousands of 'errors' on the production DB that are not actually corruption"
  - "An audit count for a metric disagrees with the count an existing command (plan) already self-reports for the same metric"
  - "A check flags a row state that is actually a DESIGNED encoding, not an inconsistency"
root_cause: missing_validation
resolution_type: process_fix
related_components:
  - planner
  - reviewer
tags:
  - audit
  - false-positive
  - db-quality
  - cross-check
  - decision-ledger
  - verification
---

# A New DB-Quality Check Must Itself Be Verified

## Problem

While building `photo_organizer audit` (`auditor.py`, a read-only DB-only consistency pass
distinct from `validate` and `reconcile`), two of its checks fired huge false-positive
counts on the real ~282k-file production DB. A new verification tool is itself unverified
code — its first output cannot be trusted as ground truth.

## Symptoms

- A `duplicates` check flagged 1,091 rows as "errors" that were not corruption.
- A `low_confidence_dates` summary reported 5,564 when the true figure was 4,917 — an
  inflation of 647.
- Both numbers *looked* plausible enough to be mistaken for real DB problems.

## Root Cause

1. **Flagging a designed encoding as corruption.** The check treated
   `duplicates.status='reviewed' AND keep_file_id IS NULL` as an inconsistency. But that
   row shape is the DESIGNED encoding for a "keep all" decision on a NEAR cluster — written
   intentionally by `reviewer._record_decision`. It is not corruption, so flagging it
   produced 1,091 false errors.
2. **Wrong scope vs. the code that actually writes the data.** `low_confidence_dates` must
   scope to the same file types the project's own date-forensics pass considers
   (`planner._DATE_AUDIT_FILE_TYPES` = RAW / CAMERA_JPEG / DEV_JPEG / HEIC / VIDEO).
   `RESIZED_JPEG` / `UNKNOWN` are deliberately skipped by date forensics (they are headed
   for STAGE_DELETE, not filing) and can carry a stale/absent `date_confidence` that
   carries zero misfiling risk. Counting them inflated the figure by 647.

## Solution

- Replace the false "missing keep_file_id" check with a real one:
  `file_id_a = file_id_b` (a genuinely self-referential / corrupt row).
- Scope `low_confidence_dates` to `planner._DATE_AUDIT_FILE_TYPES`. After scoping, the audit
  reported 4,917 — which matches `plan`'s own self-reported low-confidence figure exactly.

## Why This Works

The corrected scope reconciles against an existing command's own reported number for the
same metric (`plan`'s low-confidence count). When two independently-written code paths agree
on a count, that agreement is strong evidence the new check is measuring the right set. The
designed-encoding fix comes from reading the actual writer (`reviewer._record_decision`)
rather than assuming what a row "should" mean from schema shape alone.

## Prevention

Before trusting a new DB-quality / audit / consistency check:

1. **Read the code path that writes the data**, not just the schema. A row shape that looks
   wrong may be a deliberate encoding (here: "keep all" = `reviewed` + NULL `keep_file_id`).
2. **Cross-check the count against any existing command that already reports the same
   metric.** If `plan` already prints a low-confidence-date count, a new audit of the same
   thing must reconcile to it before being believed.
3. Treat a brand-new verification tool as unverified code: its first output is a hypothesis,
   not a finding.

## Related Issues

- `photo_organizer/auditor.py` — `audit` command, the two corrected checks
- `photo_organizer/reviewer.py` — `_record_decision` ("keep all" encoding)
- `photo_organizer/planner.py` — `_DATE_AUDIT_FILE_TYPES`, plan's own low-confidence count
- Auto-memory: `project-trust-pillars` (Pillar 1) indexes this lesson
