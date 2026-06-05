"""Task-aware model recommendation policy for provider registry rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mnemos.core.numeric import safe_float


@dataclass(frozen=True)
class RecommendationPolicy:
    task_type: str
    required_caps: tuple[str, ...]
    any_caps: tuple[str, ...] = ()
    excluded_caps: tuple[str, ...] = ()
    max_context_window: int | None = None
    cost_ceiling: float | None = None
    allowed_tiers: tuple[str, ...] = ()
    fallback_tiers: tuple[str, ...] = ()
    preferred_models: tuple[tuple[str, str], ...] = ()
    preferred_name_tokens: tuple[str, ...] = ()
    excluded_name_tokens: tuple[str, ...] = ()


_TASK_ALIASES = {
    "code-fix": "coding",
    "code_fix": "coding",
    "code-generation": "coding",
    "code_generation": "coding",
    "coding": "coding",
    "narrative": "narrative",
    "chat": "narrative",
    "summarize": "narrative",
    "summarization": "narrative",
    "copywriting": "narrative",
    "reasoning": "reasoning",
    "reason": "reasoning",
    "embedding": "embedding",
    "embeddings": "embedding",
    "embed": "embedding",
    "routing": "routing",
    "classification": "routing",
    "classify": "routing",
    "web_search": "web_search",
    "web-search": "web_search",
    "search": "web_search",
    "architecture_design": "reasoning",
}

_POLICIES = {
    "coding": RecommendationPolicy(
        task_type="coding",
        required_caps=("code",),
        excluded_caps=("embedding",),
        cost_ceiling=10.0,
        allowed_tiers=("A", "B"),
        fallback_tiers=("C",),
        preferred_models=(
            ("nvidia", "qwen/qwen3-coder-480b"),
            ("deepseek-direct", "deepseek-coder"),
            ("deepseek-direct", "deepseek-v4-flash"),
            ("anthropic", "claude-sonnet"),
        ),
        preferred_name_tokens=("coder", "deepseek", "sonnet"),
    ),
    "narrative": RecommendationPolicy(
        task_type="narrative",
        required_caps=("chat",),
        excluded_caps=("embedding", "routing"),
        cost_ceiling=10.0,
        allowed_tiers=("A", "B"),
        preferred_models=(
            ("anthropic", "claude-sonnet-4-6"),
            ("anthropic", "claude-sonnet"),
            ("gemini", "gemini-2.5-flash"),
        ),
        preferred_name_tokens=("sonnet", "gemini", "flash"),
        excluded_name_tokens=("opus",),
    ),
    "reasoning": RecommendationPolicy(
        task_type="reasoning",
        required_caps=("reasoning",),
        excluded_caps=("embedding",),
        cost_ceiling=50.0,
        allowed_tiers=("A", "B", "C"),
        preferred_models=(
            ("anthropic", "claude-opus-4-7"),
            ("anthropic", "claude-opus-4-6"),
            ("nvidia", "deepseek-v4-pro"),
            ("deepseek-direct", "deepseek-v4-pro"),
            ("openai", "gpt-5.5"),
        ),
        preferred_name_tokens=("opus", "deepseek-v4-pro", "gpt-5.5"),
    ),
    "embedding": RecommendationPolicy(
        task_type="embedding",
        required_caps=("embedding",),
        excluded_caps=("chat",),
        cost_ceiling=1.0,
        allowed_tiers=("A",),
        fallback_tiers=("B",),
        preferred_models=(
            ("mnemos-local", "bge-m3"),
            ("local", "bge-m3"),
            ("openai", "text-embedding-3-small"),
            ("openai", "text-embedding-3-large"),
            ("voyage", "voyage-3"),
        ),
        preferred_name_tokens=("bge-m3", "voyage-3", "text-embedding-3"),
    ),
    "routing": RecommendationPolicy(
        task_type="routing",
        required_caps=(),
        any_caps=("routing", "chat"),
        excluded_caps=("embedding",),
        max_context_window=32768,
        cost_ceiling=1.0,
        allowed_tiers=("A",),
        fallback_tiers=("B",),
        preferred_models=(
            ("groq", "llama-3.1-8b-instant"),
            ("nvidia", "kimi-k2.6"),
            ("nvidia", "moonshotai/kimi-k2.6"),
        ),
        preferred_name_tokens=("llama-3.1-8b-instant", "kimi-k2.6"),
    ),
    "web_search": RecommendationPolicy(
        task_type="web_search",
        required_caps=("web_search",),
        excluded_caps=("embedding",),
        cost_ceiling=10.0,
        allowed_tiers=("A", "B"),
        preferred_models=(("perplexity", "sonar"), ("perplexity", "sonar-pro")),
        preferred_name_tokens=("sonar",),
    ),
}

_CAP_ALIASES = {
    "code": "coding",
    "coder": "coding",
    "embeddings": "embedding",
    "embed": "embedding",
    "online": "web_search",
    "search": "web_search",
    "internet": "web_search",
    "logic": "reasoning",
}

_SPECIAL_PURPOSE_MODEL_TOKENS = ("content-safety", "safety", "moderation", "guardrail")


def recommendation_policy(task_type: str) -> RecommendationPolicy:
    canonical = _TASK_ALIASES.get(task_type.strip().lower(), task_type.strip().lower())
    return _POLICIES.get(canonical, _POLICIES["narrative"])


def default_quality_floor(task_type: str, requested_floor: float) -> float:
    if requested_floor <= 0:
        return 0.7
    return requested_floor


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _canonical_cap(capability: str) -> str:
    cap = capability.strip().lower().replace("-", "_")
    return _CAP_ALIASES.get(cap, cap)


def _capability_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
    else:
        parsed = value

    caps: set[str] = set()
    if isinstance(parsed, dict):
        for key, enabled in parsed.items():
            if enabled is True:
                caps.add(_canonical_cap(str(key)))
    elif isinstance(parsed, (list, tuple, set)):
        caps.update(_canonical_cap(str(cap)) for cap in parsed if str(cap).strip())
    return caps


def _avg_cost(row: Any) -> float | None:
    in_cost = _row_get(row, "input_cost_per_mtok")
    out_cost = _row_get(row, "output_cost_per_mtok")
    if in_cost is None or out_cost is None:
        return None
    return (safe_float(in_cost) + safe_float(out_cost)) / 2.0


def _tier(row: Any) -> str:
    raw = str(_row_get(row, "cost_tier") or _row_get(row, "usage_tier") or "").strip().upper()
    if raw in {"A", "B", "C"}:
        return raw
    cost = _avg_cost(row)
    if cost is not None:
        if cost <= 1.0:
            return "A"
        if cost <= 10.0:
            return "B"
        return "C"
    quality = _quality(row)
    if quality >= 0.95:
        return "C"
    if quality >= 0.85:
        return "B"
    return "A"


def _quality(row: Any) -> float:
    return safe_float(_row_get(row, "graeae_weight") or _row_get(row, "quality_score") or 0)


def _context_window(row: Any) -> int | None:
    value = _row_get(row, "context_window")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _model_key(row: Any) -> tuple[str, str]:
    return str(_row_get(row, "provider") or ""), str(_row_get(row, "model_id") or "")


def _model_matches_preference(row: Any, preferred: tuple[str, str]) -> bool:
    provider, model_id = _model_key(row)
    preferred_provider, preferred_model = preferred
    return provider == preferred_provider and (
        model_id == preferred_model or model_id.startswith(preferred_model) or preferred_model in model_id
    )


def _is_special_purpose(row: Any) -> bool:
    provider, model_id = _model_key(row)
    display_name = str(_row_get(row, "display_name") or "")
    text = f"{provider}/{model_id} {display_name}".lower()
    return any(token in text for token in _SPECIAL_PURPOSE_MODEL_TOKENS)


def _model_text(row: Any) -> str:
    provider, model_id = _model_key(row)
    display_name = str(_row_get(row, "display_name") or "")
    return f"{provider}/{model_id} {display_name}".lower()


def _has_name_token(row: Any, tokens: tuple[str, ...]) -> bool:
    text = _model_text(row)
    return any(token.lower() in text for token in tokens)


def _caps_match(caps: set[str], policy: RecommendationPolicy) -> bool:
    if policy.required_caps and not set(policy.required_caps).issubset(caps):
        return False
    if policy.any_caps and not any(cap in caps for cap in policy.any_caps):
        return False
    if any(cap in caps for cap in policy.excluded_caps):
        return False
    return True


def _tier_filter(rows: list[Any], policy: RecommendationPolicy) -> list[Any]:
    if not policy.allowed_tiers:
        return rows
    allowed = [row for row in rows if _tier(row) in set(policy.allowed_tiers)]
    if allowed:
        return allowed
    if not policy.fallback_tiers:
        return []
    return [row for row in rows if _tier(row) in set(policy.fallback_tiers)]


def _preference_rank(row: Any, policy: RecommendationPolicy) -> int:
    for index, preferred in enumerate(policy.preferred_models):
        if _model_matches_preference(row, preferred):
            return index
    if policy.preferred_name_tokens and _has_name_token(row, policy.preferred_name_tokens):
        return len(policy.preferred_models)
    return len(policy.preferred_models) + 1


def choose_recommended_model(
    rows: list[Any],
    task_type: str,
    cost_budget: float,
    quality_floor: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    policy = recommendation_policy(task_type)
    effective_floor = max(0.0, quality_floor or 0.7)
    budget = policy.cost_ceiling if policy.cost_ceiling is not None else cost_budget

    capable = []
    for row in rows:
        caps = _capability_set(_row_get(row, "capabilities"))
        if not _caps_match(caps, policy):
            continue
        if policy.max_context_window is not None:
            context = _context_window(row)
            if context is not None and context > policy.max_context_window:
                continue
        capable.append(row)

    capable = [row for row in capable if not _is_special_purpose(row)]
    if policy.excluded_name_tokens:
        capable = [row for row in capable if not _has_name_token(row, policy.excluded_name_tokens)]
    capable = _tier_filter(capable, policy)

    qualified = [
        row
        for row in capable
        if _quality(row) >= effective_floor and _avg_cost(row) is not None and (_avg_cost(row) or 0.0) <= budget
    ]

    if qualified:
        chosen = sorted(
            qualified,
            key=lambda row: (
                _preference_rank(row, policy),
                _avg_cost(row) or 0.0,
                -_quality(row),
            ),
        )[0]
        return _format_model(chosen), list(policy.required_caps or policy.any_caps)

    degraded = [row for row in capable if _quality(row) >= effective_floor]
    if degraded:
        chosen = sorted(
            degraded,
            key=lambda row: (
                _preference_rank(row, policy),
                _avg_cost(row) is None,
                _avg_cost(row) or 0.0,
                -_quality(row),
            ),
        )[0]
        return _format_model(chosen), list(policy.required_caps or policy.any_caps)

    return None, list(policy.required_caps or policy.any_caps)


def _format_model(row: Any) -> dict[str, Any]:
    return {
        "provider": _row_get(row, "provider"),
        "model_id": _row_get(row, "model_id"),
        "display_name": _row_get(row, "display_name"),
        "cost_per_mtok": _avg_cost(row),
        "quality_score": _quality(row),
        "context_window": _row_get(row, "context_window"),
    }
