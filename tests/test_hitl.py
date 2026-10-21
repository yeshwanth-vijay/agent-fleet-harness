"""The human-in-the-loop gate: holds, decisions, live controls and triggers (no model calls)."""
import json

import pytest

from agents.harness import hitl


class FakeRun:
    def __init__(self, d):
        self.dir, self.state, self.lines = d, {"run_id": "t", "status": "running", "done": [], "seats": {}}, []

    def save(self):
        (self.dir / "state.json").write_text(json.dumps(self.state))

    def log(self, msg):
        self.lines.append(msg)


def plan(d, **kw):
    hitl.set_plan(d, {"mode": "suggested", "checkpoints": [], "triggers": [], **kw})


def test_no_plan_passes(tmp_path):
    run = FakeRun(tmp_path)
    hitl.gate(run, "extract", "before")
    assert run.state["status"] == "running"


def test_blocking_checkpoint_holds_then_approve_passes(tmp_path):
    run = FakeRun(tmp_path)
    plan(tmp_path, checkpoints=[{"id": "cp-1", "phase": "estimate", "when": "before", "who": "Product owner", "why": "check scope"}])
    hitl.gate(run, "extract", "before")  # other phases are not held
    with pytest.raises(SystemExit):
        hitl.gate(run, "estimate", "before")
    assert run.state["status"] == "waiting_approval" and run.state["hitl_pending"]["id"] == "cp-1"
    with pytest.raises(SystemExit):  # resumed without a decision: still held
        hitl.gate(run, "estimate", "before")
    hitl.decide(tmp_path, "cp-1", "approve", "go ahead")
    hitl.gate(run, "estimate", "before")
    assert run.state["status"] == "running" and "hitl_pending" not in run.state


def test_reject_stops_the_run(tmp_path):
    run = FakeRun(tmp_path)
    plan(tmp_path, checkpoints=[{"id": "cp-1", "phase": "deploy", "when": "before", "who": "Release manager", "why": "deploy"}])
    with pytest.raises(SystemExit):
        hitl.gate(run, "deploy", "before")
    hitl.decide(tmp_path, "cp-1", "reject", "not this week")
    with pytest.raises(SystemExit):
        hitl.gate(run, "deploy", "before")
    assert run.state["status"] == "stopped" and "not this week" in run.state["error"]


def test_notice_does_not_hold(tmp_path):
    run = FakeRun(tmp_path)
    plan(tmp_path, checkpoints=[{"id": "n-1", "phase": "write", "when": "after", "who": "QA lead", "why": "skim", "blocking": False}])
    hitl.gate(run, "write", "after")
    hitl.gate(run, "write", "after")
    assert run.state["status"] == "running" and run.state["hitl_noticed"] == ["n-1"]
    assert sum(e["event"] == "notice" for e in hitl.history(tmp_path)) == 1


def test_pause_and_stop_controls(tmp_path):
    run = FakeRun(tmp_path)
    hitl.set_control(tmp_path, pause=True, by="You", reason="lunch")
    with pytest.raises(SystemExit):
        hitl.gate(run, "compare", "before")
    pend = run.state["hitl_pending"]
    assert pend["kind"] == "pause" and not hitl.control(tmp_path)["pause"]
    hitl.decide(tmp_path, pend["id"], "approve")
    hitl.gate(run, "compare", "before")
    hitl.set_control(tmp_path, stop=True, by="You")
    with pytest.raises(SystemExit):
        hitl.gate(run, "compare", "after")
    assert run.state["status"] == "stopped"


def test_triggers_fire_once_in_suggested_mode(tmp_path):
    run = FakeRun(tmp_path)
    plan(tmp_path, triggers=[{"on": "judge_revise", "who": "Tech lead"}, {"on": "budget_80", "who": "You"}])
    run.state["budget"] = {"run_budget_usd": 10, "spent_usd": 2}
    hitl.gate(run, "review", "after")  # nothing fired yet
    run.state["verdict"] = "REVISE"
    with pytest.raises(SystemExit):
        hitl.gate(run, "review", "after")
    hitl.decide(tmp_path, run.state["hitl_pending"]["id"], "approve")
    hitl.gate(run, "finalize", "before")
    hitl.gate(run, "finalize", "after")  # judge_revise fired already; budget still under 80%
    assert run.state["status"] == "running"
    run.state["budget"]["spent_usd"] = 8.5
    with pytest.raises(SystemExit):
        hitl.gate(run, "finalize", "after")
    assert run.state["hitl_pending"]["id"] == "trig-budget_80"


def test_launch_plan_is_copied_from_env(tmp_path, monkeypatch):
    src = tmp_path / "launch-plan.json"
    src.write_text(json.dumps({"mode": "auto", "checkpoints": [], "triggers": []}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("SWARM_HITL_PLAN", str(src))
    assert hitl.plan(run_dir)["mode"] == "auto" and (run_dir / "hitl.json").exists()
