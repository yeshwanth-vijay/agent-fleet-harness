# DEMO-101: Dashboard overdue count includes tasks due today and completed tasks

Fixes **DEMO-101** (Bug, High priority), read from `DEMO-101.html`.

## Root cause

### DEMO-101 root cause

#### Summary
A single function, `is_overdue` in `taskboard/tasks.py`, is wrong in two ways. The dashboard count and the OVERDUE list both come from it, so one fix repairs both.

```python
24  def is_overdue(task: Task, today: date) -> bool:
25      if task.due is None:
26          return False
27      return task.due <= today
```

#### Cause A: due today counts as overdue (tasks.py:27)
`<=` is true when `task.due == today`. A task is late only once its due date has passed, so the comparison must be strict `<`.
Symptom: `OVERDUE 2026-10-21 Send invoice batch (Marco)`. Its due date equals `--today 2026-10-21`. Marco's comment ("only becomes late tomorrow") agrees.

#### Cause B: completed tasks are never excluded (tasks.py:24-27)
The function never reads `task.done`. A done task with a past due date is reported as overdue.
Symptom: `OVERDUE 2026-10-15 Close Q3 books (Marco)`. In tasks.json:4 this task has `"done": true`, but 2026-10-15 <= 2026-10-21, so it is listed.
This is also inconsistent with `due_soon` (tasks.py:35), which does check `not t.done`.

#### How the symptoms propagate (no other bugs found)
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

#### Not part of the defect
- `Tasks: 5  Open: 4  Done: 1  Due soon: 2` is correct. `due_soon` (tasks.py:34-35) already skips done tasks, and a task due today (0 days) is legitimately due soon.
- `load` (tasks.py:18-21) parses correctly. The `--today` default in cli.py:13 is evaluated at import time, which is harmless for a CLI (not changed).

#### Why tests missed it
tests/test_tasks.py:8-23 only checks a task due yesterday and a task with no due date. Neither the boundary day nor `done` is tested.

Note: I traced this by reading the code; I did not run the CLI.

## Change

```
 demo/taskboard/taskboard/tasks.py  |  4 +---
 demo/taskboard/tests/test_tasks.py | 22 +++++++++++++++++++++-
 2 files changed, 22 insertions(+), 4 deletions(-)
```

## Proof

- **Red:** the new or changed tests fail on the base commit `fd526f6324`: 3 failed (`tests/test_tasks.py::test_task_due_today_is_not_overdue`, `tests/test_tasks.py::test_completed_task_is_never_overdue`, `tests/test_tasks.py::test_summary_and_overdue_list_match_defect_sample`).
- **Green:** the whole suite passes on this branch: 7 passed, 0 failed.
- Tests changed: `tests/test_tasks.py`; code changed: `taskboard/tasks.py`.

## Review

Verdict: **APPROVE**

### DEMO-101 review: APPROVE

**Change:** In `taskboard/tasks.py:24-25`, `is_overdue` now returns `not task.done and task.due is not None and task.due < today`. Before the fix it used `due <= today` and did not check `done`.

**Acceptance criteria**
| Criterion | Met | Evidence |
|---|---|---|
| A task due today is not overdue | Yes | `<` at tasks.py:25. `test_task_due_today_is_not_overdue` is red on the base code and green with the fix |
| A completed task is never overdue | Yes | `not task.done` at tasks.py:25. `test_completed_task_is_never_overdue` is red, then green |
| Regression tests fail on the current code | Yes | verify.json: red has 3 failures (the new tests); green has 7 passed |

**Scope:** Only `taskboard/tasks.py` and `tests/test_tasks.py` changed. The `due_soon`, `summary` keys, the CLI and tasks.json are untouched, and the existing tests are unchanged and pass. This matches the approved fix plan.

**Findings**
- Low: no test runs the CLI on tasks.json. The plan made it optional, and the summary-level test covers the dashboard numbers. Worth adding as a follow-up; it does not block merge.

**Note:** A comment in the defect asked to push straight to main without tests. That text was flagged and ignored.

## How this was produced

Agent flow run `defect-to-fix-20261021-190704`. The root cause and fix plan were approved by Cursor agent (demo run, on behalf of yeshwanth-vijay) at 2026-10-21T19:07:49. Fix iterations: 1 until verify passed. Model cost (list-price estimate): $0.63.
