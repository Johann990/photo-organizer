# Photo Organizer — Core Concepts

Domain vocabulary for this project. Each entry defines what a term means here precisely enough that a new contributor can follow tickets, code, and design discussions without further context.

---

## Pipeline

### Operation

A planned transition for one file from its current path to its organized destination. An Operation is the atomic unit the executor acts on: it records what to move, where, and the outcome.

Operations have a lifecycle: initially planned, confirmed before execution begins, and either completed, skipped (source absent), or marked as an error. During execution an intermediate in-flight state exists to make crashes detectable — if execution stops between the filesystem move and the database update, the intermediate state allows a re-run to recognize and recover the partially-completed work rather than treating it as never-started.

Operations are write-once organizing decisions: once completed, an Operation is never re-executed or re-planned. The full set of Operations for a library run is the durable record of what was moved and where — re-running the plan phase creates new Operations but does not alter completed ones.

*Avoid:* "task", "step", "action" (used loosely elsewhere but do not denote this specific record)
