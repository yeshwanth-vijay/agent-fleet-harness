"""
agents/defectfix/verify.py — deterministic red/green check of a fix branch. No model decides whether a fix works.

  green  the whole test suite passes on the fix branch (HEAD).
  red    the test files the fix added or changed FAIL on the base commit, with the fix branch's tests copied over
         the old code. A regression test that passes on the old code does not reproduce the defect.
  diff   the branch changes at least one test file and at least one non-test file.

Writes verify.json (status "pass" only when all three hold, plus the reasons when not), verify.log (full output)
and branch.diff (everything the branch changed since the base commit).

  python -m agents.defectfix.verify --repo <worktree target> --base <commit> --out <dir> [--test-cmd "python -m pytest -q"]
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def is_test(path: str) -> bool:
    name = Path(path).name
    return "tests/" in f"/{path}" or name.startswith("test_") or name.endswith("_test.py")


def _run(cmd: list[str], cwd: Path) -> dict:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
    out = p.stdout + p.stderr
    count = lambda word: int(m.group(1)) if (m := re.search(rf"(\d+) {word}", out)) else 0  # noqa: E731
    return {"cmd": " ".join(cmd), "exit_code": p.returncode, "passed": count("passed"), "failed": count("failed"),
            "errors": count("error"), "failed_tests": re.findall(r"^FAILED (\S+)", out, re.M), "output": out}


def verify(repo: Path, base: str, test_cmd: list[str]) -> dict:
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout  # noqa: E731
    changed = [f for f in git("diff", "--name-only", "--relative", base, "HEAD").splitlines() if f.strip()]
    tests = [f for f in changed if is_test(f) and (repo / f).exists()]
    source = [f for f in changed if not is_test(f)]
    reasons = []
    if not tests:
        reasons.append("no test file was added or changed, so nothing proves the defect is fixed")
    if not source:
        reasons.append("no non-test file changed, so the defect cannot have been fixed")

    green = _run(test_cmd + ["-rf"], repo)
    if green["exit_code"] == 5:
        reasons.append("the test suite collected no tests")
    elif green["exit_code"] != 0:
        reasons.append(f"the test suite fails on the fix branch ({green['failed']} failed, {green['errors']} errors)")

    red = None
    if tests:
        prefix = git("rev-parse", "--show-prefix").strip().rstrip("/")
        top = git("rev-parse", "--show-toplevel").strip()
        # from a subfolder, git archive narrows to that subfolder again inside the tree and returns nothing
        tar = subprocess.run(["git", "archive", "--format=tar", f"{base}:{prefix}" if prefix else base],
                             cwd=top, capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory(prefix="verify-red-") as tmp:
            with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
                tf.extractall(tmp, filter="data")
            for f in tests:
                dest = Path(tmp) / f
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(repo / f, dest)
            red = _run(test_cmd + ["-rf", *tests], Path(tmp))
        if red["exit_code"] == 0:
            reasons.append("the new or changed tests pass on the base commit, so they do not reproduce the defect")
        elif red["exit_code"] != 1:
            reasons.append(f"the new or changed tests error on the base commit (exit {red['exit_code']}) instead of failing an assertion")

    return {"status": "pass" if not reasons else "fail", "reasons": reasons, "base": base,
            "changed": {"tests": tests, "source": source},
            "green": {k: v for k, v in green.items() if k != "output"},
            "red": {k: v for k, v in red.items() if k != "output"} if red else None,
            "_logs": {"green": green["output"], "red": red["output"] if red else ""}}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--test-cmd", default=f"{shlex.quote(sys.executable)} -m pytest -q")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    r = verify(Path(a.repo), a.base, shlex.split(a.test_cmd))
    logs = r.pop("_logs")
    diff = subprocess.run(["git", "diff", a.base, "HEAD", "--", "."], cwd=a.repo, capture_output=True, text=True).stdout
    (out / "branch.diff").write_text(diff)
    (out / "verify.log").write_text(f"## green: tests on the fix branch\n\n{logs['green']}\n\n"
                                    f"## red: new/changed tests on the base commit\n\n{logs['red'] or '(not run)'}\n")
    (out / "verify.json").write_text(json.dumps(r, indent=2))
    red = r["red"]
    print(f"green: exit {r['green']['exit_code']}, {r['green']['passed']} passed, {r['green']['failed']} failed")
    print(f"red:   " + (f"exit {red['exit_code']}, {red['failed']} failed on the base commit: {', '.join(red['failed_tests'])}" if red else "not run"))
    print(f"verify: {r['status'].upper()}" + "".join(f"\n  - {x}" for x in r["reasons"]))
    return 0 if r["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
