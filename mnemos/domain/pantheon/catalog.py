"""PANTHEON model catalog derived from the GRAEAE muses registry."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.numeric import safe_float
from mnemos.core.provider_registry import GRAEAE_REGISTRY_MAP
from mnemos.domain.graeae.engine import get_graeae_engine

logger = logging.getLogger(__name__)
_DEFAULT_CACHE_PATHS = (
    "data/pantheon_catalog.json",
    "data/pantheon/catalog.json",
    "data/llm_provider_registry.json",
    "llm_provider_registry.json",
)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _registry_provider(graeae_provider: str) -> str:
    mapping = GRAEAE_REGISTRY_MAP.get(graeae_provider)
    return mapping["registry_provider"] if mapping else graeae_provider


def _cost_per_mtok(source: dict[str, Any]) -> float | None:
    explicit = source.get("cost_per_mtok")
    if explicit is not None:
        return safe_float(explicit)
    in_cost = source.get("price_in", source.get("input_cost_per_mtok"))
    out_cost = source.get("price_out", source.get("output_cost_per_mtok"))
    if in_cost is None or out_cost is None:
        cost = source.get("cost")
        if isinstance(cost, dict):
            in_cost = cost.get("input")
            out_cost = cost.get("output")
    if in_cost is None or out_cost is None:
        return None
    return (safe_float(in_cost) + safe_float(out_cost)) / 2.0


def _quality_score(source: dict[str, Any], provider_cfg: dict[str, Any]) -> float:
    for key in ("quality_score", "graeae_weight", "weight"):
        if source.get(key) is not None:
            return safe_float(source[key])
    if provider_cfg.get("weight") is not None:
        return safe_float(provider_cfg["weight"])
    return 0.0


def _infer_capabilities(model_id: str, provider_cfg: dict[str, Any]) -> list[str]:
    configured = provider_cfg.get("capabilities")
    if isinstance(configured, (list, tuple, set)):
        return sorted({str(cap).strip() for cap in configured if str(cap).strip()})

    caps = {"chat"}
    mid = model_id.lower()
    api = str(provider_cfg.get("api") or "").lower()

    if "embed" in mid:
        caps.add("embeddings")
    if any(token in mid for token in ("code", "coder", "codestral")):
        caps.add("code")
    if any(token in mid for token in ("reason", "thinking", "r1", "qwq", "o3", "o4")):
        caps.add("reasoning")
    if any(token in mid for token in ("claude", "gpt-5", "grok-4", "gemini-3")):
        caps.add("reasoning")
    if any(token in mid for token in ("vision", "vl", "4o", "gemini", "claude", "grok")):
        caps.add("vision")
    if any(token in mid for token in ("sonar", "search", "online", "perplexity")):
        caps.add("web_search")
    if api == "gemini":
        caps.add("vision")

    return sorted(caps)


def _usage_tier(source: dict[str, Any], quality_score: float, cost: float | None) -> str:
    explicit = source.get("usage_tier") or source.get("tier")
    if explicit:
        return str(explicit)
    if quality_score >= 0.95:
        return "frontier"
    if quality_score >= 0.85:
        return "premium"
    if cost is not None and cost <= 1.0:
        return "budget"
    return "standard"


def _provider_health(provider: str, status: dict[str, Any]) -> dict[str, Any]:
    def _as_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    circuit = _as_dict((status.get("circuit_breakers") or {}).get(provider))
    quality = _as_dict((status.get("quality") or {}).get(provider))
    rate_limiter_raw = (status.get("rate_limiters") or {}).get(provider)
    # Some resilience backends return per-provider counters as scalars
    # (e.g. an int for "current limited count"). Coerce shape so the
    # health response stays consistent regardless of backend variant.
    if isinstance(rate_limiter_raw, dict):
        rate_limited_value: Any = rate_limiter_raw.get("limited")
    else:
        rate_limited_value = rate_limiter_raw
    concurrency = (status.get("concurrency") or {}).get(provider, {})
    return {
        "state": circuit.get("state") or "unknown",
        "success_rate": quality.get("success_rate"),
        "p50_latency_ms": quality.get("p50_latency_ms"),
        "rate_limited": rate_limited_value,
        "concurrency": concurrency,
    }


def _normalize_model(
    *,
    provider: str,
    provider_cfg: dict[str, Any],
    model_source: dict[str, Any],
    health: dict[str, Any],
) -> dict[str, Any]:
    model_id = str(model_source.get("model_id") or model_source.get("id") or provider_cfg.get("model") or "")
    display_name = str(model_source.get("display_name") or model_source.get("name") or model_id)
    capabilities = model_source.get("capabilities")
    if not isinstance(capabilities, (list, tuple, set)):
        capabilities = _infer_capabilities(model_id, provider_cfg)
    capabilities = sorted({str(cap).strip() for cap in capabilities if str(cap).strip()})
    cost = _cost_per_mtok({**provider_cfg, **model_source})
    quality_score = _quality_score(model_source, provider_cfg)
    p50_latency_ms = model_source.get("p50_latency_ms") or provider_cfg.get("p50_latency_ms")
    if p50_latency_ms is None:
        p50_latency_ms = health.get("p50_latency_ms")

    return {
        "id": model_id,
        "object": "model",
        "created": int(model_source.get("created") or provider_cfg.get("created") or time.time()),
        "owned_by": str(model_source.get("owned_by") or provider),
        "provider": provider,
        "registry_provider": _registry_provider(provider),
        "display_name": display_name,
        "capabilities": capabilities,
        "usage_tier": _usage_tier({**provider_cfg, **model_source}, quality_score, cost),
        "cost_per_mtok": cost,
        "price_in": model_source.get("price_in") or provider_cfg.get("price_in"),
        "price_out": model_source.get("price_out") or provider_cfg.get("price_out"),
        "input_cost_per_mtok": model_source.get("input_cost_per_mtok") or provider_cfg.get("input_cost_per_mtok"),
        "output_cost_per_mtok": model_source.get("output_cost_per_mtok") or provider_cfg.get("output_cost_per_mtok"),
        "quality_score": quality_score,
        "arena_rank": model_source.get("arena_rank") or provider_cfg.get("arena_rank"),
        "graeae_weight": model_source.get("graeae_weight") or provider_cfg.get("graeae_weight"),
        "release_date": model_source.get("release_date") or provider_cfg.get("release_date"),
        "last_synced": model_source.get("last_synced") or provider_cfg.get("last_synced"),
        "context_window": model_source.get("context_window") or provider_cfg.get("context_window"),
        "model_max_ctx": model_source.get("model_max_ctx")
        or model_source.get("context_window")
        or provider_cfg.get("model_max_ctx")
        or provider_cfg.get("context_window"),
        "max_output_tokens": model_source.get("max_output_tokens") or provider_cfg.get("max_output_tokens"),
        "p50_latency_ms": p50_latency_ms,
        "available": bool(model_source.get("available", provider_cfg.get("available", True))),
        "deprecated": bool(model_source.get("deprecated", provider_cfg.get("deprecated", False))),
        "health": health,
    }


async def _registry_rows() -> list[Any]:
    pool = _lc._pool
    if pool is None:
        return []
    queries = [
        """
        SELECT provider, model_id, display_name, capabilities,
               price_in, price_out, input_cost_per_mtok, output_cost_per_mtok,
               context_window, model_max_ctx, max_output_tokens,
               arena_rank, COALESCE(graeae_weight, 0) AS graeae_weight,
               release_date, last_synced, available, deprecated
        FROM model_registry
        WHERE available = true
        ORDER BY provider, model_id
        """,
        """
        SELECT provider, model_id, display_name, capabilities,
               input_cost_per_mtok, output_cost_per_mtok,
               context_window, max_output_tokens, arena_rank,
               COALESCE(graeae_weight, 0) AS graeae_weight,
               last_synced, available, deprecated
        FROM model_registry
        WHERE available = true
        ORDER BY provider, model_id
        """,
    ]
    try:
        async with pool.acquire() as conn:
            for query in queries:
                try:
                    return list(await conn.fetch(query))
                except Exception as exc:
                    logger.debug("[PANTHEON] model_registry catalog overlay unavailable: %s", exc)
            return []
    except Exception as exc:
        logger.debug("[PANTHEON] model_registry catalog overlay unavailable: %s", exc)
        return []


def _catalog_cache_paths() -> list[Path]:
    paths: list[str] = []
    try:
        configured = get_settings().pantheon.catalog_cache_path
        if configured:
            paths.append(str(configured))
    except Exception:
        pass
    env_path = os.environ.get("MNEMOS_PANTHEON_CATALOG_CACHE_PATH")
    if env_path:
        paths.append(env_path)
    paths.extend(_DEFAULT_CACHE_PATHS)
    cwd = Path.cwd()
    return [Path(p) if Path(p).is_absolute() else cwd / p for p in paths]


def _cache_payload_models(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("data") or payload.get("models") or payload.get("catalog") or []
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    models: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or item.get("owned_by") or "").strip()
        model_id = str(item.get("id") or item.get("model_id") or "").strip()
        if not provider or not model_id:
            continue
        cfg = {"model": model_id, "api": "openai", "weight": item.get("quality_score") or item.get("weight") or 0.0}
        normalized = _normalize_model(
            provider=provider,
            provider_cfg=cfg,
            model_source={**item, "model_id": model_id},
            health={"state": "cached"},
        )
        models.append(normalized)
    return models


def _cached_catalog_models() -> list[dict[str, Any]]:
    for path in _catalog_cache_paths():
        try:
            if not path.exists():
                continue
            models = _cache_payload_models(json.loads(path.read_text()))
            if models:
                logger.info("[PANTHEON] loaded cached phase-A catalog from %s", path)
                return models
        except Exception as exc:
            logger.debug("[PANTHEON] cached catalog %s unavailable: %s", path, exc)
    return []


def _model_sources(provider_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    raw_models = provider_cfg.get("models")
    if isinstance(raw_models, list) and raw_models:
        out: list[dict[str, Any]] = []
        for item in raw_models:
            if isinstance(item, dict):
                out.append(dict(item))
            elif item:
                out.append({"model_id": str(item)})
        return out
    return [{"model_id": provider_cfg.get("model")}]


async def list_models() -> list[dict[str, Any]]:
    """Return the extended PANTHEON model catalog.

    Prefer the phase-A cached provider catalog when present; fall back to the
    live GRAEAE registry/provider config otherwise.
    """
    cached = _cached_catalog_models()
    if cached:
        return sorted(
            cached,
            key=lambda item: (
                not item["available"],
                item["deprecated"],
                item["cost_per_mtok"] is None,
                item["cost_per_mtok"] if item["cost_per_mtok"] is not None else float("inf"),
                -float(item["quality_score"] or 0.0),
                item["id"],
            ),
        )
    engine = get_graeae_engine()
    try:
        provider_status = engine.provider_status()
    except Exception:
        provider_status = {}

    models: dict[tuple[str, str], dict[str, Any]] = {}
    for provider, cfg in engine.providers.items():
        provider_cfg = dict(cfg)
        health = _provider_health(provider, provider_status)
        for model_source in _model_sources(provider_cfg):
            if not model_source.get("model_id") and not model_source.get("id"):
                continue
            normalized = _normalize_model(
                provider=provider,
                provider_cfg=provider_cfg,
                model_source=model_source,
                health=health,
            )
            models[(normalized["provider"], normalized["id"])] = normalized

    registry_to_graeae = {cfg["registry_provider"]: name for name, cfg in GRAEAE_REGISTRY_MAP.items()}
    provider_cfgs = {name: dict(cfg) for name, cfg in engine.providers.items()}
    for row in await _registry_rows():
        registry_provider = str(_row_get(row, "provider") or "")
        provider = registry_to_graeae.get(registry_provider, registry_provider)
        provider_cfg = provider_cfgs.get(provider, {"model": _row_get(row, "model_id")})
        model_source = {
            "model_id": _row_get(row, "model_id"),
            "display_name": _row_get(row, "display_name"),
            "capabilities": _row_get(row, "capabilities") or [],
            "price_in": _row_get(row, "price_in"),
            "price_out": _row_get(row, "price_out"),
            "input_cost_per_mtok": _row_get(row, "input_cost_per_mtok"),
            "output_cost_per_mtok": _row_get(row, "output_cost_per_mtok"),
            "context_window": _row_get(row, "context_window"),
            "model_max_ctx": _row_get(row, "model_max_ctx"),
            "max_output_tokens": _row_get(row, "max_output_tokens"),
            "arena_rank": _row_get(row, "arena_rank"),
            "graeae_weight": _row_get(row, "graeae_weight"),
            "release_date": _row_get(row, "release_date"),
            "last_synced": _row_get(row, "last_synced"),
            "available": _row_get(row, "available", True),
            "deprecated": _row_get(row, "deprecated", False),
        }
        health = _provider_health(provider, provider_status)
        normalized = _normalize_model(
            provider=provider,
            provider_cfg=provider_cfg,
            model_source=model_source,
            health=health,
        )
        models[(normalized["provider"], normalized["id"])] = normalized

    return sorted(
        models.values(),
        key=lambda item: (
            not item["available"],
            item["deprecated"],
            item["cost_per_mtok"] is None,
            item["cost_per_mtok"] if item["cost_per_mtok"] is not None else float("inf"),
            -float(item["quality_score"] or 0.0),
            item["id"],
        ),
    )


async def models_response(
    *,
    filter_capabilities: list[str] | None = None,
    filter_tier: str | None = None,
    max_cost: float | None = None,
) -> dict[str, Any]:
    models = filter_models(
        await list_models(),
        filter_capabilities=filter_capabilities,
        filter_tier=filter_tier,
        max_cost=max_cost,
    )
    return {"object": "list", "data": models}


def filter_models(
    models: list[dict[str, Any]],
    *,
    filter_capabilities: list[str] | None = None,
    filter_tier: str | None = None,
    max_cost: float | None = None,
) -> list[dict[str, Any]]:
    required = {cap.strip() for cap in (filter_capabilities or []) if cap and cap.strip()}
    tier = filter_tier.strip() if isinstance(filter_tier, str) and filter_tier.strip() else None
    out: list[dict[str, Any]] = []
    for model in models:
        if required and not required.issubset(set(model.get("capabilities") or [])):
            continue
        if tier and model.get("usage_tier") != tier:
            continue
        cost = model.get("cost_per_mtok")
        if max_cost is not None and (cost is None or float(cost) > max_cost):
            continue
        out.append(model)
    return out


def find_model(models: list[dict[str, Any]], model_id: str) -> dict[str, Any] | None:
    for model in models:
        if model["id"] == model_id:
            return model
        namespaced = f"{model['provider']}/{model['id']}"
        registry_namespaced = f"{model['registry_provider']}/{model['id']}"
        if model_id in {namespaced, registry_namespaced}:
            return model
    return None
