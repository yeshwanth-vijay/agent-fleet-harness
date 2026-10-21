"""Deterministic half of the defect-to-fix flow: intake, red/green verify, PR gate, engine variables (no model calls)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agents.defectfix import intake, pr, verify
from agents.flows import engine

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "demo" / "jira-replacement" / "DEMO-101.html"


def test_intake_reads_the_demo_defect():
    d = intake.parse(PAGE.read_text(), PAGE.name)
    assert d["key"] == "DEMO-101"
    assert d["summary"].startswith("Dashboard overdue count")
    assert (d["type"], d["priority"], d["components"]) == ("Bug", "High", "taskboard")
    assert len(d["steps"]) == 3 and len(d["acceptance"]) == 3
    assert "Overdue: 3" in d["actual"]


def test_intake_quarantines_and_flags_instructions_in_comments():
    d = intake.parse(PAGE.read_text(), PAGE.name)
    assert [f["field"] for f in d["injection_flags"]] == ["comments"]
    md = intake.to_markdown(d)
    assert "<untrusted_content" in md
    assert "Ignore all previous instructions" not in md and "[removed: instruction-like text]" in md


def test_intake_falls_back_to_title_and_body_without_markup():
    d = intake.parse("<html><head><title>[ABC-7] Login fails on Safari</title></head><body><p>Click login.</p></body></html>")
    assert (d["key"], d["summary"]) == ("ABC-7", "Login fails on Safari")
    assert "Click login." in d["description"]


def test_intake_cli_rejects_a_page_that_is_not_a_defect(tmp_path):
    page = tmp_path / "x.html"
    page.write_text("<html><body>hello</body></html>")
    assert intake.main([str(page), str(tmp_path / "out")]) == 2


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "app"
    (r / "tests").mkdir(parents=True)
    (r / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (r / "calc.py").write_text("def is_adult(age):\n    return age > 18\n")
    (r / "tests" / "test_calc.py").write_text("from calc import is_adult\n\ndef test_child():\n    assert not is_adult(10)\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "base", "--allow-empty")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return r, _git(r, "rev-parse", "HEAD")


def _commit(r, msg):
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)


CMD = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def test_verify_passes_a_real_fix_with_a_reproducing_test(repo):
    r, base = repo
    (r / "calc.py").write_text("def is_adult(age):\n    return age >= 18\n")
    (r / "tests" / "test_calc.py").write_text((r / "tests" / "test_calc.py").read_text() + "\ndef test_eighteen():\n    assert is_adult(18)\n")
    _commit(r, "fix")
    out = verify.verify(r, base, CMD)
    assert out["status"] == "pass", out["reasons"]
    assert out["red"]["exit_code"] == 1 and out["green"]["exit_code"] == 0


def test_verify_fails_a_test_that_passes_on_the_old_code(repo):
    r, base = repo
    (r / "calc.py").write_text("def is_adult(age):\n    return age >= 18\n")
    (r / "tests" / "test_calc.py").write_text((r / "tests" / "test_calc.py").read_text() + "\ndef test_thirty():\n    assert is_adult(30)\n")
    _commit(r, "weak test")
    out = verify.verify(r, base, CMD)
    assert out["status"] == "fail"
    assert any("do not reproduce the defect" in x for x in out["reasons"])


def test_verify_fails_a_code_change_without_tests(repo):
    r, base = repo
    (r / "calc.py").write_text("def is_adult(age):\n    return age >= 18\n")
    _commit(r, "no tests")
    out = verify.verify(r, base, CMD)
    assert out["status"] == "fail"
    assert any("no test file" in x for x in out["reasons"])


def test_pr_refuses_without_an_approving_review(tmp_path):
    (tmp_path / "stages" / "verify").mkdir(parents=True)
    (tmp_path / "stages" / "verify" / "verify.json").write_text(json.dumps({"status": "pass"}))
    (tmp_path / "state.json").write_text(json.dumps({"run_id": "r", "workspace": {}}))
    _, problems = pr.build(tmp_path)
    assert problems == ["the reviewer did not approve (verdict None)"]


def test_engine_publishes_vars_and_names_the_branch_from_them(tmp_path, monkeypatch, repo):
    r, base = repo
    flows = tmp_path / "flows"
    flows.mkdir()
    script = "import json,sys; open(sys.argv[1] + '/vars.json', 'w').write(json.dumps({'slug': 'abc-7'}))"
    (flows / "t.json").write_text(json.dumps({"id": "t", "name": "t", "goal": "g", "stages": [
        {"id": "intake", "name": "i", "team": "bot", "kind": "command", "command": ["{python}", "-c", script, "{stage}"]},
        {"id": "prepare", "name": "p", "team": "bot", "kind": "prepare", "branch": "fix/{var:slug}"}]}))
    monkeypatch.setattr(engine, "FLOWS", flows)
    monkeypatch.setattr(engine, "RUNS", tmp_path / "runs")
    st = engine.FlowRun(None, "t", str(r), {}).go()
    assert st["status"] == "completed", st.get("error")
    assert st["workspace"]["branch"] == "fix/abc-7" and st["workspace"]["base"] == base
    assert _git(r, "branch", "--list", "fix/abc-7")


def test_defect_flow_file_is_well_formed():
    flow = engine.load_flow("defect-to-fix")
    kinds = {s["kind"] for s in flow["stages"]}
    assert kinds <= {"agent", "prepare", "command", "gate"}
    assert all(hasattr(engine.FlowRun, f"stage_{k}") for k in kinds)
    gate = next(s for s in flow["stages"] if s["id"] == "approve-plan")
    assert gate["approval"] is True
    writers = [s["id"] for s in flow["stages"] if s.get("writes")]
    assert writers == ["fix"]


def test_fleet_finalize_fails_closed_without_an_approving_judge():
    from agents.fleet.orchestrator import finalize
    assert finalize({"draft": "x", "judge": {"verdict": "UNAVAILABLE"}})["status"] == "needs_review"
    assert finalize({"draft": "x", "judge": {"verdict": "REJECT"}})["status"] == "needs_review"
    assert finalize({"draft": "x", "judge": {"verdict": "APPROVE"}})["status"] == "completed"
