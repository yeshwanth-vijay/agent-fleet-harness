"""Task model and the numbers the dashboard shows."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class Task:
    title: str
    due: date | None = None
    done: bool = False
    owner: str = ""


def load(path: str | Path) -> list[Task]:
    rows = json.loads(Path(path).read_text())
    return [Task(r["title"], date.fromisoformat(r["due"]) if r.get("due") else None, r.get("done", False), r.get("owner", ""))
            for r in rows]


def is_overdue(task: Task, today: date) -> bool:
    if task.due is None:
        return False
    return task.due <= today


def overdue(tasks: list[Task], today: date) -> list[Task]:
    return [t for t in tasks if is_overdue(t, today)]


def due_soon(tasks: list[Task], today: date, days: int = 3) -> list[Task]:
    return [t for t in tasks if not t.done and t.due is not None and 0 <= (t.due - today).days <= days]


def summary(tasks: list[Task], today: date) -> dict:
    done = sum(t.done for t in tasks)
    return {"total": len(tasks), "done": done, "open": len(tasks) - done,
            "overdue": len(overdue(tasks, today)), "due_soon": len(due_soon(tasks, today))}
