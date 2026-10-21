"""
evals/golden_set.py

Golden-task evaluation: deterministic citation checks first, then
per-section judging, then coverage / forbidden claims. Repeated runs are aggregated
with a mean and a 95% confidence interval (Student t), so v1 vs v2 differences can be
told apart from LLM variance. No silent except/pass, no default scores.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from evals.citation_verifier import verify
from evals.section_judge import judge_document

HERE = Path(__file__).parent
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}


def load_tasks(path: str | Path = HERE / "golden_tasks.json") -> dict[str, dict]:
    return {t["task_id"]: t for t in json.loads(Path(path).read_text())["tasks"]}


def evaluate_output(task: dict, markdown: str, *, judge_model: str = "opus", citation_backend: str = "auto",
                    run_id: str = "", seen_urls: set[str] | None = None) -> dict:
    cites = verify(markdown, backend=citation_backend, run_id=run_id, seen_urls=seen_urls)
    judged = judge_document(markdown, task, model=judge_model, run_id=run_id)
    return {"task_id": task["task_id"], "citations": {k: v for k, v in cites.items() if k != "details"},
            "citation_details": cites["details"], **judged,
            "eval_cost_usd": round(judged["judge_cost_usd"], 4)}


def ci95(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    m = statistics.fmean(vals)
    if len(vals) < 2:
        return {"mean": round(m, 3), "lo": None, "hi": None, "n": 1}
    half = T95.get(len(vals) - 1, 1.96) * statistics.stdev(vals) / math.sqrt(len(vals))
    return {"mean": round(m, 3), "lo": round(m - half, 3), "hi": round(m + half, 3), "n": len(vals)}


def aggregate(evals: list[dict], runs: list[dict] | None = None) -> dict:
    """evals: one evaluate_output() per (task, repetition). runs: optional fleet run metadata."""
    out = {
        "judge_mean": ci95([e.get("judge_mean") for e in evals]),
        "citation_accuracy": ci95([e["citations"].get("citation_accuracy") for e in evals]),
        "topic_coverage": ci95([e.get("topic_coverage") for e in evals]),
        "forbidden_claim_rate": ci95([e.get("forbidden_claim_rate") for e in evals]),
        "completeness": ci95([e.get("completeness") for e in evals]),
        "words": ci95([e.get("word_count") for e in evals]),
        "n_outputs": len(evals),
        "citation_backends": sorted({e["citations"]["backend"] for e in evals}),
    }
    if runs is not None:
        done = [r for r in runs if r.get("status") == "completed"]
        out["task_completion_rate"] = round(len(done) / len(runs), 3) if runs else None
        out["cost_per_task_usd"] = ci95([r.get("cost_usd") for r in done])
        out["wall_time_s"] = ci95([r.get("wall_time_s") for r in done])
    return out
