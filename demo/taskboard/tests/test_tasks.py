from datetime import date

from taskboard.tasks import Task, due_soon, is_overdue, summary

TODAY = date(2026, 10, 21)


def test_task_due_yesterday_is_overdue():
    assert is_overdue(Task("a", date(2026, 10, 20)), TODAY)


def test_task_without_due_date_is_never_overdue():
    assert not is_overdue(Task("a"), TODAY)


def test_due_soon_counts_open_tasks_in_the_next_three_days():
    tasks = [Task("a", date(2026, 10, 23)), Task("b", date(2026, 10, 30)), Task("c", date(2026, 10, 22), done=True)]
    assert [t.title for t in due_soon(tasks, TODAY)] == ["a"]


def test_summary_counts_total_open_and_done():
    s = summary([Task("a"), Task("b", done=True)], TODAY)
    assert (s["total"], s["open"], s["done"]) == (2, 1, 1)
