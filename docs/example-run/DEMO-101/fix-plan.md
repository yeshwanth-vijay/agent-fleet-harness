# DEMO-101 fix plan

## Change (one function)
`taskboard/tasks.py`, `is_overdue` (lines 24-27):

```python
def is_overdue(task: Task, today: date) -> bool:
    return not task.done and task.due is not None and task.due < today
```

`overdue()` and `summary()` need no edit because they call `is_overdue`. `cli.py` needs no edit.

## Regression tests (add to tests/test_tasks.py)
Each must fail on the current code and pass after the fix.

1. `test_task_due_today_is_not_overdue`: `assert not is_overdue(Task("a", TODAY), TODAY)`.
   - Acceptance: a task due today is not overdue.
   - Fails now: `<=` returns True.
2. `test_completed_task_is_never_overdue`: `assert not is_overdue(Task("a", date(2026, 10, 15), done=True), TODAY)`.
   - Acceptance: a completed task is never overdue.
   - Fails now: no `done` check.
3. `test_summary_and_overdue_list_match_defect_sample`: use tasks equivalent to tasks.json, with the invoice due today and Q3 books done and late. Assert `summary(...)["overdue"] == 1` and `[t.title for t in overdue(...)] == ["Renew TLS certificate"]`.
   - Covers the dashboard end to end.
   - Fails now: gives 3.
4. Optional CLI check: run `main()` on tasks.json with `--today 2026-10-21` via capsys and assert `Overdue: 1` is in the output.
   - Fails now.
   - Keep it only if it is cheap.

Existing tests stay as they are: yesterday is overdue, no due date is not overdue, `due_soon`, and the summary counts. They must still pass.

## Must not change
- `due_soon` behaviour: a task due today stays in Due soon, and done tasks stay excluded.
- `Task`, `load`, the CLI output format, and the `summary` keys.
- tasks.json sample data, because it is the reproduction input.
- The existing tests.

## Risks
- Anyone who relied on due-today counting as overdue will see a lower count. This is the intended behaviour per the acceptance criteria.
- A task can appear in Due soon (due today) without appearing in Overdue. This is intended.
- Timezone: `today` is passed in, so the fix does not touch time handling. The `date.today()` default in cli.py:13 is out of scope.
- Verification: run pytest on the base commit with only the new tests applied, to confirm red, then run again with the fix, to confirm green.
