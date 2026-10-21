"""agents/fleet/seats.py — seat configs loaded from config/fleet_config.yaml + versioned prompts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agents.harness.security import UNTRUSTED_RULES

ROOT = Path(__file__).resolve().parents[2]
WEB_TOOLS = {"WebSearch", "WebFetch"}
WRITE_TOOLS = {"Write", "Edit", "Bash", "NotebookEdit"}


@dataclass
class SeatConfig:
    name: str
    tier: str
    temperature: float
    tools: list[str]
    max_turns: int
    budget_usd: float
    required: bool
    untrusted_input: bool
    prompt: str

    def system(self) -> str:
        return self.prompt + ("\n\n" + UNTRUSTED_RULES if self.untrusted_input or self.name in ("synthesizer", "editor", "critic", "judge") else "")


def load(path: Path | None = None) -> tuple[dict, dict[str, SeatConfig]]:
    cfg = yaml.safe_load((path or ROOT / "config" / "fleet_config.yaml").read_text())
    seats = {}
    for name, s in cfg["seats"].items():
        tools = list(s.get("tools") or [])
        # privilege reduction: a seat that reads the web never gets write/shell tools
        if WEB_TOOLS & set(tools):
            tools = [t for t in tools if t not in WRITE_TOOLS]
        seats[name] = SeatConfig(
            name=name, tier=s["tier"], temperature=float(s.get("temperature", 0.3)), tools=tools,
            max_turns=int(s.get("max_turns", 10)), budget_usd=float(s.get("budget_usd", 1.0)),
            required=bool(s.get("required", False)), untrusted_input=bool(s.get("untrusted_input", False)),
            prompt=(ROOT / "config" / "prompts" / f"{name}.md").read_text().strip())
    return cfg, seats
