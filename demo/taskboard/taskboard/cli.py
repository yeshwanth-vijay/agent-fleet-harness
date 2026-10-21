"""python -m taskboard.cli tasks.json [--today YYYY-MM-DD] — print the dashboard."""
from __future__ import annotations

import argparse
from datetime import date

from taskboard.tasks import load, overdue, summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    a = ap.parse_args()
    tasks = load(a.file)
    s = summary(tasks, a.today)
    print(f"Tasks: {s['total']}  Open: {s['open']}  Done: {s['done']}  Overdue: {s['overdue']}  Due soon: {s['due_soon']}")
    for t in overdue(tasks, a.today):
        print(f"  OVERDUE  {t.due}  {t.title} ({t.owner or 'unassigned'})")


if __name__ == "__main__":
    main()
