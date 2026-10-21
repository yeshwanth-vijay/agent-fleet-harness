# agent-fleet-harness

A small harness for running fleets of Claude agents with the safety rails you need before you trust their output: per-seat permissions, a cost governor, semantic compaction, prompt-injection quarantine, OpenTelemetry-style telemetry, checkpoint/resume, human approval gates, and evals that a model cannot talk its way past.

Every agent seat is **Claude Code in headless mode** (`claude -p`) on your Claude login. No API keys are read; `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` and `GOOGLE_API_KEY` are stripped from every child process.

## What's in it

```
agents/harness/claude_runner.py   one seat = one `claude -p` run: tool allowlist, --json-schema, --max-budget-usd, --max-turns
agents/harness/budget.py          deterministic cost governor across seats (downgrade a tier at 80%, skip optional seats at 100%)
agents/harness/security.py        quarantines web content in <untrusted_content> and neutralises injection patterns
agents/harness/compaction.py      semantic compaction of hand-offs between seats (facts and source URLs kept, never cut)
agents/harness/telemetry.py       OTel GenAI-convention spans to runs/<id>/telemetry.jsonl
agents/harness/hitl.py            human in the loop: pause, stop, blocking checkpoints, triggers
agents/fleet/                     research fleet: LangGraph supervisor, planner → parallel research seats → synthesizer
                                  → editor → critic (≤2 revisions) → deterministic fact-check → judge (REJECT → replan ≤1)
agents/flows/engine.py            staged flows against any git repo: agent / prepare / command / gate stages,
                                  approval gates, re-run from any stage, loops with feedback
evals/                            deterministic citation checks, per-section judging, rubric, golden set with 95% CI,
                                  position-swapped pairwise comparison, go/no-go regression gate
config/                           fleet roster (fleet_config.yaml), versioned seat prompts, flow definitions
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
claude auth status        # must show your Claude login (claude.ai), not an API key
pytest -q
```

## Research fleet

```bash
python run_fleet.py --task caching-strategies          # a golden task from evals/golden_tasks.json
python run_fleet.py --query "..." --budget 6           # ad-hoc question, $6 cap
python run_fleet.py --resume <run_id>                  # continue from the last checkpoint
```

Output lands in `runs/fleet/<run_id>/`: `article.md`, `run.json` (cost, roster, revisions, judge verdict, citation report), `seen_urls.json` and `telemetry.jsonl`.

## Flows

A flow is a JSON file in `config/flows/` that lists stages in order. Each stage is run by one team:

| Kind | What it does |
|------|--------------|
| `agent` | A `claude -p` seat. Read-only unless `"writes": "wt"`, which lets it edit the run's git worktree. Returns its deliverables as files. |
| `prepare` | Creates a git worktree of the target repo on a new branch, so every change is a normal reviewable diff. |
| `command` | Runs a fixed command (tests, a build, a script) and keeps its output. |
| `gate` | Runs only after a person approves it, and records who approved and when. |

Any stage can set `"approval": true` to wait for a person, and `"loop"` to send work back to an earlier stage until a check passes.

```bash
python run_flow.py --flow <id> --repo <path> [--input name=path ...]
python run_flow.py --resume <run_id> --approve <stage>
python run_flow.py --resume <run_id> --rerun <stage>
```

## Defect to fix

[![Intro video: Jira defect to reviewed fix](docs/media/defect-to-fix-intro.png)](docs/media/defect-to-fix-intro.mp4)

A flow that takes a defect from an HTML page (a Jira replacement: no tracker is contacted or changed) to a pull request. First it finds the root cause and waits for you to approve the plan. Then it writes regression tests and the fix on its own branch, proves the tests fail on the original code and pass on the fix, and has an independent reviewer check every acceptance criterion. Pass and fail are decided by code, and nothing is pushed unless you ask.

```bash
python run_flow.py --flow defect-to-fix --repo demo/taskboard --input defect=demo/jira-replacement/DEMO-101.html
python run_flow.py --resume <run_id> --approve approve-plan --by "your name"
```

How to use it, the page format and the guardrails: [docs/defect-to-fix.md](docs/defect-to-fix.md). A full example run, which cost $0.63: [docs/example-run/DEMO-101](docs/example-run/DEMO-101/).

## License

MIT
