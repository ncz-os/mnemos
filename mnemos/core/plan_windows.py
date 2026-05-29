"""Deterministic KNEMON plan-window identifiers.

Pure helper extracted from ``mnemos.api.routes.ledger`` so that lower layers
(``domain``, ``persistence``) may compute window ids without importing the api
layer. See the "mnemos layered architecture" import-linter contract.
"""

from __future__ import annotations

from datetime import datetime, timezone

_PLAN_WINDOWS: dict[tuple[str, str], tuple[str, int | None]] = {
    ("anthropic", "claude_max_200"): ("rolling", 18000),
    ("anthropic", "claude_max_100"): ("rolling", 18000),
    ("anthropic", "claude_max_interactive_post_jun15"): ("rolling", 18000),
    ("anthropic", "agent_sdk_credit_pool_post_jun15"): ("monthly", None),
    ("openai", "chatgpt_plus"): ("rolling", 10800),
    ("openai", "chatgpt_pro"): ("weekly", 604800),
    ("nvidia", "ngc_integrate"): ("monthly", None),
    ("nvidia", "ngc_inference"): ("monthly", None),
    ("groq", "dev_tier"): ("monthly", None),
    ("together", "api"): ("monthly", None),
    ("deepseek-direct", "api"): ("monthly", None),
    ("xai", "api"): ("monthly", None),
    ("xai", "supergrok"): ("monthly", None),
    ("gemini", "api"): ("monthly", None),
    ("perplexity", "api"): ("monthly", None),
}


def compute_plan_window_id(
    provider: str,
    plan_name: str,
    ts: datetime | None = None,
    *,
    reset_anchor: str | None = None,
    window_seconds: int | None = None,
) -> str:
    """Return the deterministic KNEMON plan-window id for a usage record."""
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    provider_key = provider.strip().lower()
    plan_key = plan_name.strip().lower()
    anchor, seconds = _PLAN_WINDOWS.get((provider_key, plan_key), ("monthly", None))
    anchor = (reset_anchor or anchor or "monthly").lower()
    seconds = int(window_seconds or seconds or 0)
    if anchor in {"daily", "day"}:
        return f"{provider_key}-{plan_key}-{ts:%Y-%m-%d}"
    if anchor in {"monthly", "month"}:
        return f"{provider_key}-{plan_key}-{ts:%Y-%m}"
    iso = ts.isocalendar()
    day_seconds = ts.hour * 3600 + ts.minute * 60 + ts.second
    window_index = day_seconds // max(seconds, 1)
    return f"{provider_key}-{plan_key}-{iso.year}-W{iso.week:02d}-d{iso.weekday}-w{window_index}"
