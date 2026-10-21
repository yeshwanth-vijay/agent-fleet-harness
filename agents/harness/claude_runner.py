"""
agents/harness/claude_runner.py

The agent loop for every fleet seat, provided by Claude Code headless (`claude -p`)
on the user's Claude login. No API keys: ANTHROPIC_API_KEY / OPENAI / GEMINI are
stripped from the child environment.

Claude Code already supplies most harness components:
  1 agent loop (observe/think/act, tool_use until end_turn)      -> built in
  2 tool surface (Read/Grep/Glob/WebSearch/WebFetch, schemas)    -> --allowedTools / --tools
  3 permissions/sandbox                                          -> --allowedTools, --disallowedTools, cwd
  4-5 context engineering + semantic compaction inside a seat    -> built-in auto-compaction
  8 cost governance per seat                                     -> --max-budget-usd, --max-turns
  structured outputs                                             -> --json-schema
We add on top: budget governor across seats (budget.py), quarantine (security.py),
telemetry spans (telemetry.py), cross-seat compaction (compaction.py), and
durability/resume (LangGraph checkpointer in agents/fleet/orchestrator.py).

Temperature: Claude Code exposes no temperature, and Opus/Sonnet 5.x reject it on
the API anyway. A seat's nominal temperature maps to --effort plus a steering line.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agents.harness import telemetry

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
_URL = __import__("re").compile(r"https?://[^\s\"'<>\\)\]]+")
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def temperature_to_effort(t: float) -> tuple[str, str]:
    if t <= 0.2:
        return "low", "Be precise and deterministic. Prefer the single best-supported answer."
    if t <= 0.5:
        return "medium", "Be careful and specific. Mention an alternative only when it matters."
    return "high", "Explore alternatives and edge cases before settling, and vary your examples."


@dataclass
class SeatRun:
    seat: str
    text: str = ""
    structured: Optional[dict] = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    turns: int = 0
    duration_s: float = 0.0
    model: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    seen_urls: list[str] = field(default_factory=list)
    error: Optional[str] = None
    stopped_by_budget: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def run_seat(
    seat: str,
    prompt: str,
    *,
    model: str,
    system: str,
    temperature: float = 0.3,
    tools: list[str] | None = None,
    json_schema: dict | None = None,
    max_turns: int = 20,
    budget_usd: float = 2.0,
    cwd: str | None = None,
    add_dirs: list[str] | None = None,
    on_event: Callable[[dict], None] | None = None,
    timeout_s: int = 1800,
    run_id: str = "",
    mcp_config: str | None = None,
    allow_writes: bool = False,
) -> SeatRun:
    """Run one seat to completion. Never raises: errors come back on SeatRun.error."""
    effort, steer = temperature_to_effort(temperature)
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    workdir = cwd or tempfile.mkdtemp(prefix=f"seat-{seat}-")
    fd, sys_file = tempfile.mkstemp(prefix=f"{seat}-sys-", suffix=".md")
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{system}\n\n{steer}\n")

    args = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--effort", effort, "--append-system-prompt-file", sys_file,
            "--max-turns", str(max_turns), "--max-budget-usd", f"{budget_usd:.2f}"]
    if tools:
        args += ["--allowedTools", *tools]
    else:
        args += ["--tools", ""]
    if not allow_writes:  # opt-in only (a development seat in its own git worktree); edits stay inside cwd/add_dirs
        args += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    if json_schema:
        args += ["--json-schema", json.dumps(json_schema)]
    for d in add_dirs or []:
        args += ["--add-dir", d]
    if mcp_config:  # seat-specific MCP servers only (e.g. a browser instrument); tools still need --allowedTools
        args += ["--mcp-config", mcp_config, "--strict-mcp-config"]

    out = SeatRun(seat=seat, model=model)
    span = telemetry.start_span("gen_ai.invoke_agent", run_id=run_id, seat=seat, attrs={
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": seat, "gen_ai.request.model": model,
        "gen_ai.request.temperature_nominal": temperature, "fleet.effort": effort, "fleet.budget_usd": budget_usd})
    t0 = time.time()
    result: dict | None = None
    try:
        proc = subprocess.Popen(args, cwd=workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        assert proc.stdin and proc.stdout
        proc.stdin.write(prompt)
        proc.stdin.close()
        deadline = t0 + timeout_s
        for line in proc.stdout:
            if time.time() > deadline:
                proc.kill()
                out.error = f"timeout after {timeout_s}s"
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for c in ev.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use" and c.get("name") != "StructuredOutput":
                        inp = c.get("input") or {}
                        target = inp.get("query") or inp.get("url") or inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
                        call = {"tool": c.get("name"), "target": str(target)[:200], "t": round(time.time() - t0, 1)}
                        out.tool_calls.append(call)
                        telemetry.event("gen_ai.execute_tool", run_id=run_id, seat=seat, attrs={
                            "gen_ai.tool.name": call["tool"], "fleet.tool.target": call["target"]})
                        if on_event:
                            on_event({"type": "tool", "seat": seat, **call})
            elif ev.get("type") == "user":
                # tool results: remember every URL a search/fetch actually returned (provenance)
                for c in ev.get("message", {}).get("content", []) or []:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        body = c.get("content")
                        body = json.dumps(body) if not isinstance(body, str) else body
                        for u in _URL.findall(body or ""):
                            if u not in out.seen_urls:
                                out.seen_urls.append(u.rstrip(".,;)\"'"))
            elif ev.get("type") == "result":
                result = ev
        proc.wait(timeout=30)
        stderr = proc.stderr.read() if proc.stderr else ""
    except Exception as exc:  # noqa: BLE001
        out.error = f"{type(exc).__name__}: {exc}"
        stderr = ""
    finally:
        os.remove(sys_file)

    out.duration_s = round(time.time() - t0, 1)
    if result:
        usage = result.get("usage") or {}
        out.text = result.get("result") or ""
        out.structured = result.get("structured_output")
        out.cost_usd = float(result.get("total_cost_usd") or 0)
        out.input_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
        out.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        out.output_tokens = int(usage.get("output_tokens") or 0)
        out.turns = int(result.get("num_turns") or 0)
        mu = result.get("modelUsage") or {}
        # Claude Code may call haiku internally (e.g. web-search helpers); the seat's model is the costliest one
        out.model = max(mu, key=lambda k: float(mu[k].get("costUSD") or 0)) if mu else model
        sub = result.get("subtype", "")
        if "budget" in sub:
            out.stopped_by_budget = True
            out.error = out.error or "seat budget exhausted"
        elif result.get("is_error") and not out.error:
            out.error = f"{sub}: {out.text[-300:]}"
        if json_schema and out.structured is None and not out.error:
            try:
                out.structured = json.loads(out.text[out.text.index("{"): out.text.rindex("}") + 1])
            except ValueError:
                out.error = "no structured output"
    elif not out.error:
        out.error = f"no result event; stderr: {stderr[-300:]}"

    telemetry.end_span(span, attrs={
        "gen_ai.response.model": out.model, "gen_ai.usage.input_tokens": out.input_tokens,
        "gen_ai.usage.output_tokens": out.output_tokens, "gen_ai.usage.cache_read_tokens": out.cache_read_tokens,
        "fleet.cost_usd_list": out.cost_usd, "fleet.turns": out.turns, "fleet.tool_calls": len(out.tool_calls),
        "error.type": out.error or ""})
    if on_event:
        on_event({"type": "seat_done", "seat": seat, "ok": out.ok, "cost_usd": out.cost_usd,
                  "duration_s": out.duration_s, "turns": out.turns, "error": out.error})
    return out
