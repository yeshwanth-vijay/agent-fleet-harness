#!/usr/bin/env python3
"""
run_flow.py — run a user-defined flow (config/flows/<id>.json) against a target repo: ordered stages, one team
each, with approval gates, re-run from any stage, and a session log per stage.

  python3 run_flow.py --flow <id> --repo <path> [--input name=path ...]
  python3 run_flow.py --resume <run_id> --approve <stage>     # approve a waiting stage and continue
  python3 run_flow.py --resume <run_id> --rerun <stage>       # reset a stage and everything after it, then continue

No API keys are used: every agent stage is `claude -p` on your Claude login.
"""
import argparse
import json
import os
import sys

for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(k, None)

from agents.flows.engine import FlowRun  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--flow")
    g.add_argument("--resume")
    ap.add_argument("--repo", help="target repository (or a folder inside one)")
    ap.add_argument("--input", action="append", default=[], metavar="NAME=PATH", help="a file the flow reads")
    ap.add_argument("--approve", action="append", default=[], help="stage id to approve before continuing")
    ap.add_argument("--rerun", help="stage id to reset (with everything after it) before continuing")
    ap.add_argument("--by", default="you (terminal)", help="who is approving, as recorded in the run")
    a = ap.parse_args()
    inputs = dict(i.split("=", 1) for i in a.input)
    run = FlowRun(a.resume, a.flow, a.repo, inputs)
    if a.rerun:
        run.rerun(a.rerun)
    for s in a.approve:
        run.approve(s, a.by)
    st = run.go()
    print(json.dumps({k: st.get(k) for k in ("run_id", "status", "waiting_for", "error")}, indent=2))
    return 0 if st["status"] in ("completed", "waiting_approval") else 2


if __name__ == "__main__":
    sys.exit(main())
