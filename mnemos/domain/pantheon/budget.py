"""Per-tenant budget pre-gate for PANTHEON (GRAEAE mandate §F).

Pure decision arithmetic: decide allow/deny BEFORE a job is dispatched so an
out-of-budget tenant's request is rejected fast (402-style) and never consumes a
worker. Spend is supplied by the caller (read through the persistence ABC); this
module owns only the decision + the pre-call reservation check, never I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BudgetVerdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class BudgetDecision:
    verdict: BudgetVerdict
    remaining_usd: float
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict is BudgetVerdict.ALLOW


def evaluate_budget(
    *,
    spent_usd: float,
    limit_usd: float | None,
    estimated_cost_usd: float = 0.0,
) -> BudgetDecision:
    """Allow iff already-spent (+ this call's estimate) stays within the limit.

    ``limit_usd is None`` means unlimited. ``estimated_cost_usd`` is the optional
    pre-call reservation: if spending it would cross the limit, deny up-front
    rather than after the fact.
    """
    if limit_usd is None:
        return BudgetDecision(BudgetVerdict.ALLOW, float("inf"), "no limit")

    remaining = max(0.0, limit_usd - spent_usd)
    if spent_usd >= limit_usd:
        return BudgetDecision(BudgetVerdict.DENY, remaining, f"budget exhausted ({spent_usd:.4f}/{limit_usd:.4f})")
    if estimated_cost_usd > 0 and (spent_usd + estimated_cost_usd) > limit_usd:
        return BudgetDecision(
            BudgetVerdict.DENY,
            remaining,
            f"estimated ${estimated_cost_usd:.4f} would exceed remaining ${remaining:.4f}",
        )
    return BudgetDecision(BudgetVerdict.ALLOW, remaining, "within budget")
