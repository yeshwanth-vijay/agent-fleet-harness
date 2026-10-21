"""
agents/flows/engine.py — user-defined flows: an ordered list of stages, each run by one team.

Modelled on GitHub Actions: a flow file (config/flows/<id>.json) is the workflow; a run executes its stages
in order; a stage with "approval": true waits for a person (like an environment with required reviewers);
any stage can be re-run, which resets it and everything after it (like "re-run failed jobs" with its
dependents). Every stage keeps a session log: the team's own account of its steps (what, why, evidence), the
exact tool calls it made, its output files, cost and time.

Stage kinds
  agent    a `claude -p` seat. Read-only unless "writes": "wt" (the run's git worktree). It returns its
           deliverables as files, which the engine writes into the stage folder.
  prepare  creates a git worktree of the target repo on a new branch, so every change the flow makes is an
           ordinary, reviewable diff against the commit the run started from.
  command  runs a fixed command (tests, a build, a script), output kept in the stage folder.
  gate     a pure decision point: runs only after approval and records who decided.

Placeholders in prompts, commands, cwd and branch names:
  {repo} target repo · {wt} worktree · {branch} · {run} run folder · {stage} this stage's folder
  {harness} this repo · {goal} · {input:<name>} a file passed with --input name=path

Loops: a stage with "loop": {"back_to": "<stage>", "max": 3, "until": {"file": "verify.json", "field": "status",
"equals": "pass"}} checks its own output after it finishes. If the check fails and fewer than `max` iterations
have run, the stages from back_to through this one are reset and run again; the failing output is handed to
them as loop feedback. Approval gates inside the loop must be approved again on every iteration, unless the gate
says "approval_scope": "flow" (approved once for the whole run). After `max` iterations the run stops as failed.

  python3 run_flow.py --flow <id> --repo <path> [--input name=path ...]
  python3 run_flow.py --resume <run> [--approve <stage>] [--rerun <stage>]
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from agents.harness import hitl, telemetry
from agents.harness.claude_runner import run_seat

ROOT = Path(__file__).resolve().parents[2]
FLOWS = ROOT / "config" / "flows"
RUNS = Path(os.getenv("FLOW_RUNS_DIR", ROOT / "runs" / "flows"))

RESULT_SCHEMA = {"type": "object", "required": ["summary", "steps", "files"], "properties": {
    "summary": {"type": "string"},
    "steps": {"type": "array", "items": {"type": "object", "required": ["what", "why"], "properties": {
        "what": {"type": "string"}, "why": {"type": "string"}, "evidence": {"type": "string"}}}},
    "files": {"type": "array", "items": {"type": "object", "required": ["name", "content"], "properties": {
        "name": {"type": "string"}, "content": {"type": "string"}}}},
    "open_questions": {"type": "array", "items": {"type": "string"}},
    "risks": {"type": "array", "items": {"type": "string"}}}}

SYSTEM = """You are one team in a multi-team delivery flow. Earlier teams' outputs are files you can read; later teams
build on yours, so be concrete and cite sources (file:line, test name, log line, document section).
Return JSON: summary (3-6 sentences), steps (the steps you actually took, in order: what you did, why, and the
evidence or file that backs it), files (your deliverables: name + full content; Markdown unless the task asks
for another format), open_questions, risks. Do not invent facts; say what you could not establish."""


def load_flow(flow_id: str) -> dict:
    if not re.fullmatch(r"[a-z0-9-]{1,60}", flow_id):
        raise ValueError(f"bad flow id {flow_id!r}")
    return json.loads((FLOWS / f"{flow_id}.json").read_text())


class FlowRun:
    def __init__(self, run_id: str | None = None, flow_id: str | None = None, repo: str | None = None,
                 inputs: dict[str, str] | None = None):
        if run_id:
            self.dir = RUNS / run_id
            self.state = json.loads((self.dir / "state.json").read_text())
        else:
            flow = load_flow(flow_id)
            if not repo:
                raise ValueError(f"flow {flow_id} needs a target repo: pass --repo <path>")
            missing = [n for n in flow.get("inputs", []) if n not in (inputs or {})]
            if missing:
                raise ValueError(f"flow {flow_id} needs --input for: {', '.join(missing)}")
            run_id = f"{flow['id']}-{datetime.now():%Y%m%d-%H%M%S}"
            self.dir = RUNS / run_id
            self.dir.mkdir(parents=True, exist_ok=True)
            self.state = {"run_id": run_id, "flow": flow, "status": "running", "created": datetime.now().isoformat(timespec="seconds"),
                          "repo": str(Path(repo).expanduser().resolve()),
                          "inputs": {k: str(Path(v).expanduser().resolve()) for k, v in (inputs or {}).items()},
                          "approvals": {}, "workspace": {},
                          "stages": {s["id"]: {"status": "pending"} for s in flow["stages"]}, "done": [], "seats": {}}
        self.run_id = self.state["run_id"]
        self.flow = self.state["flow"]
        self.repo = Path(self.state["repo"])
        telemetry.configure(str(self.dir / "telemetry.jsonl"))

    # ── bookkeeping ──────────────────────────────────────────────────────────
    def save(self):
        st = self.state
        st["done"] = [s["id"] for s in self.flow["stages"] if st["stages"][s["id"]]["status"] == "done"]
        st["timings"] = {k: v.get("duration_s") for k, v in st["stages"].items() if v.get("duration_s") is not None}
        st["budget"] = {"run_budget_usd": self.flow.get("budget_usd", 30),
                        "spent_usd": round(sum(v.get("cost_usd", 0) for v in st["stages"].values()), 4)}
        (self.dir / "state.json").write_text(json.dumps(st, indent=2, default=str))

    def log(self, msg: str):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with open(self.dir / "progress.log", "a") as fh:
            fh.write(line + "\n")

    def fmt(self, s: str, sid: str = "") -> str:
        ws = self.state["workspace"]
        rep = {"{repo}": str(self.repo), "{run}": str(self.dir), "{stage}": str(self.dir / "stages" / sid),
               "{harness}": str(ROOT), "{wt}": ws.get("wt", "(not created yet)"), "{branch}": ws.get("branch", ""),
               "{goal}": self.flow["goal"]}
        for k, v in rep.items():
            s = s.replace(k, v)
        return re.sub(r"\{input:([A-Za-z0-9_-]+)\}", lambda m: self.state["inputs"].get(m.group(1), m.group(0)), s)

    def sh(self, cmd: list[str], cwd: Path, timeout=1800, check=True) -> subprocess.CompletedProcess:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:5])} failed ({p.returncode}): {(p.stderr or p.stdout)[-600:]}")
        return p

    # ── control ──────────────────────────────────────────────────────────────
    def approve(self, sid: str, who: str = "you (terminal)"):
        self.state["approvals"][sid] = {"by": who, "at": datetime.now().isoformat(timespec="seconds")}
        self.log(f"approved {sid} ({who})")
        self.save()

    def rerun(self, sid: str):
        ids = [s["id"] for s in self.flow["stages"]]
        if sid not in ids:
            raise ValueError(f"no stage {sid}")
        for later in ids[ids.index(sid):]:
            self.state["stages"][later] = {"status": "pending", "previous": self.state["stages"][later]}
            if later != sid:  # downstream gates must be approved again
                self.state["approvals"].pop(later, None)
        self.state["approvals"].setdefault(sid, {"by": "re-run request", "at": datetime.now().isoformat(timespec="seconds")})
        self.log(f"re-run from {sid}: {', '.join(ids[ids.index(sid):])} reset")
        self.save()

    def go(self) -> dict:
        self.state["status"] = "running"
        self.save()
        for stage in self.flow["stages"]:
            sid = stage["id"]
            st = self.state["stages"][sid]
            if st["status"] == "done":
                continue
            if stage.get("approval") and sid not in self.state["approvals"]:
                st["status"] = "waiting"
                self.state["status"] = "waiting_approval"
                self.state["waiting_for"] = sid
                self.log(f"⏸ {sid} waits for approval: {stage.get('approval_label', 'approve to continue')}")
                self.save()
                return self.state
            try:  # human in the loop: pause, stop, checkpoints and triggers (agents/harness/hitl.py)
                hitl.gate(self, sid, "before")
            except SystemExit:
                return self.state
            self.state.pop("waiting_for", None)
            st.update(status="running", started=datetime.now().isoformat(timespec="seconds"), error=None)
            self.save()
            self.log(f"▶ {sid} ({stage['team']})")
            t0 = time.time()
            try:
                getattr(self, f"stage_{stage['kind']}")(stage)
                st.update(status="done", duration_s=round(time.time() - t0, 1), ended=datetime.now().isoformat(timespec="seconds"))
                self.log(f"✓ {sid} ({time.time() - t0:.0f}s)")
            except Exception as exc:  # noqa: BLE001
                st.update(status="failed", error=f"{type(exc).__name__}: {exc}", duration_s=round(time.time() - t0, 1))
                self.state["status"] = "failed"
                self.state["error"] = f"{sid}: {exc}"
                self.log(f"✗ failed: {sid}: {exc}  (fix, then re-run from {sid})")
                self.save()
                return self.state
            self.save()
            if stage.get("loop"):
                again = self._loop(stage)
                if again is not None:
                    return again
            try:
                hitl.gate(self, sid, "after")
            except SystemExit:
                return self.state
        self.state["status"] = "completed"
        self.state.pop("error", None)
        self.log("flow completed")
        self.save()
        return self.state

    def _loop(self, stage: dict) -> dict | None:
        """After a looping stage: None to carry on, or the state of the next iteration / the stopped run."""
        sid, lp = stage["id"], stage["loop"]
        until = lp.get("until", {})
        f = self.dir / "stages" / sid / until.get("file", "verify.json")
        try:
            value = json.loads(f.read_text()).get(until.get("field", "status"))
        except (OSError, ValueError):
            value = None
        loops = self.state.setdefault("loops", {})
        n = loops.get(sid, 0) + 1                          # iterations finished so far, this one included
        loops[sid] = n
        if value == until.get("equals", "pass"):
            self.log(f"↻ {sid}: passed on iteration {n}")
            self.save()
            return None
        if n >= lp.get("max", 3):
            self.state["status"] = "failed"
            self.state["error"] = f"{sid}: not passing after {n} iterations ({until.get('field', 'status')} = {value!r})"
            self.log(f"✗ {sid}: loop ended after {n} iterations without passing; see stages/{sid}/{f.name}")
            self.save()
            return self.state
        ids = [s["id"] for s in self.flow["stages"]]
        back = lp["back_to"]
        span = ids[ids.index(back):ids.index(sid) + 1]
        fb = self.dir / "loop-feedback.md"
        fb.write_text(f"# Loop feedback (iteration {n} of at most {lp.get('max', 3)})\n\n"
                      f"Stage `{sid}` did not pass: {until.get('field', 'status')} = {value!r}.\n\n"
                      f"Its output:\n\n```\n{f.read_text() if f.exists() else '(missing)'}\n```\n")
        keep = {s["id"] for s in self.flow["stages"] if s.get("approval_scope") == "flow"}
        for later in span:
            self.state["stages"][later] = {"status": "pending", "previous": self.state["stages"][later]}
            if later not in keep:
                self.state["approvals"].pop(later, None)
        self.log(f"↻ {sid}: did not pass ({value!r}); iteration {n + 1} from {back}: {', '.join(span)} reset")
        self.save()
        return self.go()

    # ── stage kinds ──────────────────────────────────────────────────────────
    def _context(self, stage: dict) -> str:
        sid = stage["id"]
        lines = [f"Flow goal: {self.flow['goal']}", f"Target repo (read): {self.repo}",
                 f"Your stage folder (outputs are written here for you): {self.dir / 'stages' / sid}"]
        for name, path in self.state["inputs"].items():
            lines.append(f"Input '{name}': {path}")
        prev = []
        for s in self.flow["stages"]:
            if s["id"] == sid:
                break
            d = self.dir / "stages" / s["id"]
            if self.state["stages"][s["id"]]["status"] == "done" and d.exists():
                files = [str(f) for f in sorted(d.iterdir()) if f.is_file() and f.name not in ("result.json", "trace.json", "prompt.md")]
                prev.append(f"- {s['name']} ({s['team']}): " + ", ".join(files))
        if prev:
            lines.append("Outputs of earlier stages (read them):\n" + "\n".join(prev))
        if self.state["workspace"]:
            lines.append("Workspace: " + json.dumps(self.state["workspace"]))
        if (self.dir / "loop-feedback.md").exists() and self.state.get("loops"):
            lines.append(f"This is a repeat iteration. Read the loop feedback first and fix what it reports: {self.dir / 'loop-feedback.md'}")
        return "\n".join(lines)

    def stage_agent(self, stage: dict):
        sid = stage["id"]
        out = self.dir / "stages" / sid
        out.mkdir(parents=True, exist_ok=True)
        writes = stage.get("writes")
        if writes and writes != "wt":
            raise ValueError(f"stage {sid}: \"writes\" must be \"wt\" (the run's worktree)")
        cwd = Path(self.state["workspace"]["wt"]) if writes else self.dir
        if writes and not cwd.exists():
            raise RuntimeError("worktree missing; run the prepare stage first")
        tools = list(stage.get("tools", ["Read", "Grep", "Glob"]))
        prompt = (self._context(stage) + "\n\nYour task:\n" + self.fmt(stage["prompt"], sid) +
                  f"\n\nDeliverables to return in files: {', '.join(stage.get('outputs', []))}. Return the JSON.")
        (out / "prompt.md").write_text(prompt)
        reads = [str(self.dir), str(self.repo)] + [os.path.dirname(p) for p in self.state["inputs"].values()]
        if self.state["workspace"].get("wt"):
            reads.append(self.state["workspace"]["wt"])
        r = run_seat(sid, prompt, model=stage.get("model", "sonnet"), system=SYSTEM, temperature=stage.get("temperature", 0.3),
                     tools=tools, json_schema=RESULT_SCHEMA, max_turns=stage.get("max_turns", 60),
                     budget_usd=stage.get("budget_usd", 3.0), cwd=str(cwd), add_dirs=sorted(set(reads)),
                     run_id=self.run_id, timeout_s=stage.get("timeout_s", 3600), allow_writes=bool(writes))
        st = self.state["stages"][sid]
        st.update(cost_usd=round(r.cost_usd, 4), turns=r.turns, model=r.model, tool_calls=len(r.tool_calls), seat_error=r.error)
        self.state["seats"][sid] = {"model": r.model, "cost_usd": r.cost_usd, "turns": r.turns, "duration_s": r.duration_s, "error": r.error}
        (out / "trace.json").write_text(json.dumps(r.tool_calls, indent=1))
        res = r.structured or {}
        (out / "result.json").write_text(json.dumps(res, indent=2))
        problems = []
        for f in res.get("files", []):
            name = re.sub(r"[^A-Za-z0-9._-]", "-", Path(f.get("name", "output.md")).name)[:80] or "output.md"
            (out / name).write_text(f.get("content", ""))
            if name.endswith(".html"):
                problems += [f"{name}: {p}" for p in self._check_html(f.get("content", ""))]
        if problems:  # a deliverable that does not run must not look finished
            st["warnings"] = problems
            self.log(f"  ⚠ {sid}: " + "; ".join(problems))
        if writes:  # the change itself: diff of the worktree against its last commit, then committed
            diff = self.sh(["git", "diff", "--stat"], cwd, check=False).stdout + "\n" + self.sh(["git", "diff"], cwd, check=False).stdout
            (out / "changes.diff").write_text(diff)
            self.sh(["git", "add", "-A"], cwd, check=False)
            self.sh(["git", "-c", "user.name=Agent flow", "-c", "user.email=agent-flow@localhost", "commit", "-q", "-m",
                     f"flow {self.run_id}: {stage['name']}"], cwd, check=False)
        self.log(f"  {sid}: ${r.cost_usd:.2f}, {r.turns} turns, {len(r.tool_calls)} tool calls{' · ' + r.error if r.error else ''}")
        if not res and r.error:
            raise RuntimeError(f"team returned nothing: {r.error}")

    def _check_html(self, html: str) -> list[str]:
        """Syntax-check every inline <script> with `node --check` (agents' mockups have shipped broken scripts)."""
        node = shutil.which("node")
        if not node:
            return []
        out = []
        for i, js in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)):
            f = self.dir / f".check-{i}.js"
            f.write_text(js)
            p = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            f.unlink()
            if p.returncode != 0:
                msg = next((l for l in p.stderr.splitlines() if "Error" in l), p.stderr.strip()[-200:])
                out.append(f"script {i + 1} does not parse ({msg.strip()})")
        return out

    def stage_prepare(self, stage: dict):
        """Worktree of the repo that contains the target, on a new branch; {wt} points at the target inside it."""
        out = self.dir / "stages" / stage["id"]
        out.mkdir(parents=True, exist_ok=True)
        top = Path(self.sh(["git", "rev-parse", "--show-toplevel"], self.repo).stdout.strip())
        rel = self.repo.relative_to(top)
        branch = self.fmt(stage.get("branch", "flow/{run_id}").replace("{run_id}", self.run_id), stage["id"])
        wt_root = self.dir / "wt"
        notes = []
        if not wt_root.exists():
            base = self.sh(["git", "rev-parse", "--short", "HEAD"], top).stdout.strip()
            self.sh(["git", "worktree", "add", "-q", "-b", branch, str(wt_root), "HEAD"], top)
            notes.append(f"worktree {wt_root} on branch {branch} from {top.name}@{base}")
        self.state["workspace"] = {"wt": str(wt_root / rel), "wt_root": str(wt_root), "branch": branch, "repo_root": str(top)}
        (out / "workspace.md").write_text("# Workspace\n\n" + "\n".join(f"- {n}" for n in notes) + "\n")
        self.state["stages"][stage["id"]]["summary"] = "; ".join(notes) or "worktree already present"
        self.log("  " + ("; ".join(notes) or "worktree already present"))

    def stage_gate(self, stage: dict):
        """A pure decision point: it runs only after approval and records who decided what."""
        out = self.dir / "stages" / stage["id"]
        out.mkdir(parents=True, exist_ok=True)
        a = self.state["approvals"].get(stage["id"], {})
        (out / "decision.md").write_text(f"# Decision\n\n{stage.get('approval_label', 'Approved')}\n\n- by: {a.get('by')}\n- at: {a.get('at')}\n")
        self.state["stages"][stage["id"]]["summary"] = f"approved by {a.get('by')} at {a.get('at')}"

    def stage_command(self, stage: dict):
        out = self.dir / "stages" / stage["id"]
        out.mkdir(parents=True, exist_ok=True)
        cmd = [self.fmt(a, stage["id"]) for a in stage["command"]]   # {stage} is this stage's folder
        cwd = Path(self.fmt(stage.get("cwd", str(self.dir)), stage["id"]))
        p = self.sh(cmd, cwd, timeout=stage.get("timeout_s", 3600), check=False)
        (out / "command.log").write_text(f"$ {' '.join(cmd)}\n(cwd {cwd})\n\n{p.stdout}\n{p.stderr}")
        self.state["stages"][stage["id"]]["summary"] = f"exit {p.returncode}: {' '.join(cmd[:4])} …"
        if p.returncode != 0 and not stage.get("allow_failure"):
            raise RuntimeError(f"command exited {p.returncode}; see stages/{stage['id']}/command.log")
