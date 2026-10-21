"""
agents/fleet/orchestrator.py

Research fleet: a LangGraph supervisor that picks its roster per query, instead of a fixed pipeline.

  planner ──Send──▶ research seats in PARALLEL (scout / github / codebase, chosen per query)
          ──▶ synthesizer ──▶ editor ──▶ critic ──(major/reject: revise ≤2)──▶ synthesizer
          ──▶ fact_checker (deterministic) ──(citations failing: revise)──▶ synthesizer
          ──▶ judge ──(REJECT: replan ≤1)──▶ planner
          ──▶ finalize

Every seat is a Claude Code headless session on the Claude login (no API keys).
Durability: a LangGraph SqliteSaver checkpoint is written after every node; a crashed
run resumes from the last completed node with `--resume <run_id>`.
Cost governance: BudgetGovernor (deterministic) gates and downgrades every seat.
"""
from __future__ import annotations

import json
import operator
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agents.fleet import seats as seat_mod
from agents.harness import telemetry
from agents.harness.budget import BudgetGovernor
from agents.harness.claude_runner import run_seat
from agents.harness.compaction import compact
from agents.harness.security import quarantine
from evals.citation_verifier import verify

ROOT = Path(__file__).resolve().parents[2]
RUNS = Path(os.getenv("FLEET_RUNS_DIR", ROOT / "runs" / "fleet"))
CFG, SEATS = seat_mod.load()
TIERS = CFG["model_tiers"]


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class FleetState(TypedDict, total=False):
    run_id: str
    task_id: str
    query: str
    sub_questions: list[str]
    codebase_paths: list[str]
    min_words: int
    max_words: int
    budget_usd: float
    plan: dict
    roster: list[dict]
    findings: Annotated[dict, _merge]
    seen_urls: Annotated[list, operator.add]
    draft: str
    critique: dict
    revisions: int
    replans: int
    citation_report: dict
    judge: dict
    final: str
    status: str
    spent_usd: Annotated[float, operator.add]
    ledger: Annotated[list, operator.add]
    notes: Annotated[list, operator.add]


def _log(state_or_id, msg: str) -> None:
    rid = state_or_id if isinstance(state_or_id, str) else state_or_id.get("run_id", "")
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    telemetry.event("fleet.log", run_id=rid, attrs={"message": msg})


def _gov(state: FleetState) -> BudgetGovernor:
    g = BudgetGovernor(run_budget_usd=state.get("budget_usd", CFG["run"]["budget_usd"]), downgrade_at=CFG["run"]["downgrade_at"])
    g.spent_usd = state.get("spent_usd", 0.0)
    return g


def _seat(state: FleetState, name: str, prompt: str, *, schema: dict | None = None, label: str | None = None,
          add_dirs: list[str] | None = None, tools: list[str] | None = None) -> tuple[Any, dict]:
    """Run one seat under the governor. Returns (SeatRun|None, state_update)."""
    sc = SEATS[name]
    gov = _gov(state)
    allow = gov.request(name, sc.tier, sc.budget_usd, required=sc.required)
    upd: dict = {"notes": [], "ledger": [], "spent_usd": 0.0}
    if allow.reason:
        upd["notes"].append(f"governor: {name}: {allow.reason}")
        _log(state, f"governor: {name}: {allow.reason}")
    if not allow.allowed:
        return None, upd
    r = run_seat(label or name, prompt, model=TIERS[allow.tier], system=sc.system(), temperature=sc.temperature,
                 tools=tools if tools is not None else sc.tools, json_schema=schema, max_turns=sc.max_turns,
                 budget_usd=allow.budget_usd, add_dirs=add_dirs, run_id=state["run_id"])
    upd["spent_usd"] = r.cost_usd
    upd["ledger"] = [{"seat": label or name, "tier": allow.tier, "model": r.model, "cost_usd": round(r.cost_usd, 4),
                      "turns": r.turns, "tool_calls": len(r.tool_calls), "duration_s": r.duration_s, "error": r.error}]
    _log(state, f"{label or name:<12} {r.model:<22} {r.duration_s:>6.0f}s  ${r.cost_usd:.3f}  tools={len(r.tool_calls)}" + (f"  ERROR {r.error}" if r.error else ""))
    return r, upd


# ------------------------------------------------------------------ planner
PLAN_SCHEMA = {"type": "object", "required": ["rationale", "roster"], "properties": {
    "rationale": {"type": "string"},
    "roster": {"type": "array", "items": {"type": "object", "required": ["seat", "queries", "sub_questions"], "properties": {
        "seat": {"type": "string", "enum": ["scout", "github", "codebase"]},
        "queries": {"type": "array", "items": {"type": "string"}},
        "sub_questions": {"type": "array", "items": {"type": "string"}}}}}}}


def planner(state: FleetState) -> dict:
    feedback = state.get("judge", {}).get("feedback") if state.get("replans") else None
    prompt = (f"Query: {state['query']}\nSub-questions:\n" + "\n".join(f"- {q}" for q in state.get("sub_questions", []))
              + (f"\nCodebase paths available: {state['codebase_paths']}" if state.get("codebase_paths") else "\nNo codebase paths: do not spawn codebase.")
              + (f"\n\nThe previous draft was REJECTED by the judge. Feedback to address:\n{feedback}" if feedback else ""))
    r, upd = _seat(state, "planner", prompt, schema=PLAN_SCHEMA)
    roster = (r.structured or {}).get("roster", []) if r and r.ok else []
    if not state.get("codebase_paths"):
        roster = [a for a in roster if a["seat"] != "codebase"]
    if not roster:  # deterministic fallback so the run can still proceed
        roster = [{"seat": "scout", "queries": [state["query"]], "sub_questions": state.get("sub_questions", [])}]
        upd["notes"].append("planner fallback roster: scout only")
    _log(state, f"plan: {[a['seat'] for a in roster]} — {(r.structured or {}).get('rationale', '')[:140] if r else ''}")
    return {**upd, "plan": (r.structured if r else {}) or {}, "roster": roster}


def dispatch(state: FleetState) -> list[Send]:
    return [Send("research", {"assignment": a, "run_id": state["run_id"], "query": state["query"],
                              "codebase_paths": state.get("codebase_paths", []), "spent_usd": state.get("spent_usd", 0.0),
                              "budget_usd": state.get("budget_usd")}) for a in state["roster"]]


# ----------------------------------------------------------------- research
FIND_SCHEMA = {"type": "object", "required": ["summary", "findings"], "properties": {
    "summary": {"type": "string"},
    "findings": {"type": "array", "items": {"type": "object", "required": ["claim", "source_url"], "properties": {
        "claim": {"type": "string"}, "source_url": {"type": "string"}, "evidence": {"type": "string"},
        "number": {"type": "string"}, "date": {"type": "string"}}}},
    "gaps": {"type": "array", "items": {"type": "string"}}}}


def research(payload: dict) -> dict:
    a, name = payload["assignment"], payload["assignment"]["seat"]
    prompt = (f"Overall query: {payload['query']}\nYour sub-questions:\n" + "\n".join(f"- {q}" for q in a["sub_questions"])
              + "\nStart from these searches/paths:\n" + "\n".join(f"- {q}" for q in a["queries"])
              + "\nIf WebFetch is unavailable or fails, rely on WebSearch result snippets and cite the result URL.")
    add_dirs = payload.get("codebase_paths") if name == "codebase" else None
    r, upd = _seat(payload, name, prompt, schema=FIND_SCHEMA, add_dirs=add_dirs)
    if not r or not r.ok or not r.structured:
        return {**upd, "findings": {name: f"[{name} produced no findings: {r.error if r else 'skipped by governor'}]"}}
    s = r.structured
    text = s["summary"] + "\n" + "\n".join(
        f"- {f['claim']}" + (f" ({f['number']})" if f.get("number") else "") + (f" [{f['date']}]" if f.get("date") else "")
        + f" — source: {f['source_url']}" + (f"\n  evidence: {f['evidence'][:300]}" if f.get("evidence") else "") for f in s["findings"])
    if s.get("gaps"):
        text += "\nGaps: " + "; ".join(s["gaps"])
    text, stats = compact(name, text, model=TIERS[CFG["run"]["compact_model"]], run_id=payload["run_id"])
    if stats.get("compacted"):
        upd["notes"].append(f"compacted {name}: {stats['chars_in']}→{stats['chars_out']} chars, {stats.get('facts_kept')} facts kept")
        upd["spent_usd"] += stats.get("cost_usd", 0)
    wrapped, hits = quarantine(text, f"seat:{name}")
    if hits:
        upd["notes"].append(f"security: {name}: neutralised {hits}")
    return {**upd, "findings": {name: wrapped}, "seen_urls": r.seen_urls + [f["source_url"] for f in s["findings"]]}


# --------------------------------------------------------------- synthesis
def synthesize(state: FleetState) -> dict:
    revising = bool(state.get("draft"))
    crit = state.get("critique") or {}
    bad = [c for c in (state.get("citation_report") or {}).get("details", []) if c["status"] in ("unresolved", "weak_support", "not_found")]
    prompt = (f"Query: {state['query']}\nSub-questions:\n" + "\n".join(f"- {q}" for q in state.get("sub_questions", []))
              + f"\nTarget length: {state['min_words']}-{state['max_words']} words.\n\nFindings from the research seats:\n\n"
              + "\n\n".join(state.get("findings", {}).values()))
    if revising:
        prompt += "\n\nYOUR PREVIOUS DRAFT:\n" + state["draft"]
        if crit.get("issues"):
            prompt += "\n\nCRITIQUE TO FIX:\n" + "\n".join(f"- [{i.get('severity','')}] {i['problem']} → {i.get('fix','')}" for i in crit["issues"])
        if bad:
            prompt += "\n\nCITATIONS THAT FAILED DETERMINISTIC VERIFICATION (fix the claim, cite a URL from the findings, or remove):\n" + \
                      "\n".join(f"- {c['url']} — {c['claim'][:160]} ({c['status']})" for c in bad[:40])
        prompt += "\n\nReturn the complete revised article."
    r, upd = _seat(state, "synthesizer", prompt)
    draft = r.text if r and r.ok and len(r.text.split()) > 300 else state.get("draft", "")
    return {**upd, "draft": draft, "revisions": state.get("revisions", 0) + (1 if revising else 0)}


def edit(state: FleetState) -> dict:
    r, upd = _seat(state, "editor", "Edit this article. Return the full article only.\n\n" + state["draft"])
    if not r or not r.ok:
        return upd
    import re
    urls = lambda t: set(re.findall(r"https?://[^\s)\]]+", t))
    before, after = state["draft"], r.text
    # deterministic guard: an editor may not drop content or citations
    if len(after.split()) < 0.85 * len(before.split()) or len(urls(before) - urls(after)) > 2:
        upd["notes"].append("editor output rejected: dropped content or citations")
        _log(state, "editor output rejected by guard (lost content/citations); keeping synthesizer draft")
        return upd
    return {**upd, "draft": after}


CRIT_SCHEMA = {"type": "object", "required": ["severity", "issues"], "properties": {
    "severity": {"type": "string", "enum": ["pass", "minor", "major", "reject"]},
    "issues": {"type": "array", "items": {"type": "object", "required": ["problem"], "properties": {
        "quote": {"type": "string"}, "problem": {"type": "string"}, "fix": {"type": "string"},
        "severity": {"type": "string", "enum": ["minor", "major"]}}}}}}


def critique(state: FleetState) -> dict:
    r, upd = _seat(state, "critic", f"Query: {state['query']}\nSub-questions: {state.get('sub_questions')}\n\nDraft to critique:\n\n{state['draft']}",
                   schema=CRIT_SCHEMA)
    c = (r.structured if r and r.ok else None) or {"severity": "pass", "issues": [], "skipped": True}
    _log(state, f"critic: {c['severity']} ({len(c.get('issues', []))} issues)")
    return {**upd, "critique": c}


def after_critique(state: FleetState) -> str:
    if state["critique"]["severity"] in ("major", "reject") and state.get("revisions", 0) < CFG["run"]["max_revisions"]:
        return "synthesize"
    return "fact_check"


def fact_check(state: FleetState) -> dict:
    rep = verify(state["draft"], backend=os.getenv("FLEET_CITATIONS", "auto"), run_id=state["run_id"], seen_urls=set(state.get("seen_urls", [])))
    _log(state, f"fact-check ({rep['backend']}): {rep['verified']}/{rep['checked'] - rep['network_blocked']} verified, "
                f"accuracy={rep['citation_accuracy']}, provenance={rep['provenance_rate']}")
    return {"citation_report": rep}


def after_fact_check(state: FleetState) -> str:
    acc = state["citation_report"].get("citation_accuracy")
    if acc is not None and acc < 0.70 and state.get("revisions", 0) < CFG["run"]["max_revisions"]:
        return "synthesize"
    return "judge"


JUDGE_SCHEMA = {"type": "object", "required": ["verdict", "scores", "feedback"], "properties": {
    "verdict": {"type": "string", "enum": ["APPROVE", "REJECT"]},
    "scores": {"type": "object", "properties": {k: {"type": "integer", "minimum": 0, "maximum": 5} for k in
               ["accuracy", "depth_actionability", "citation_quality", "clarity_structure", "honesty_calibration"]}},
    "feedback": {"type": "string"}}}


def judge(state: FleetState) -> dict:
    from evals.rubric import rubric_text
    rep = {k: v for k, v in (state.get("citation_report") or {}).items() if k != "details"}
    r, upd = _seat(state, "judge", f"Query: {state['query']}\n\nRubric:\n{rubric_text()}\n\nDeterministic citation report: {json.dumps(rep)}\n\n<article>\n{state['draft']}\n</article>",
                   schema=JUDGE_SCHEMA)
    j = (r.structured if r and r.ok else None) or {"verdict": "APPROVE", "scores": {}, "feedback": "judge unavailable", "skipped": True}
    _log(state, f"judge: {j['verdict']} {j.get('scores')}")
    return {**upd, "judge": j, "replans": state.get("replans", 0) + (1 if j["verdict"] == "REJECT" else 0)}


def after_judge(state: FleetState) -> str:
    if state["judge"]["verdict"] == "REJECT" and state.get("replans", 0) <= CFG["run"]["max_replans"]:
        return "planner"
    return "finalize"


def finalize(state: FleetState) -> dict:
    return {"final": state["draft"], "status": "completed"}


# ------------------------------------------------------------------- graph
def _crashable(name, fn):
    def wrapped(state):
        out = fn(state)
        if os.getenv("FLEET_CRASH_AFTER") == name:
            os.environ.pop("FLEET_CRASH_AFTER")
            raise SystemExit(f"simulated crash after {name}")
        return out
    return wrapped


def build(checkpointer):
    g = StateGraph(FleetState)
    for name, fn in [("planner", planner), ("research", research), ("synthesize", synthesize), ("edit", edit),
                     ("critique", critique), ("fact_check", fact_check), ("judge", judge), ("finalize", finalize)]:
        g.add_node(name, _crashable(name, fn))
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", dispatch, ["research"])
    g.add_edge("research", "synthesize")
    g.add_edge("synthesize", "edit")
    g.add_edge("edit", "critique")
    g.add_conditional_edges("critique", after_critique, ["synthesize", "fact_check"])
    g.add_conditional_edges("fact_check", after_fact_check, ["synthesize", "judge"])
    g.add_conditional_edges("judge", after_judge, ["planner", "finalize"])
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def run(task: dict | None = None, *, resume: str | None = None, budget_usd: float | None = None,
        min_words: int | None = None, max_words: int | None = None) -> dict:
    RUNS.mkdir(parents=True, exist_ok=True)
    run_id = resume or f"{(task or {}).get('task_id', 'adhoc')}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    rdir = RUNS / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    telemetry.configure(str(rdir / "telemetry.jsonl"))
    conn = sqlite3.connect(str(RUNS / "checkpoints.sqlite"), check_same_thread=False)
    graph = build(SqliteSaver(conn))
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 60}
    meta_path = rdir / "run.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"run_id": run_id, "task_id": (task or {}).get("task_id"), "attempts": []}
    t0 = time.time()
    meta["attempts"].append({"started": t0, "resumed": bool(resume)})
    meta_path.write_text(json.dumps(meta, indent=2))
    span = telemetry.start_span("fleet.run", run_id=run_id, attrs={"resume": bool(resume)})
    if resume:
        snap = graph.get_state(config)
        _log(run_id, f"resuming {run_id} from checkpoint; next={list(snap.next)}; spent so far ${snap.values.get('spent_usd', 0):.2f}")
        inp = None
    else:
        inp = {"run_id": run_id, "task_id": task.get("task_id", "adhoc"), "query": task["query"],
               "sub_questions": task.get("sub_questions", []), "codebase_paths": task.get("codebase_paths", []),
               "min_words": min_words or task.get("min_word_count", 2500), "max_words": max_words or task.get("max_word_count", 8000),
               "budget_usd": budget_usd or CFG["run"]["budget_usd"], "revisions": 0, "replans": 0, "spent_usd": 0.0}
    status, err = "completed", None
    try:
        final = graph.invoke(inp, config)
    except SystemExit as e:  # simulated crash
        status, err, final = "crashed", str(e), graph.get_state(config).values
    except Exception as e:  # noqa: BLE001
        status, err, final = "failed", f"{type(e).__name__}: {e}", graph.get_state(config).values
    wall = time.time() - t0
    meta["attempts"][-1].update({"ended": time.time(), "wall_time_s": round(wall, 1), "status": status, "error": err})
    meta.update({"status": status if status != "completed" else final.get("status", "completed"),
                 "wall_time_s": round(sum(a.get("wall_time_s", 0) for a in meta["attempts"]), 1),
                 "cost_usd": round(final.get("spent_usd", 0.0), 4), "ledger": final.get("ledger", []), "notes": final.get("notes", []),
                 "roster": [a["seat"] for a in final.get("roster", [])], "revisions": final.get("revisions"), "replans": final.get("replans"),
                 "judge": final.get("judge"), "critique": {k: v for k, v in (final.get("critique") or {}).items()},
                 "citation_report": {k: v for k, v in (final.get("citation_report") or {}).items() if k != "details"},
                 "seen_urls": len(set(final.get("seen_urls", [])))})
    meta_path.write_text(json.dumps(meta, indent=2, default=str))
    if final.get("final"):
        (rdir / "article.md").write_text(final["final"])
    (rdir / "seen_urls.json").write_text(json.dumps(sorted(set(final.get("seen_urls", [])))))
    telemetry.end_span(span, attrs={"fleet.status": meta["status"], "fleet.cost_usd": meta["cost_usd"]})
    conn.close()
    return meta
