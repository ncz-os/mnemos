"""Cost-per-quality triage for PANTHEON model selection."""

from __future__ import annotations

import logging
from typing import Any, Literal

from mnemos.core.numeric import safe_float
from mnemos.domain.graeae import model_registry
from mnemos.domain.pantheon import catalog

logger = logging.getLogger(__name__)

QualityNeed = Literal["low", "med", "medium", "high"]

_PRICE_ABSENT_LOGGED = False
_QUALITY_WEIGHTS = {
    "low": 0.40,
    "med": 0.55,
    "medium": 0.55,
    "high": 0.70,
}
_TASK_CAPABILITIES = {
    "chat": "chat",
    "code": "code",
    "reason": "reasoning",
    "reasoning": "reasoning",
    "embed": "embeddings",
    "embedding": "embeddings",
    "embeddings": "embeddings",
}


def _price_blended(model: dict[str, Any]) -> float | None:
    price_in = model.get("price_in", model.get("input_cost_per_mtok"))
    price_out = model.get("price_out", model.get("output_cost_per_mtok"))
    if price_in is not None and price_out is not None:
        return (safe_float(price_in) + safe_float(price_out)) / 2.0
    cost_per_mtok = model.get("cost_per_mtok")
    if cost_per_mtok is not None:
        return safe_float(cost_per_mtok)
    return None


def _ctx_factor(model: dict[str, Any], requested: int) -> float | None:
    model_max = model.get("model_max_ctx", model.get("context_window"))
    if model_max is None:
        return 0.3
    max_ctx = safe_float(model_max)
    if max_ctx <= 0:
        return 0.3
    if max_ctx < requested:
        return None
    return 1.0


def _newness_norm(model: dict[str, Any], values: list[float]) -> float:
    value = model_registry.newness_value(model)
    if value is None or not values:
        return 0.0
    low = min(values)
    high = max(values)
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _priority_ok(model: dict[str, Any], priority: str) -> bool:
    if priority in {"", "any", "auto", "standard"}:
        return True
    tier = str(model.get("usage_tier") or "")
    if priority in {"low", "cheap", "budget"}:
        return tier in {"", "budget", "standard", "agentic_ok"}
    if priority in {"high", "premium", "frontier"}:
        return tier in {"premium", "frontier", "consultation_only", "agentic_ok"}
    return True


def score_model(
    model: dict[str, Any],
    *,
    ctx_size: int,
    quality_need: QualityNeed = "med",
    newness_values: list[float] | None = None,
    price_weighting: bool = True,
) -> float | None:
    """Score one model with KNEMON cost-per-quality triage.

    ``None`` means the model is ineligible, currently only because its known
    context window is smaller than the requested context size.
    """
    ctx_factor = _ctx_factor(model, max(0, int(ctx_size)))
    if ctx_factor is None:
        return None
    perf_weight = _QUALITY_WEIGHTS.get(str(quality_need).lower(), _QUALITY_WEIGHTS["med"])
    numerator = model_registry.perf_rank(model) * perf_weight + _newness_norm(model, newness_values or []) * 0.25
    if not price_weighting:
        return numerator * ctx_factor
    price = _price_blended(model)
    if price is None:
        return numerator * ctx_factor
    return numerator / max(price, 0.01) * ctx_factor


async def recommend(
    task_kind: str,
    priority: str = "standard",
    ctx_size: int = 0,
    quality_need: QualityNeed = "med",
) -> dict[str, Any]:
    """Return the best PANTHEON catalog model for the requested task."""
    global _PRICE_ABSENT_LOGGED

    required_capability = _TASK_CAPABILITIES.get((task_kind or "chat").lower(), "chat")
    candidates = [
        model
        for model in await catalog.list_models()
        if model.get("available", True)
        and not model.get("deprecated", False)
        and required_capability in set(model.get("capabilities") or [])
        and _priority_ok(model, (priority or "standard").lower())
        and _ctx_factor(model, max(0, int(ctx_size))) is not None
    ]
    if not candidates:
        raise ValueError(f"no model can satisfy task_kind={task_kind!r} ctx_size={ctx_size}")

    price_weighting = all(_price_blended(model) is not None for model in candidates)
    if not price_weighting and not _PRICE_ABSENT_LOGGED:
        logger.warning("[PANTHEON] model_registry price columns absent; triage skipping price weighting")
        _PRICE_ABSENT_LOGGED = True

    newness_values = [
        value for value in (model_registry.newness_value(model) for model in candidates) if value is not None
    ]
    scored = [
        (
            score_model(
                model,
                ctx_size=ctx_size,
                quality_need=quality_need,
                newness_values=newness_values,
                price_weighting=price_weighting,
            ),
            model,
        )
        for model in candidates
    ]
    scored = [(score, model) for score, model in scored if score is not None]
    if not scored:
        raise ValueError(f"no model can satisfy task_kind={task_kind!r} ctx_size={ctx_size}")
    score, model = max(
        scored,
        key=lambda item: (
            float(item[0] or 0.0),
            safe_float(item[1].get("quality_score")),
            str(item[1].get("id") or ""),
        ),
    )
    return {**model, "triage_score": score}


__all__ = ["recommend", "score_model"]
