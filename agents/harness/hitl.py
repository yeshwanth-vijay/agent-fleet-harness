"""
agents/harness/hitl.py — human in the loop for every fleet, at phase boundaries.

Every orchestrator calls gate(run, phase, "before") as a phase starts and gate(run, phase, "after") once it is done.
`run` is the orchestrator itself: it has .dir, .state, .save() and .log(); .gov (the budget governor) is optional.

Files in the run folder (a control UI or a person can edit them; a run started from the terminal honours them):
  hitl.json            the plan: mode, checkpoints, triggers (written at launch from SWARM_HITL_PLAN, editable live)
  control.json         live controls: {"pause": true} holds at the next boundary, {"stop": true} stops there
  hitl-decisions.json  a person's answer per checkpoint: {"<id>": {"decision": "approve"|"reject", "note", "by", "at"}}
  hitl-log.json        every hold, notice, decision and on-the-fly call, in order

Plan:
  {"mode": "off" | "suggested" | "auto",
   "checkpoints": [{"id", "phase", "when": "before"|"after", "who", "why", "blocking": true}],
   "triggers":    [{"on": "judge_revise"|"budget_80"|"seat_error", "who", "why"}]}

A blocking checkpoint holds the run: status waiting_approval, waiting_for "hitl", the checkpoint in
state["hitl_pending"], a ⏸ line in progress.log, and the process exits 0 so it can be resumed (--resume).
A non-blocking checkpoint only leaves a notice. A trigger fires once, after a phase. In "auto" mode the swarm
decides on the fly whether a fired trigger needs a person (a small seat reads the evidence); if that seat
cannot answer, the run holds: unsure means ask a human. A rejected checkpoint stops the run.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

PLAN, CONTROL, DECISIONS, LOG = "hitl.json", "control.json", "hitl-decisions.json", "hitl-log.json"
TRIGGERS = {
    "judge_revise": "the reviewer said REVISE or REJECT",
    "budget_80": "the run has spent 80% of its budget",
    "seat_error": "an agent seat ended with an error",
}
DECIDE_SCHEMA = {"type": "object", "required": ["hold", "who", "why"], "properties": {
    "hold": {"type": "boolean"}, "who": {"type": "string"}, "why": {"type": "string"}}}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, data):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(p)


def plan(run_dir: Path) -> dict:
    """The run's plan; the first call copies the launch plan (SWARM_HITL_PLAN) into the run folder."""
    p = Path(run_dir) / PLAN
    if not p.exists():
        src = os.environ.get("SWARM_HITL_PLAN")
        if src and Path(src).exists():
            data = _read(Path(src), {})
            data.setdefault("applied_at", _now())
            _write(p, data)
    return _read(p, {})


def set_plan(run_dir: Path, data: dict):
    _write(Path(run_dir) / PLAN, {**data, "edited_at": _now()})
    note(run_dir, {"event": "plan edited", "checkpoints": len(data.get("checkpoints", [])), "mode": data.get("mode")})


def control(run_dir: Path) -> dict:
    return _read(Path(run_dir) / CONTROL, {})


def set_control(run_dir: Path, **kw):
    c = {**control(run_dir), **kw, "at": _now()}
    _write(Path(run_dir) / CONTROL, c)
    return c


def decisions(run_dir: Path) -> dict:
    return _read(Path(run_dir) / DECISIONS, {})


def decide(run_dir: Path, cid: str, decision: str, note_text: str = "", by: str = "you") -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    d = decisions(run_dir)
    d[cid] = {"decision": decision, "note": note_text[:500], "by": by, "at": _now()}
    _write(Path(run_dir) / DECISIONS, d)
    note(run_dir, {"event": decision, "checkpoint": cid, "by": by, "note": note_text[:500]})
    return d[cid]


def history(run_dir: Path) -> list[dict]:
    return _read(Path(run_dir) / LOG, [])


def note(run_dir: Path, entry: dict):
    h = history(run_dir)
    h.append({"at": _now(), **entry})
    _write(Path(run_dir) / LOG, h[-500:])


# ── the gate ────────────────────────────────────────────────────────────────

def gate(run, phase: str, when: str = "before"):
    d, st = Path(run.dir), run.state
    ctl = control(d)
    if ctl.get("stop"):
        set_control(d, stop=False)
        st.update(status="stopped", error=f"stopped by {ctl.get('by') or 'you'} at {when} {phase}")
        st.pop("hitl_pending", None)
        run.log(f"■ stopped by {ctl.get('by') or 'you'} ({when} {phase})")
        note(d, {"event": "stopped", "phase": phase, "when": when, "by": ctl.get("by") or "you"})
        run.save()
        raise SystemExit(0)

    pend = st.get("hitl_pending")
    if pend:
        r = decisions(d).get(pend["id"])
        if not r:
            _hold(run, pend, again=True)
        st.pop("hitl_pending", None)
        st["status"] = "running"
        if r["decision"] == "reject":
            st.update(status="stopped", error=f"rejected by {r['by']} at {pend['id']}: {r.get('note') or 'no reason given'}")
            run.log(f"■ {r['by']} rejected {pend['id']} — run stopped. {r.get('note') or ''}")
            run.save()
            raise SystemExit(0)
        run.log(f"  ✓ {r['by']} approved {pend['id']}{': ' + r['note'] if r.get('note') else ''}")
        run.save()

    if ctl.get("pause"):
        set_control(d, pause=False)  # from here the hold itself is the pause
        _hold(run, {"id": f"pause-{datetime.now():%H%M%S}", "phase": phase, "when": when, "who": ctl.get("by") or "You",
                    "why": ctl.get("reason") or "paused by a person", "blocking": True, "kind": "pause"})

    p = plan(d)
    if not p or p.get("mode") == "off":
        return
    done = decisions(d)
    for cp in p.get("checkpoints", []):
        if cp.get("phase") != phase or cp.get("when", "before") != when:
            continue
        cid = cp.get("id") or f"{when}-{phase}"
        if cid in done or cid in st.get("hitl_noticed", []):
            continue
        if not cp.get("blocking", True):
            st.setdefault("hitl_noticed", []).append(cid)
            run.log(f"  ℹ human in the loop (notice): {cp.get('who', 'You')} — {cp.get('why', '')} ({when} {phase})")
            note(d, {"event": "notice", "checkpoint": cid, "phase": phase, "when": when, "who": cp.get("who")})
            run.save()
            continue
        _hold(run, {**cp, "id": cid, "kind": "checkpoint"})

    if when != "after":
        return
    for t, detail, key in _fired_triggers(st, p):
        st.setdefault("hitl_fired", []).append(key)
        cp = {"id": key, "phase": phase, "when": "after", "who": t.get("who") or "You", "kind": "trigger", "blocking": True,
              "why": f"{TRIGGERS[t['on']]} ({detail})" + (f": {t['why']}" if t.get("why") else "")}
        if p.get("mode") == "auto":
            call = decide_on_the_fly(run, cp, p)
            note(d, {"event": "on-the-fly", "trigger": t["on"], "phase": phase, **call})
            if not call["hold"]:
                run.log(f"  ℹ {t['on']} fired after {phase}; the swarm carried on: {call['why']}")
                run.save()
                continue
            cp.update(who=call["who"] or cp["who"], why=call["why"] or cp["why"], decided_by="swarm")
        _hold(run, cp)
    run.save()


def _fired_triggers(st: dict, p: dict):
    fired = set(st.get("hitl_fired", []))
    for t in p.get("triggers", []):
        on = t.get("on")
        if on == "judge_revise":
            v = st.get("verdict") or (st.get("judge") or {}).get("verdict")
            if v in ("REVISE", "REJECT") and f"trig-{on}" not in fired:
                yield t, f"verdict {v}", f"trig-{on}"
        elif on == "budget_80":
            b = st.get("budget") or {}
            cap, spent = b.get("run_budget_usd") or 0, b.get("spent_usd") or 0
            if cap and spent >= 0.8 * cap and f"trig-{on}" not in fired:
                yield t, f"${spent:.2f} of ${cap:.2f}", f"trig-{on}"
        elif on == "seat_error":
            for name, s in (st.get("seats") or {}).items():
                if isinstance(s, dict) and s.get("error") and f"trig-{on}-{name}" not in fired:
                    yield t, f"{name}: {str(s['error'])[:120]}", f"trig-{on}-{name}"


def _hold(run, cp: dict, again: bool = False):
    st = run.state
    cp = {**cp, "since": cp.get("since") or _now()}
    st.update(status="waiting_approval", waiting_for="hitl", hitl_pending=cp, error=None)
    run.log(f"⏸ human in the loop: {cp.get('who', 'You')} — {cp.get('why', '')} "
            f"({cp.get('when', 'before')} {cp.get('phase')}; approve or reject in hitl-decisions.json)")
    if not again:
        note(run.dir, {"event": "hold", **{k: cp.get(k) for k in ("id", "phase", "when", "who", "why", "kind", "decided_by")}})
    run.save()
    raise SystemExit(0)


# ── on the fly ──────────────────────────────────────────────────────────────

def decide_on_the_fly(run, cp: dict, p: dict) -> dict:
    """A trigger fired in auto mode: does this need a person now? Unsure or failed → hold."""
    from agents.harness.claude_runner import run_seat  # late import: the gate itself needs no model

    st = run.state
    judge = st.get("judge") or {}
    facts = {"trigger": cp["why"], "phase_just_finished": cp["phase"], "phases_done": st.get("done", []),
             "judge": {"verdict": judge.get("verdict"), "issues": (judge.get("issues") or [])[:6]},
             "budget": st.get("budget"), "seat_errors": {k: v.get("error") for k, v in (st.get("seats") or {}).items()
                                                         if isinstance(v, dict) and v.get("error")},
             "roles": p.get("roles") or ["You"], "task": p.get("task", "")}
    gov = getattr(run, "gov", None)
    if gov is not None:
        allow = gov.request("hitl_decider", "sonnet", 0.3, required=False)
        if not allow.allowed:
            return {"hold": True, "who": cp["who"], "why": f"{cp['why']} (budget left no room to decide automatically)"}
    r = run_seat("hitl_decider", "Decide whether this run needs a person now.\n\n" + json.dumps(facts, indent=1, default=str),
                 model="sonnet", system=DECIDER_SYSTEM, tools=[], json_schema=DECIDE_SCHEMA, max_turns=2, budget_usd=0.3,
                 cwd=str(run.dir), run_id=st.get("run_id", ""), timeout_s=180)
    if gov is not None:
        gov.record("hitl_decider", "sonnet", r.cost_usd)
    seats = st.setdefault("seats", {})
    seats[f"hitl_decider-{len(seats)}"] = {"model": r.model, "cost_usd": round(r.cost_usd, 4), "turns": r.turns,
                                          "duration_s": round(r.duration_s, 1), "error": r.error}
    out = r.structured or {}
    if "hold" not in out:
        return {"hold": True, "who": cp["who"], "why": f"{cp['why']} (could not decide automatically, so asking)"}
    return {"hold": bool(out["hold"]), "who": str(out.get("who") or cp["who"])[:80], "why": str(out.get("why") or "")[:300]}


DECIDER_SYSTEM = """You decide, mid-run, whether an agent fleet must stop and ask a person. Hold (hold=true) when
continuing could waste real money or time, act on a wrong premise, change code, data or an org, or publish
something a reviewer flagged as wrong. Carry on (hold=false) when the issue is minor, already handled by the
fleet's own revision step, or only cosmetic. When unsure, hold. Name who should look (pick from roles) and say
why in one plain sentence that names the specific issue."""
