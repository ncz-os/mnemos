"""KNEMON hybrid model router."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from mnemos.core.plan_windows import compute_plan_window_id


class NoModelAvailable(RuntimeError):
    """Raised when the registry has no model satisfying hard constraints."""


@dataclass
class KnemonRouteRequest:
    task_kind: str
    priority: int
    est_tokens_in: int
    est_tokens_out: int
    caller_session_id: Optional[str]
    caller_subsystem: str
    exclude_providers: list[str] = field(default_factory=list)
    require_capability: list[str] = field(default_factory=list)


@dataclass
class KnemonRouteDecision:
    provider: str
    model_id: str
    auth_method: str
    path_kind: str
    estimated_cost_usd: float
    sub_window_utilization_pct: float
    fallback_chain: list[tuple]
    reasoning: str


_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_SESSION_BURN_REQUESTS_PER_HOUR = 10
_LOW_PRIORITY_API_COST_CEILING_USD = 0.50


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs) if callable(value) else value
    return await result if inspect.isawaitable(result) else result


def _conn_from_tx(tx: Any) -> Any:
    return getattr(tx, "conn", tx)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        pass
    lower = key.lower()
    try:
        return row[lower]
    except Exception:
        pass
    return getattr(row, key, getattr(row, lower, default))


async def _rows(backend: Any, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    async with backend.transactional() as tx:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(cursor.execute, sql, params or {})
            fetched = await _call(cursor.fetchall)
            description = getattr(cursor, "description", None) or []
            names = [str(col[0]).lower() for col in description]
            return [dict(zip(names, row)) for row in fetched or []]
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                await _call(close)


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return bool(value)
    return str(value).strip().lower() in {"1", "t", "true", "y", "yes"}


def _normalize_capabilities(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    raw = str(value).strip()
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return {str(item).strip() for item in parsed if str(item).strip()}
        if isinstance(parsed, dict):
            return {str(key).strip() for key, enabled in parsed.items() if enabled and str(key).strip()}
    except json.JSONDecodeError:
        pass
    return {item.strip().strip('"').strip("'") for item in raw.strip("{}[]").split(",") if item.strip()}


def _normalize_pool(value: Any) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip().lower()).strip("_")


def _normalize_pools(value: Any) -> set[str]:
    return {_normalize_pool(pool) for pool in _normalize_capabilities(value) if _normalize_pool(pool)}


def _subscription_pool_aliases(plan: dict[str, Any]) -> set[str]:
    provider = _normalize_pool(plan.get("provider"))
    plan_name = _normalize_pool(plan.get("plan_name"))
    parent_plan_id = _normalize_pool(plan.get("parent_plan_id"))
    aliases = {pool for pool in (plan_name, parent_plan_id) if pool}
    if provider:
        aliases.add(f"{provider}_subscription")
    if provider == "anthropic":
        aliases.add("claude_subscription")
    if provider == "openai":
        aliases.update({"chatgpt_subscription", "codex_subscription"})
    return aliases


async def _worker_pools_for_session(backend: Any, session_id: str | None) -> set[str] | None:
    if not session_id:
        return None
    try:
        rows = await _rows(
            backend,
            """
            SELECT subscription_pools
            FROM hive_agents
            WHERE session_id = :session_id
              AND status IN ('online', 'idle')
            ORDER BY last_heartbeat DESC
            FETCH FIRST 1 ROW ONLY
            """,
            {"session_id": session_id},
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "fetch" not in msg:
            if "hive_agents" in msg or "subscription_pools" in msg or "no such table" in msg or "no such column" in msg:
                return None
            raise
        try:
            rows = await _rows(
                backend,
                """
                SELECT subscription_pools
                FROM hive_agents
                WHERE session_id = :session_id
                  AND status IN ('online', 'idle')
                ORDER BY last_heartbeat DESC
                LIMIT 1
                """,
                {"session_id": session_id},
            )
        except Exception as fallback_exc:
            fallback_msg = str(fallback_exc).lower()
            if (
                "hive_agents" in fallback_msg
                or "subscription_pools" in fallback_msg
                or "no such table" in fallback_msg
                or "no such column" in fallback_msg
            ):
                return None
            raise
    if not rows:
        return None
    return _normalize_pools(rows[0].get("subscription_pools"))


def _worker_has_pool(worker_pools: set[str] | None, plan: dict[str, Any]) -> bool:
    if worker_pools is None:
        return True
    return bool(worker_pools.intersection(_subscription_pool_aliases(plan)))


def _quality(row: dict[str, Any]) -> float:
    for key in ("quality_score", "graeae_weight", "weight"):
        value = _to_float(row.get(key), -1.0)
        if value >= 0:
            return value / 100.0 if value > 1.0 else value
    arena = _to_float(row.get("arena_score"), 0.0)
    if arena > 0:
        return max(0.0, min(1.0, arena / 1500.0))
    return 0.0


def _tier(row: dict[str, Any]) -> str:
    raw = str(row.get("cost_tier") or row.get("usage_tier") or "").strip().upper()
    if raw in {"A", "B", "C"}:
        return raw
    quality = _quality(row)
    if quality >= 0.85:
        return "A"
    if quality >= 0.75:
        return "B"
    return "C"


def _price(row: dict[str, Any], tokens_in: int, tokens_out: int) -> float:
    return round(
        (
            max(0, tokens_in) * _to_float(row.get("input_cost_per_mtok"))
            + max(0, tokens_out) * _to_float(row.get("output_cost_per_mtok"))
        )
        / 1_000_000.0,
        6,
    )


async def _registry_candidates(req: KnemonRouteRequest, backend: Any) -> list[dict[str, Any]]:
    rows = await _rows(
        backend,
        """
        SELECT provider, model_id, display_name, capabilities,
               input_cost_per_mtok, output_cost_per_mtok,
               context_window, arena_score, graeae_weight,
               available, deprecated
        FROM model_registry
        ORDER BY graeae_weight DESC
        """,
    )
    excluded = {provider.strip().lower() for provider in req.exclude_providers if provider.strip()}
    required = {cap.strip() for cap in req.require_capability if cap.strip()}
    min_context = int(max(0, req.est_tokens_in) * 1.2)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        provider = str(row.get("provider") or "").strip()
        if not provider or provider.lower() in excluded:
            continue
        if not _truthy(row.get("available", True)) or _truthy(row.get("deprecated", False)):
            continue
        if _to_int(row.get("context_window"), 0) < min_context:
            continue
        caps = _normalize_capabilities(row.get("capabilities"))
        if required and not required.issubset(caps):
            continue
        enriched = dict(row)
        enriched["capabilities"] = sorted(caps)
        enriched["quality"] = _quality(enriched)
        enriched["tier"] = _tier(enriched)
        enriched["estimated_cost_usd"] = _price(enriched, req.est_tokens_in, req.est_tokens_out)
        candidates.append(enriched)
    candidates.sort(key=lambda row: _to_float(row.get("graeae_weight")), reverse=True)
    return candidates


def _apply_priority_ceiling(candidates: list[dict[str, Any]], priority: int) -> list[dict[str, Any]]:
    if priority >= 14:
        return [row for row in candidates if row["quality"] >= 0.85]
    if priority >= 10:
        return [row for row in candidates if row["tier"] in {"A", "B"} and row["quality"] >= 0.75]
    eligible = [row for row in candidates if row["tier"] in {"A", "B"}]
    return sorted(eligible, key=lambda row: (row["tier"] != "A", -_to_float(row.get("graeae_weight"))))


async def _plans_by_provider(backend: Any) -> dict[str, list[dict[str, Any]]]:
    sql = """
        SELECT provider, plan_name, auth_method, path_kind, monthly_usd, msg_cap,
               msg_window_seconds, token_cap, token_window_seconds,
               reset_anchor, overage_pricing_per_mtok_in,
               overage_pricing_per_mtok_out, effective_from, effective_until,
               parent_plan_id
        FROM subscription_plans
        WHERE effective_from <= TRUNC(SYSTIMESTAMP)
          AND (effective_until IS NULL OR effective_until >= TRUNC(SYSTIMESTAMP))
        ORDER BY provider, monthly_usd DESC, msg_cap DESC
        """
    try:
        rows = await _rows(backend, sql)
    except Exception as exc:
        msg = str(exc).lower()
        if "trunc" not in msg and "effective_from" not in msg and "path_kind" not in msg:
            raise
        rows = await _rows(
            backend,
            """
            SELECT provider, plan_name, auth_method, monthly_usd, msg_cap,
                   msg_window_seconds, token_cap, token_window_seconds,
                   reset_anchor, overage_pricing_per_mtok_in,
                   overage_pricing_per_mtok_out
            FROM subscription_plans
            ORDER BY provider, monthly_usd DESC, msg_cap DESC
            """,
        )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row.get("provider") or "").lower(), []).append(row)
    return out


def _best_plan(plans: dict[str, list[dict[str, Any]]], provider: str) -> dict[str, Any]:
    provider_plans = plans.get(provider.lower()) or []
    if provider_plans:
        return provider_plans[0]
    return {"provider": provider, "plan_name": "api", "auth_method": "api", "path_kind": "api"}


async def _usage_for_plan(backend: Any, plan: dict[str, Any]) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    plan_name = str(plan.get("plan_name") or "api")
    provider = str(plan.get("provider") or "")
    path_kind = str(plan.get("path_kind") or "api")
    window_id = compute_plan_window_id(
        provider,
        plan_name,
        now,
        reset_anchor=str(plan.get("reset_anchor") or "monthly"),
        window_seconds=_to_int(plan.get("msg_window_seconds") or plan.get("token_window_seconds"), 0) or None,
    )
    sql = """
        SELECT COALESCE(SUM(request_count), 0) AS requests_used,
               COALESCE(SUM(tokens_in + tokens_out + tokens_reasoning), 0) AS tokens_used
        FROM usage_ledger
        WHERE provider = :provider
          AND tier = :plan_name
          AND path_kind = :path_kind
          AND plan_window_id LIKE :window_pattern
        """
    params = {"provider": provider, "plan_name": plan_name, "path_kind": path_kind, "window_pattern": f"{window_id}%"}
    try:
        rows = await _rows(backend, sql, params)
    except Exception as exc:
        if "path_kind" not in str(exc).lower():
            raise
        rows = await _rows(
            backend,
            """
            SELECT COALESCE(SUM(request_count), 0) AS requests_used,
                   COALESCE(SUM(tokens_in + tokens_out + tokens_reasoning), 0) AS tokens_used
            FROM usage_ledger
            WHERE provider = :provider
              AND tier = :plan_name
              AND plan_window_id LIKE :window_pattern
            """,
            params,
        )
    row = rows[0] if rows else {}
    return _to_int(row.get("requests_used")), _to_int(row.get("tokens_used"))


def _utilization(plan: dict[str, Any], requests_used: int, tokens_used: int) -> float:
    msg_cap = _to_float(plan.get("msg_cap"))
    if msg_cap > 0:
        return round((requests_used / msg_cap) * 100.0, 2)
    token_cap = _to_float(plan.get("token_cap"))
    if token_cap > 0:
        return round((tokens_used / token_cap) * 100.0, 2)
    return 0.0


async def _session_burned(backend: Any, session_id: str | None) -> bool:
    if not session_id:
        return False
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = await _rows(
        backend,
        """
        SELECT COALESCE(SUM(request_count), 0) AS requests_used
        FROM usage_ledger
        WHERE session_id = :session_id AND ts >= :since_ts
        """,
        {"session_id": session_id, "since_ts": since},
    )
    return _to_int((rows[0] if rows else {}).get("requests_used")) > _SESSION_BURN_REQUESTS_PER_HOUR


def _downgrade_priority(priority: int) -> int:
    if priority >= 14:
        return 13
    if priority >= 10:
        return 9
    return priority


def _fallback_bucket(item: dict[str, Any]) -> int:
    auth = str(item.get("auth_method") or "api").lower()
    util = _to_float(item.get("sub_window_utilization_pct"))
    cost = _to_float(item.get("estimated_cost_usd"))
    if auth == "free":
        return 0
    if auth == "subscription" and util < 70:
        return 1
    if auth in {"api", "token"} and cost <= _LOW_PRIORITY_API_COST_CEILING_USD:
        return 2
    if auth in {"api", "token"}:
        return 3
    return 4


def _fallback_chain(candidates: list[dict[str, Any]], selected: dict[str, Any]) -> list[tuple]:
    alternates = [
        row for row in candidates if (row["provider"], row["model_id"]) != (selected["provider"], selected["model_id"])
    ]
    alternates.sort(
        key=lambda row: (
            _fallback_bucket(row),
            _to_float(row.get("estimated_cost_usd")),
            -_to_float(row.get("graeae_weight")),
        )
    )
    return [
        (
            row["provider"],
            row["model_id"],
            row.get("auth_method", "api"),
            row.get("path_kind", "api"),
            row.get("estimated_cost_usd", 0.0),
        )
        for row in alternates[:3]
    ]


async def _route_locked(req: KnemonRouteRequest, backend: Any) -> KnemonRouteDecision:
    candidates = await _registry_candidates(req, backend)
    if not candidates:
        raise NoModelAvailable("no model satisfies required capabilities, provider exclusions, and context window")

    effective_priority = (
        _downgrade_priority(req.priority) if await _session_burned(backend, req.caller_session_id) else req.priority
    )
    candidates = _apply_priority_ceiling(candidates, effective_priority)
    if not candidates:
        raise NoModelAvailable("no model satisfies priority tier and quality constraints")

    plans = await _plans_by_provider(backend)
    worker_pools = await _worker_pools_for_session(backend, req.caller_session_id)
    enriched: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    reasons: list[str] = []
    blocked_subscription_keys: set[tuple[str, str]] = set()
    for index, row in enumerate(candidates):
        plan = _best_plan(plans, row["provider"])
        auth_method = str(plan.get("auth_method") or "api").lower()
        path_kind = str(plan.get("path_kind") or auth_method).lower()
        requests_used, tokens_used = await _usage_for_plan(backend, plan) if auth_method == "subscription" else (0, 0)
        util = _utilization(plan, requests_used, tokens_used)
        item = {
            **row,
            "auth_method": auth_method,
            "path_kind": path_kind,
            "plan_name": plan.get("plan_name", "api"),
            "sub_window_utilization_pct": util,
        }
        enriched.append(item)
        if auth_method == "subscription":
            if not _worker_has_pool(worker_pools, plan):
                blocked_subscription_keys.add((row["provider"], row["model_id"]))
                required = sorted(_subscription_pool_aliases(plan))
                reasons.append(
                    "skipped subscription because caller workspace lacks pool "
                    f"{required[0] if required else item['plan_name']}"
                )
                continue
            if util < 70:
                selected = item
                reasons.append(f"selected subscription under 70% utilization ({util:.2f}%)")
                break
            if util <= 90 and effective_priority >= 12:
                selected = item
                reasons.append(f"selected subscription near cap for priority {effective_priority} ({util:.2f}%)")
                break
            no_other_candidate = index == len(candidates) - 1
            if util > 90 and util < 100 and effective_priority >= 14:
                selected = item
                reasons.append(f"selected over-90% subscription for G1 priority {effective_priority} ({util:.2f}%)")
                break
            if util > 90 and no_other_candidate:
                selected = item
                reasons.append(f"selected over-90% subscription because no alternate remained ({util:.2f}%)")
                break
            reasons.append(f"skipped subscription at {util:.2f}% utilization")
            continue
        if auth_method == "free" and effective_priority < 12:
            selected = item
            reasons.append("selected free plan for low-priority request")
            break
        if auth_method in {"api", "token"}:
            if effective_priority < 10 and item["estimated_cost_usd"] > _LOW_PRIORITY_API_COST_CEILING_USD:
                reasons.append(f"skipped API cost ${item['estimated_cost_usd']:.4f} for low-priority request")
                continue
            selected = item
            reasons.append(f"selected {auth_method} candidate")
            break

    if selected is None and enriched:
        selected = enriched[0]
        reasons.append("selected first remaining candidate because no lower-cost rule matched")
    if selected is None:
        raise NoModelAvailable("no model survived subscription, free, and API waterfall rules")

    seen = {(row["provider"], row["model_id"]) for row in enriched}
    fallback_candidates = list(enriched)
    for row in candidates:
        if (row["provider"], row["model_id"]) in seen:
            continue
        plan = _best_plan(plans, row["provider"])
        if str(plan.get("auth_method") or "api").lower() == "subscription" and not _worker_has_pool(worker_pools, plan):
            blocked_subscription_keys.add((row["provider"], row["model_id"]))
            continue
        fallback_candidates.append(
            {
                **row,
                "auth_method": str(plan.get("auth_method") or "api").lower(),
                "path_kind": str(plan.get("path_kind") or plan.get("auth_method") or "api").lower(),
                "plan_name": plan.get("plan_name", "api"),
                "sub_window_utilization_pct": 0.0,
            }
        )
    if blocked_subscription_keys:
        fallback_candidates = [
            row for row in fallback_candidates if (row["provider"], row["model_id"]) not in blocked_subscription_keys
        ]
    return KnemonRouteDecision(
        provider=selected["provider"],
        model_id=selected["model_id"],
        auth_method=selected["auth_method"],
        path_kind=selected["path_kind"],
        estimated_cost_usd=float(selected["estimated_cost_usd"]),
        sub_window_utilization_pct=float(selected["sub_window_utilization_pct"]),
        fallback_chain=_fallback_chain(fallback_candidates, selected),
        reasoning="; ".join(reasons),
    )


async def route(req: KnemonRouteRequest, backend: Any) -> KnemonRouteDecision:
    """Route a request to a provider/model using KNEMON waterfall policy."""
    if req.caller_session_id:
        lock = _SESSION_LOCKS.setdefault(req.caller_session_id, asyncio.Lock())
        async with lock:
            return await _route_locked(req, backend)
    return await _route_locked(req, backend)
