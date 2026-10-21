"""
agents/harness/compaction.py

Semantic compaction for hand-offs between seats. Inside a seat,
Claude Code compacts its own context automatically. Between seats, findings can
still be large, so before they reach the Synthesizer they are summarised by a
cheap model with a schema that keeps every fact, number and citation, instead
of cutting text at a character count.
"""
from __future__ import annotations

from agents.harness.claude_runner import run_seat

COMPACT_AT_CHARS = 24_000  # ~6k tokens; below this, pass findings through untouched

SCHEMA = {
    "type": "object", "required": ["summary", "facts"],
    "properties": {
        "summary": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "object", "required": ["claim", "source_url"],
                  "properties": {"claim": {"type": "string"}, "source_url": {"type": "string"},
                                 "number": {"type": "string"}}}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}


def compact(seat: str, findings: str, *, model: str, run_id: str, on_event=None) -> tuple[str, dict]:
    """Return (text_for_next_seat, stats). Never drops citations."""
    if len(findings) <= COMPACT_AT_CHARS:
        return findings, {"compacted": False, "chars_in": len(findings), "chars_out": len(findings), "cost_usd": 0.0}
    r = run_seat(
        f"compactor:{seat}", f"Compact these findings from the {seat} seat.\n\n{findings}", model=model, temperature=0.1,
        system=("You compact research findings for a downstream writer. Keep EVERY distinct factual claim, number, "
                "benchmark, version, date and its source URL. Drop repetition, filler and narrative. Never invent."),
        json_schema=SCHEMA, max_turns=3, budget_usd=0.40, run_id=run_id, on_event=on_event)
    if not r.ok or not r.structured:
        return findings, {"compacted": False, "error": r.error, "chars_in": len(findings), "chars_out": len(findings), "cost_usd": r.cost_usd}
    s = r.structured
    lines = [s.get("summary", ""), ""] + [
        f"- {f['claim']}" + (f" ({f['number']})" if f.get("number") else "") + f" [source]({f['source_url']})"
        for f in s.get("facts", [])]
    if s.get("open_questions"):
        lines += ["", "Open questions:"] + [f"- {q}" for q in s["open_questions"]]
    text = "\n".join(lines)
    return text, {"compacted": True, "chars_in": len(findings), "chars_out": len(text),
                  "facts_kept": len(s.get("facts", [])), "cost_usd": r.cost_usd}
