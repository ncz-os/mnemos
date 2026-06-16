"""Simple PANTHEON v0.1 routing policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.config import get_settings
from mnemos.domain.pantheon import catalog
from mnemos.domain.pantheon.aliases import PantheonRoutingError, resolve_alias
from mnemos.domain.pantheon.policy import resolve_with_policy


@dataclass(frozen=True)
class RouteDecision:
    alias: str
    provider: str
    model_id: str | None
    route_type: str
    reason: str
    model: dict[str, Any] | None = None
    task_type: str | None = None
    candidates: list[str] | None = None
    rolling_window_minutes: int | None = None
    scores: dict[str, dict[str, Any]] | None = None
    selection_reason: str | None = None

    def explain(self) -> dict[str, Any]:
        selection_reason = self.selection_reason or self.reason
        return {
            "alias": self.alias,
            "resolved_model": self.model_id,
            "provider": self.provider,
            "route_type": self.route_type,
            "reason": self.reason,
            "task_type": self.task_type,
            "candidates": self.candidates or ([] if self.route_type == "consensus" else [self.model_id]),
            "rolling_window_minutes": self.rolling_window_minutes,
            "scores": self.scores or {},
            "selected": self.model_id,
            "selection_reason": selection_reason,
            "resolution_chain": [
                {"step": "input", "value": self.alias},
                {
                    "step": "alias_resolution",
                    "type": self.route_type,
                    "resolved_model": self.model_id,
                    "provider": self.provider,
                },
                {"step": "policy", "reason": selection_reason},
            ],
            "model": self.model,
        }


def _body_quality_floor(body: dict[str, Any], default: float) -> float:
    pantheon = body.get("pantheon") if isinstance(body.get("pantheon"), dict) else {}
    raw = body.get("quality_floor", pantheon.get("quality_floor", default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _body_max_cost(body: dict[str, Any], default: float | None) -> float | None:
    pantheon = body.get("pantheon") if isinstance(body.get("pantheon"), dict) else {}
    raw = body.get("max_cost", pantheon.get("max_cost", default))
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _passthrough_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "passthrough_enabled", True))


def _passthrough_provider(settings: Any) -> str:
    provider = str(getattr(settings, "passthrough_provider", "nvidia") or "").strip()
    return provider or "nvidia"


_KNOWN_FREE_PASSTHROUGH_PROVIDERS = {"eih", "ngc", "nvidia"}
_FREE_TIER_VALUES = {
    "free",
    "free_tier",
    "free-tier",
    "ngc_free",
    "eih_free",
    "zero",
    "zero_cost",
    "zero-cost",
    "no_cost",
    "no-cost",
    "gratis",
}
_FREE_MARKER_KEYS = (
    "free",
    "is_free",
    "free_tier",
    "zero_cost",
    "cost_zero_by_design",
    "free_by_design",
    "no_cost",
)
_FREE_TIER_KEYS = (
    "tier",
    "usage_tier",
    "billing_tier",
    "plan_tier",
    "pricing_tier",
)


def _positive_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0.0 else default


def _free_marker_truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    if isinstance(raw, (int, float)):
        return raw != 0
    text = str(raw).strip().lower()
    return text in {"1", "true", "yes", "y", "on", "free"}


def _free_tier_text(raw: Any) -> bool:
    if raw is None:
        return False
    text = str(raw).strip().lower().replace(" ", "_")
    if text in _FREE_TIER_VALUES:
        return True
    return text.startswith("free_") or text.endswith("_free")


def _config_marks_free(config: dict[str, Any]) -> bool:
    for key in _FREE_MARKER_KEYS:
        if _free_marker_truthy(config.get(key)):
            return True
    for key in _FREE_TIER_KEYS:
        if _free_tier_text(config.get(key)):
            return True
    for key in ("pricing_note", "notes", "description"):
        note = config.get(key)
        if isinstance(note, str) and ("free tier" in note.lower() or "zero cost by design" in note.lower()):
            return True
    return False


def _provider_config_candidates(provider: str, model: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = [model]
    provider_names = {provider}
    try:
        from mnemos.core.config import GRAEAE_CONFIG

        cfg = (GRAEAE_CONFIG.get("providers") or {}).get(provider)
        if isinstance(cfg, dict):
            provider_cfg = dict(cfg)
            candidates.append(provider_cfg)
            key_name = str(provider_cfg.get("key_name") or "").strip()
            if key_name:
                provider_names.add(key_name)
    except Exception:
        pass
    try:
        from mnemos.domain.graeae.api_keys import get_provider_config

        for name in provider_names:
            cfg = get_provider_config(name)
            if cfg:
                candidates.append(cfg)
    except Exception:
        pass
    return candidates


def _is_free_passthrough(provider: str, model: dict[str, Any]) -> bool:
    provider_name = provider.strip().lower()
    if provider_name in _KNOWN_FREE_PASSTHROUGH_PROVIDERS:
        return True
    return any(_config_marks_free(config) for config in _provider_config_candidates(provider, model))


def _pricing_missing(raw: Any) -> bool:
    try:
        return float(raw) <= 0.0
    except (TypeError, ValueError):
        return True


def _passthrough_default_model(model_id: str, provider: str, settings: Any) -> dict[str, Any]:
    input_cost = _positive_float(getattr(settings, "passthrough_default_input_cost_per_mtok", 5.0), 5.0)
    output_cost = _positive_float(getattr(settings, "passthrough_default_output_cost_per_mtok", 30.0), 30.0)
    return {
        "id": model_id,
        "object": "model",
        "owned_by": provider,
        "provider": provider,
        "registry_provider": provider,
        "display_name": model_id,
        "capabilities": ["chat", "embeddings"],
        "usage_tier": "frontier",
        "cost_per_mtok": (input_cost + output_cost) / 2.0,
        "price_in": input_cost,
        "price_out": output_cost,
        "input_cost_per_mtok": input_cost,
        "output_cost_per_mtok": output_cost,
        "available": True,
        "deprecated": False,
        "pricing_source": "passthrough-default",
    }


def _model_id_variants(model_id: str) -> set[str]:
    raw = model_id.strip().lower()
    variants = {raw} if raw else set()
    if raw.startswith("nvcf/"):
        variants.add(raw.split("/", 1)[1])
    parts = raw.split("/")
    if len(parts) > 2:
        variants.add("/".join(parts[1:]))
    return variants


def _catalog_pricing_model(model_id: str, provider: str, models: list[dict[str, Any]]) -> dict[str, Any] | None:
    requested = _model_id_variants(model_id)
    matches: list[dict[str, Any]] = []
    for model in models:
        ids = {
            str(model.get("id") or ""),
            str(model.get("model_id") or ""),
            str(model.get("pricing_raw_model_id") or ""),
        }
        aliases = set(ids)
        for mid in ids:
            if not mid:
                continue
            aliases.add(f"{model.get('provider')}/{mid}")
            aliases.add(f"{model.get('registry_provider')}/{mid}")
        if requested.intersection(variant for alias in aliases for variant in _model_id_variants(alias)):
            matches.append(model)
    if not matches:
        return None
    for model in matches:
        if str(model.get("provider") or "").strip() == provider:
            return dict(model)
    return dict(matches[0])


def _passthrough_model(model_id: str, provider: str, settings: Any, models: list[dict[str, Any]]) -> dict[str, Any]:
    model = _catalog_pricing_model(model_id, provider, models)
    defaults = _passthrough_default_model(model_id, provider, settings)
    if model is None:
        model = dict(defaults)
    free_passthrough = _is_free_passthrough(provider, model)
    if free_passthrough:
        for key in ("input_cost_per_mtok", "output_cost_per_mtok", "price_in", "price_out", "cost_per_mtok"):
            model[key] = 0.0
        model["pricing_source"] = "passthrough-free"
    else:
        for key in ("input_cost_per_mtok", "output_cost_per_mtok", "price_in", "price_out"):
            if _pricing_missing(model.get(key)):
                model[key] = defaults[key]
    if not free_passthrough and _pricing_missing(model.get("cost_per_mtok")):
        model["cost_per_mtok"] = (
            _positive_float(model.get("input_cost_per_mtok"), defaults["input_cost_per_mtok"])
            + _positive_float(model.get("output_cost_per_mtok"), defaults["output_cost_per_mtok"])
        ) / 2.0
    model["id"] = model_id
    model["provider"] = provider
    model["registry_provider"] = provider
    model.setdefault("usage_tier", "frontier")
    return model


def _passthrough_decision(model_id: str, settings: Any, models: list[dict[str, Any]]) -> RouteDecision:
    provider = _passthrough_provider(settings)
    return RouteDecision(
        alias=model_id,
        provider=provider,
        model_id=model_id,
        route_type="passthrough",
        reason=(
            "explicit model id is not in the PANTHEON catalog; "
            f"using fixed pass-through provider={provider}"
        ),
        model=_passthrough_model(model_id, provider, settings, models),
        candidates=[model_id],
    )


def _wire_model_id(model: dict[str, Any] | None) -> str | None:
    if not isinstance(model, dict):
        return None
    raw = model.get("model_id") or model.get("id")
    return str(raw) if raw is not None else None


def _is_explicit_passthrough_candidate(model_or_alias: str) -> bool:
    requested = model_or_alias.strip()
    return bool(requested) and not requested.startswith("auto:") and not requested.startswith("consensus:")


async def route_model(model_or_alias: str, body: dict[str, Any] | None = None) -> RouteDecision:
    settings = get_settings().pantheon
    request_body = body or {}
    quality_floor = _body_quality_floor(request_body, settings.default_quality_floor)
    max_cost = _body_max_cost(request_body, settings.default_max_cost_usd_per_mtok)
    models = await catalog.list_models()
    try:
        resolved = resolve_alias(
            model_or_alias,
            models,
            quality_floor=quality_floor,
            max_cost=max_cost,
        )
    except PantheonRoutingError as exc:
        if (
            exc.status_code == 404
            and _passthrough_enabled(settings)
            and _is_explicit_passthrough_candidate(model_or_alias)
        ):
            return _passthrough_decision(model_or_alias.strip(), settings, models)
        raise
    candidates = [
        str(candidate.get("id"))
        for candidate in resolved.get("candidates", [])
        if isinstance(candidate, dict) and candidate.get("id")
    ]
    scores: dict[str, dict[str, Any]] | None = None
    selection_reason: str | None = None
    rolling_window_minutes: int | None = None
    if resolved["type"] == "auto":
        rolling_window_minutes = settings.routing_window_minutes
        policy_route = await resolve_with_policy(
            _lc._pool,
            resolved["alias"],
            list(resolved.get("candidates") or []),
            window_minutes=rolling_window_minutes,
        )
        selected = policy_route.selected
        resolved = {
            **resolved,
            "provider": selected["provider"],
            "resolved_model": _wire_model_id(selected) or selected["id"],
            "model": selected,
            "reason": policy_route.selection_reason,
        }
        candidates = policy_route.candidates
        scores = policy_route.scores
        selection_reason = policy_route.selection_reason
    return RouteDecision(
        alias=resolved["alias"],
        provider=resolved["provider"],
        model_id=_wire_model_id(resolved.get("model")) or resolved["resolved_model"],
        route_type=resolved["type"],
        reason=resolved["reason"],
        model=resolved["model"],
        task_type=resolved.get("task_type"),
        candidates=candidates,
        rolling_window_minutes=rolling_window_minutes,
        scores=scores,
        selection_reason=selection_reason,
    )


def build_fallback_chain(
    primary: RouteDecision,
    models: list[dict[str, Any]],
    *,
    max_chain: int = 4,
) -> list[RouteDecision]:
    """Expand a resolved primary into a cross-provider fallback chain.

    The alias's own ranked candidate pool (``primary.candidates``) is the natural
    fallback set: ``primary`` first, then each DISTINCT ``(provider, model_id)``
    candidate resolved against the ``models`` catalog. Pure (the caller supplies
    the catalog). Consensus decisions are returned as a single-element chain
    because they are handled by the consultation path. Bounded by ``max_chain``.
    """
    if primary.route_type == "consensus":
        return [primary]
    by_id: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for m in models:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid:
            continue
        mid = str(mid)
        if mid in by_id and by_id[mid].get("provider") != m.get("provider"):
            ambiguous.add(mid)  # same id across providers -> can't disambiguate from a bare id
        else:
            by_id[mid] = m
    chain = [primary]
    seen = {(primary.provider, primary.model_id)}
    for cid in primary.candidates or []:
        cid = str(cid)
        if cid in ambiguous:
            continue
        m = by_id.get(cid)
        if not m:
            continue
        provider = m.get("provider")
        wire_model_id = _wire_model_id(m) or cid
        key = (provider, wire_model_id)
        if not provider or key in seen:
            continue
        seen.add(key)
        chain.append(
            RouteDecision(
                alias=primary.alias,
                provider=str(provider),
                model_id=wire_model_id,
                route_type="single",
                reason="fallback-candidate",
                model=m,
                task_type=primary.task_type,
            )
        )
        if len(chain) >= max_chain:
            break
    return chain


async def explain_route(body: dict[str, Any]) -> dict[str, Any]:
    model = str(body.get("model") or body.get("model_or_alias") or "auto:cheap")
    decision = await route_model(model, body)
    return decision.explain()


__all__ = ["PantheonRoutingError", "RouteDecision", "build_fallback_chain", "explain_route", "route_model"]
