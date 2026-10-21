"""
evals/regression_gate.py — the Go/No-Go scorecard as code (v1 = baseline, v2 = candidate).

Decision rule: v2 ships when >= 7 of 9 metrics pass AND both quality metrics
(judge_mean, citation_accuracy) pass. A blank (unmeasured) cell is a no-go.
Exit code 0 = GO, 1 = NO-GO, so this can gate CI.
"""
from __future__ import annotations

import json
import sys


def _m(x):
    return x.get("mean") if isinstance(x, dict) else x


def scorecard(v1: dict, v2: dict) -> dict:
    rows = []

    def row(name, v1v, v2v, rule, passed, quality=False):
        rows.append({"metric": name, "v1": v1v, "v2": v2v, "rule": rule,
                     "status": "pass" if passed is True else "fail" if passed is False else "not measured", "quality": quality})

    tc1, tc2 = v1.get("task_completion_rate"), v2.get("task_completion_rate")
    row("Task completion rate", tc1, tc2, ">= 95% of v1", None if tc1 is None or tc2 is None else tc2 >= 0.95 * tc1)
    j1, j2 = _m(v1.get("judge_mean")), _m(v2.get("judge_mean"))
    row("Judge mean score (1-5)", j1, j2, ">= v1", None if j1 is None or j2 is None else j2 >= j1, quality=True)
    c1, c2 = _m(v1.get("citation_accuracy")), _m(v2.get("citation_accuracy"))
    row("Citation accuracy", c1, c2, ">= 80% absolute", None if c2 is None else c2 >= 0.80, quality=True)
    t1, t2 = _m(v1.get("topic_coverage")), _m(v2.get("topic_coverage"))
    row("Topic coverage", t1, t2, ">= 85% absolute", None if t2 is None else t2 >= 0.85)
    f1, f2 = _m(v1.get("forbidden_claim_rate")), _m(v2.get("forbidden_claim_rate"))
    row("Forbidden claim rate", f1, f2, "= 0%", None if f2 is None else f2 == 0)
    cr1, cr2 = v1.get("crash_recovery_rate"), v2.get("crash_recovery_rate")
    row("Crash recovery rate", cr1, cr2, ">= 90%", None if cr2 is None else cr2 >= 0.90)
    k1, k2 = _m(v1.get("cost_per_task_usd")), _m(v2.get("cost_per_task_usd"))
    row("Cost per task (USD, list price)", k1, k2, "<= v1", None if k1 is None or k2 is None else k2 <= k1)
    w1, w2 = _m(v1.get("wall_time_s")), _m(v2.get("wall_time_s"))
    row("Wall time per run (s)", w1, w2, "<= 1.2x v1", None if w1 is None or w2 is None else w2 <= 1.2 * w1)
    r1, r2 = v1.get("reviewer_minutes"), v2.get("reviewer_minutes")
    row("Reviewer minutes per article", r1, r2, "<= v1", None if r1 is None or r2 is None else r2 <= r1)

    passed = sum(r["status"] == "pass" for r in rows)
    blanks = [r["metric"] for r in rows if r["status"] == "not measured"]
    quality_ok = all(r["status"] == "pass" for r in rows if r["quality"])
    go = passed >= 7 and quality_ok and not blanks
    why = []
    if blanks:
        why.append("unmeasured: " + ", ".join(blanks))
    if not quality_ok:
        why.append("a quality metric failed")
    if passed < 7:
        why.append(f"only {passed}/9 metrics pass")
    return {"rows": rows, "passed": passed, "quality_ok": quality_ok, "decision": "GO" if go else "NO-GO", "why": why}


if __name__ == "__main__":
    v1, v2 = (json.load(open(p)) for p in sys.argv[1:3])
    sc = scorecard(v1, v2)
    print(json.dumps(sc, indent=2))
    sys.exit(0 if sc["decision"] == "GO" else 1)
