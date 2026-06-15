"""KNEMON-owned usage budget decisions backed by ``usage_ledger``."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from mnemos.core.config import get_settings
from mnemos.domain.knemon.router import _rows, _to_float

logger = logging.getLogger(__name__)


class BudgetVerdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class BudgetDecision:
    verdict: BudgetVerdict
    remaining_usd: float
    reason: str
    spent_usd: float = 0.0
    limit_usd: float | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is BudgetVerdict.ALLOW


async def weekly_spend_usd(
    backend: Any,
    *,
    caller_subsystem: str = "pantheon",
    now: datetime | None = None,
) -> float:
    """Return the current rolling-seven-day spend from KNEMON's ledger."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=7)
    rows = await _rows(
        backend,
        """
        SELECT COALESCE(SUM(est_cost_usd), 0) AS spent_usd
        FROM usage_ledger
        WHERE caller_subsystem = :caller_subsystem
          AND ts >= :since_ts
        """,
        {"caller_subsystem": caller_subsystem, "since_ts": since},
    )
    return _to_float((rows[0] if rows else {}).get("spent_usd"), 0.0)


async def evaluate_usage_budget(
    backend: Any,
    *,
    estimated_cost_usd: float = 0.0,
    limit_usd: float | None = None,
    caller_subsystem: str = "pantheon",
    now: datetime | None = None,
) -> BudgetDecision:
    """Allow iff KNEMON ledger spend plus this request stays under weekly cap.

    KNEMON is the single owner of spend math: callers supply only an optional
    request-cost estimate and the ledger-backed backend. If no cap is configured
    (``limit_usd is None`` and ``MNEMOS_KNEMON_WEEKLY_BUDGET_CAP_USD <= 0``), the
    decision is unlimited. Missing/unavailable ledger backends fail open so
    PANTHEON remains usable in deployments that have not enabled KNEMON ledger
    persistence yet; over-cap ledgers deny before dispatch.
    """
    if limit_usd is None:
        configured = float(get_settings().knemon.weekly_budget_cap_usd)
        limit_usd = configured if configured > 0 else None
    if limit_usd is None:
        return BudgetDecision(BudgetVerdict.ALLOW, float("inf"), "no limit", limit_usd=None)
    if backend is None:
        return BudgetDecision(BudgetVerdict.ALLOW, limit_usd, "ledger unavailable", limit_usd=limit_usd)

    try:
        spent_usd = await weekly_spend_usd(backend, caller_subsystem=caller_subsystem, now=now)
    except Exception as exc:
        logger.debug("KNEMON budget ledger query failed; allowing request: %s", exc)
        return BudgetDecision(BudgetVerdict.ALLOW, limit_usd, "ledger unavailable", limit_usd=limit_usd)

    remaining = max(0.0, limit_usd - spent_usd)
    if spent_usd >= limit_usd:
        return BudgetDecision(
            BudgetVerdict.DENY,
            remaining,
            f"budget exhausted ({spent_usd:.4f}/{limit_usd:.4f})",
            spent_usd=spent_usd,
            limit_usd=limit_usd,
        )
    if estimated_cost_usd > 0 and (spent_usd + estimated_cost_usd) > limit_usd:
        return BudgetDecision(
            BudgetVerdict.DENY,
            remaining,
            f"estimated ${estimated_cost_usd:.4f} would exceed remaining ${remaining:.4f}",
            spent_usd=spent_usd,
            limit_usd=limit_usd,
        )
    return BudgetDecision(
        BudgetVerdict.ALLOW,
        remaining,
        "within budget",
        spent_usd=spent_usd,
        limit_usd=limit_usd,
    )


__all__ = ["BudgetDecision", "BudgetVerdict", "evaluate_usage_budget", "weekly_spend_usd"]
