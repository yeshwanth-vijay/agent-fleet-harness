# DEMO-101 root cause

## Summary
A single function, `is_overdue` in `taskboard/tasks.py`, is wrong in two ways. The dashboard count and the OVERDUE list both come from it, so one fix repairs both.

```python
24  def is_overdue(task: Task, today: date) -> bool:
25      if task.due is None:
26          return False
27      return task.due <= today
```

## Cause A: due today counts as overdue (tasks.py:27)
`<=` is true when `task.due == today`. A task is late only once its due date has passed, so the comparison must be strict `<`.
Symptom: `OVERDUE 2026-10-21 Send invoice batch (Marco)`. Its due date equals `--today 2026-10-21`. Marco's comment ("only becomes late tomorrow") agrees.

## Cause B: completed tasks are never excluded (tasks.py:24-27)
The function never reads `task.done`. A done task with a past due date is reported as overdue.
Symptom: `OVERDUE 2026-10-15 Close Q3 books (Marco)`. In tasks.json:4 this task has `"done": true`, but 2026-10-15 <= 2026-10-21, so it is listed.
This is also inconsistent with `due_soon` (tasks.py:35), which does check `not t.done`.

## How the symptoms propagate (no other bugs found)
| Symptom | Line | Why |
|---|---|---|
| `Overdue: 3` | tasks.py:41 `len(overdue(...))`, cli.py:17 | counts whatever `is_overdue` accepts: TLS (correct), invoice (cause A), Q3 books (cause B) |
| 3 OVERDUE rows | cli.py:18-19, tasks.py:30-31 | `overdue()` filters with `is_overdue`, and the CLI prints the result unchanged |

Sample-data trace with today=2026-10-21:
- TLS, due 10-19, open: late. Correct.
- Invoice, due 10-21, open: listed because of cause A.
- Q3 books, due 10-15, done: listed because of cause B.
- Onboarding, due 10-23: not late.
- Archive, no due date: not late.

## Not part of the defect
- `Tasks: 5  Open: 4  Done: 1  Due soon: 2` is correct. `due_soon` (tasks.py:34-35) already skips done tasks, and a task due today (0 days) is legitimately due soon.
- `load` (tasks.py:18-21) parses correctly. The `--today` default in cli.py:13 is evaluated at import time, which is harmless for a CLI (not changed).

## Why tests missed it
tests/test_tasks.py:8-23 only checks a task due yesterday and a task with no due date. Neither the boundary day nor `done` is tested.

Note: I traced this by reading the code; I did not run the CLI.
