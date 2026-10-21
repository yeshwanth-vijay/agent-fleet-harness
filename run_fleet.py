#!/usr/bin/env python3
"""
run_fleet.py — run the research fleet on a golden task or an ad-hoc query, on the Claude login.

  python run_fleet.py --task caching-strategies
  python run_fleet.py --query "..." --budget 6
  python run_fleet.py --resume <run_id>
  FLEET_CRASH_AFTER=synthesize python run_fleet.py --task ...   # crash test

No API keys are used: every seat is `claude -p` and ANTHROPIC_API_KEY is stripped.
"""
import argparse
import json
import os
import sys

for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(k, None)

from agents.fleet.orchestrator import run  # noqa: E402
from evals.golden_set import load_tasks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--task")
    g.add_argument("--query")
    g.add_argument("--resume")
    ap.add_argument("--budget", type=float)
    ap.add_argument("--min-words", type=int)
    ap.add_argument("--max-words", type=int)
    a = ap.parse_args()
    task = None
    if a.task:
        task = load_tasks()[a.task]
    elif a.query:
        task = {"task_id": "adhoc", "query": a.query}
    meta = run(task, resume=a.resume, budget_usd=a.budget, min_words=a.min_words, max_words=a.max_words)
    print(json.dumps({k: meta[k] for k in ("run_id", "status", "cost_usd", "wall_time_s", "roster", "revisions", "replans")}, indent=2))
    return 0 if meta["status"] == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
