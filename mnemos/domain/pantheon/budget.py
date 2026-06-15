"""PANTHEON budget pre-gate delegated to KNEMON.

PANTHEON does not compute spend. It supplies only a pre-dispatch cost estimate
and asks KNEMON to evaluate the ledger-backed weekly cap. Budget verdict types
are re-exported for the historical import path.
"""

from __future__ import annotations

from typing import Any

from mnemos.domain.knemon.budget import BudgetDecision, BudgetVerdict, evaluate_usage_budget


async def evaluate_budget(
    *,
    backend: Any,
    estimated_cost_usd: float = 0.0,
    limit_usd: float | None = None,
    caller_subsystem: str = "pantheon",
) -> BudgetDecision:
    """Delegate PANTHEON affordability checks to KNEMON's usage ledger."""
    return await evaluate_usage_budget(
        backend,
        estimated_cost_usd=estimated_cost_usd,
        limit_usd=limit_usd,
        caller_subsystem=caller_subsystem,
    )


__all__ = ["BudgetDecision", "BudgetVerdict", "evaluate_budget"]
