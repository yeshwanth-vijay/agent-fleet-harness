"""
agents/defectfix/pr.py — turn a finished defect-to-fix run into a pull request description, and optionally open it.

Refuses (exit 1) unless the verify stage passed and the reviewer's verdict is APPROVE. The description is built
from the run's own files: the defect, the root cause, the diff from the base commit, the red/green proof, the
review and who approved the plan. Nothing is pushed unless --open is given (or DEFECT_FIX_OPEN_PR=1).

  python -m agents.defectfix.pr --run <run folder> --out <dir> [--open]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _read(p: Path) -> str:
    return p.read_text().strip() if p.exists() else ""


def build(run: Path) -> tuple[dict, list[str]]:
    st = _read_json(run / "state.json")
    s = run / "stages"
    defect = _read_json(s / "intake" / "defect.json")
    verify = _read_json(s / "verify" / "verify.json")
    review = _read_json(s / "review" / "review.json")
    ws = st.get("workspace", {})
    problems = []
    if verify.get("status") != "pass":
        problems.append(f"verify did not pass (status {verify.get('status')!r})")
    if review.get("verdict") != "APPROVE":
        problems.append(f"the reviewer did not approve (verdict {review.get('verdict')!r})")
    wt = Path(ws.get("wt", "."))
    stat = subprocess.run(["git", "diff", "--stat", ws.get("base", "HEAD"), "HEAD", "--", "."], cwd=wt,
                          capture_output=True, text=True).stdout.strip() if wt.exists() else ""
    red, green = verify.get("red") or {}, verify.get("green") or {}
    approval = st.get("approvals", {}).get("approve-plan", {})
    stages = st.get("stages", {})
    cost = sum(v.get("cost_usd", 0) for v in stages.values())
    title = f"{defect.get('key', '')}: {defect.get('summary', '')}"
    body = "\n\n".join(x for x in [
        f"Fixes **{defect.get('key')}** ({defect.get('type', 'Bug')}, {defect.get('priority', '')} priority), "
        f"read from `{defect.get('source')}`.",
        "## Root cause\n\n" + (_read(s / "investigate" / "rootcause.md") or "(missing)"),
        "## Change\n\n```\n" + (stat or "(no diff)") + "\n```",
        "## Proof\n\n"
        f"- **Red:** the new or changed tests fail on the base commit `{str(verify.get('base', ''))[:10]}`: "
        f"{red.get('failed', 0)} failed ({', '.join(f'`{t}`' for t in red.get('failed_tests', [])) or 'none listed'}).\n"
        f"- **Green:** the whole suite passes on this branch: {green.get('passed', 0)} passed, {green.get('failed', 0)} failed.\n"
        f"- Tests changed: {', '.join(f'`{t}`' for t in verify.get('changed', {}).get('tests', []))}; "
        f"code changed: {', '.join(f'`{t}`' for t in verify.get('changed', {}).get('source', []))}.",
        f"## Review\n\nVerdict: **{review.get('verdict')}**\n\n" + _read(s / "review" / "review.md"),
        "## How this was produced\n\n"
        f"Agent flow run `{st.get('run_id')}`. The root cause and fix plan were approved by {approval.get('by', 'nobody')} "
        f"at {approval.get('at', '?')}. Fix iterations: {st.get('loops', {}).get('verify', 1)} until verify passed. "
        f"Model cost (list-price estimate): ${cost:.2f}.",
    ] if x)
    return {"title": title, "body": body, "branch": ws.get("branch"), "base_branch": ws.get("base_branch", "main"),
            "repo_root": ws.get("repo_root")}, problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--open", action="store_true", default=os.getenv("DEFECT_FIX_OPEN_PR") == "1")
    a = ap.parse_args(argv)
    pr, problems = build(Path(a.run))
    if problems:
        print("not opening a PR: " + "; ".join(problems), file=sys.stderr)
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pr.md").write_text(f"# {pr['title']}\n\n{pr['body']}\n")
    (out / "pr.json").write_text(json.dumps({k: v for k, v in pr.items() if k != "body"}, indent=2))
    cmd = ["gh", "pr", "create", "--base", pr["base_branch"], "--head", pr["branch"], "--title", pr["title"],
           "--body-file", str(out / "pr.md")]
    print(f"PR ready: {pr['title']}\nbranch {pr['branch']} -> {pr['base_branch']}\ndescription: {out / 'pr.md'}")
    if not a.open:
        print("not pushed. To open it: git push -u origin " + pr["branch"] + " && " + " ".join(cmd[:3]) + " ...  (or re-run with --open)")
        return 0
    root = Path(pr["repo_root"])
    subprocess.run(["git", "push", "-u", "origin", pr["branch"]], cwd=root, check=True)
    url = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    (out / "pr.json").write_text(json.dumps({**{k: v for k, v in pr.items() if k != "body"}, "url": url}, indent=2))
    print(f"opened {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
