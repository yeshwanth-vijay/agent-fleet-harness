# DEMO-101 review: APPROVE

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
