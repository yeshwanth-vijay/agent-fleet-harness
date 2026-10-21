"""
agents/harness/budget.py

Active cost governance (the Cost Governor seat). Deterministic, no LLM.

- Every seat asks for an allowance before it runs and reports its spend after.
- The governor never lets a seat start with less than its floor left.
- At 80% of the run budget, seats are downgraded one tier (opus -> sonnet -> haiku).
- At 100%, optional seats are skipped and required seats get only what is left.
- Each seat's own hard cap is passed to Claude Code as --max-budget-usd, so a
  runaway loop inside a seat is stopped by the CLI itself.

Spend is the list-price estimate Claude Code reports. On a Claude plan it is a
proxy for plan usage, not a charge, but it is the right unit to govern.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

DOWNGRADE = {"opus": "sonnet", "sonnet": "haiku", "haiku": "haiku"}


@dataclass
class Allowance:
    allowed: bool
    tier: str
    budget_usd: float
    reason: str = ""


@dataclass
class BudgetGovernor:
    run_budget_usd: float
    downgrade_at: float = 0.80
    spent_usd: float = 0.0
    ledger: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def remaining(self) -> float:
        return max(0.0, self.run_budget_usd - self.spent_usd)

    @property
    def utilisation(self) -> float:
        return self.spent_usd / self.run_budget_usd if self.run_budget_usd else 1.0

    def request(self, seat: str, tier: str, seat_budget: float, *, required: bool, floor: float = 0.05) -> Allowance:
        with self._lock:
            if self.remaining < floor:
                if not required:
                    return Allowance(False, tier, 0, f"run budget exhausted ({self.spent_usd:.2f}/{self.run_budget_usd:.2f})")
                return Allowance(True, "haiku", max(floor, self.remaining), "budget exhausted; required seat on haiku with floor allowance")
            new_tier, reason = tier, ""
            if self.utilisation >= self.downgrade_at and tier != "haiku":
                new_tier, reason = DOWNGRADE[tier], f"downgraded {tier}->{DOWNGRADE[tier]} at {self.utilisation:.0%} of budget"
            return Allowance(True, new_tier, round(min(seat_budget, self.remaining), 2), reason)

    def record(self, seat: str, tier: str, cost_usd: float) -> None:
        with self._lock:
            self.spent_usd += cost_usd
            self.ledger.append({"seat": seat, "tier": tier, "cost_usd": round(cost_usd, 4),
                                "cumulative_usd": round(self.spent_usd, 4)})

    def snapshot(self) -> dict:
        return {"run_budget_usd": self.run_budget_usd, "spent_usd": round(self.spent_usd, 4),
                "remaining_usd": round(self.remaining, 4), "ledger": list(self.ledger)}
