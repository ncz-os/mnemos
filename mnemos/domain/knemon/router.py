"""KNEMON hybrid model router."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from mnemos.core.config import get_settings
from mnemos.core.plan_windows import compute_plan_window_id

# Canonical provider identity — collapses historical/duplicate labels so a
# single policy entry covers all of them (e.g. "claude" -> "anthropic").
_PROVIDER_ALIASES = {"claude": "anthropic"}


def _canon_provider(name: str) -> str:
    canon = (name or "").strip().lower()
    return _PROVIDER_ALIASES.get(canon, canon)


def _csv_providers(raw: str) -> list[str]:
    return [_canon_provider(part) for part in (raw or "").split(",") if part.strip()]


class NoModelAvailable(RuntimeError):
    """Raised when the registry has no model satisfying hard constraints."""


@dataclass
class KnemonRouteRequest:
    task_kind: str
    priority: int
    est_tokens_in: int = 0
    est_tokens_out: int = 0
    caller_session_id: Optional[str] = None
    caller_subsystem: str = "api"
    exclude_providers: list[str] = field(default_factory=list)
    require_capability: list[str] = field(default_factory=list)
    est_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        if self.est_tokens is not None and self.est_tokens_in == 0 and self.est_tokens_out == 0:
            self.est_tokens_in = max(0, int(self.est_tokens))


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
    # Model-affinity dispatch (GRAEAE de8f4b2b layering / zeroclaw triage bridge):
    # for zeroclaw callers the decision pins the job to a specific provider/model
    # worker via a model:<provider_model> capability. Empty for non-dispatch
    # callers (api/internal) so existing consumers are unaffected.
    dispatch_kind: str = ""
    dispatch_required_capabilities: list[str] = field(default_factory=list)


_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_NAMED_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")

# Caller-subsystem -> dispatchable provider allowlist (GRAEAE consult de8f4b2b 2026-05-28:
# Option A centralized allowlist, config-driven/version-controlled, NoModelAvailable -> caller blocks).
# A dispatch worker can only EXECUTE providers that have a gateway agent alias; KNEMON must never
# route it to a provider it cannot run (e.g. anthropic/claude, which is also policy-forbidden in the
# zeroclaw stack per CLAUDE.md directive 5). openai is intentionally absent: it is reserved for the
# explicit codex scarce path (kind=codex/review:/doctor:codex-fix), not the general dispatch route.
CALLER_DISPATCHABLE_PROVIDERS: dict[str, frozenset[str]] = {
    "zeroclaw": frozenset({"groq", "xai", "deepseek", "deepseek-direct", "nvidia", "together", "gemini"}),
}

# Zeroclaw-family provider aliases — providers a zeroclaw worker can execute.
_ZEROCLAW_PROVIDER_ALIASES = {"zeroclaw", "openclaw", "local", "local-llamacpp", "local-vllm"}


def _model_capability(provider: str, model_id: str) -> str:
    """Stable model:<provider_model> capability used to pin a job to one worker."""
    safe = _normalize_pool(f"{provider}_{model_id}")
    return f"model:{safe}" if safe else "model:unknown"


def _dispatch_kind(provider: str, caller_subsystem: str = "") -> str:
    """Worker kind that should execute this job (zeroclaw for dispatch callers)."""
    if (caller_subsystem or "").strip().lower() == "zeroclaw":
        return "zeroclaw"
    provider_key = _canon_provider(provider)
    if provider_key in _ZEROCLAW_PROVIDER_ALIASES:
        return "zeroclaw"
    return provider_key or "provider-worker"


def _dispatch_required_capabilities(
    selected: dict[str, Any], required_caps: list[str], caller_subsystem: str = ""
) -> list[str]:
    """Append the model-affinity capability for zeroclaw dispatch callers."""
    out = list(required_caps)
    model_cap = _model_capability(str(selected.get("provider") or ""), str(selected.get("model_id") or ""))
    if _dispatch_kind(str(selected.get("provider") or ""), caller_subsystem) == "zeroclaw" and model_cap not in out:
        out.append(model_cap)
    return out


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


def _asyncpg_sql(sql: str, params: dict[str, Any]) -> tuple[str, list[Any]]:
    values: list[Any] = []
    positions: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            return match.group(0)
        if name not in positions:
            positions[name] = len(values) + 1
            values.append(params[name])
        return f"${positions[name]}"

    return _NAMED_PARAM_RE.sub(replace, sql), values


def _dict_rows(rows: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append({str(key).lower(): value for key, value in row.items()})
            continue
        try:
            items = dict(row).items()
        except (TypeError, ValueError):
            continue
        out.append({str(key).lower(): value for key, value in items})
    return out


async def _rows(backend: Any, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    async with backend.transactional() as tx:
        conn = _conn_from_tx(tx)
        fetch = getattr(conn, "fetch", None)
        if callable(fetch):
            pg_sql, pg_params = _asyncpg_sql(sql, params or {})
            return _dict_rows(await _call(fetch, pg_sql, *pg_params))

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


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _plan_is_effective(row: dict[str, Any], today: date | None = None) -> bool:
    if "effective_from" not in row and "effective_until" not in row:
        return True
    today = today or datetime.now(timezone.utc).date()
    effective_from = _to_date(row.get("effective_from"))
    effective_until = _to_date(row.get("effective_until"))
    if effective_from is not None and effective_from > today:
        return False
    if effective_until is not None and effective_until < today:
        return False
    return True


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
        if "chatgpt" in plan_name:
            aliases.add("chatgpt_subscription")
        if "codex" in plan_name:
            aliases.add("codex_subscription")
    return aliases


def _plan_family_alias(plan: dict[str, Any]) -> str | None:
    aliases = _subscription_pool_aliases(plan)
    for family in ("chatgpt_subscription", "codex_subscription", "claude_subscription"):
        if family in aliases:
            return family
    return None


def _candidate_plan_family(candidate: dict[str, Any] | None) -> str | None:
    if not candidate:
        return None
    provider = _normalize_pool(candidate.get("provider"))
    if provider == "anthropic":
        return "claude_subscription"
    if provider != "openai":
        return None
    raw = " ".join(
        str(part or "")
        for part in (
            candidate.get("model_id"),
            candidate.get("display_name"),
            " ".join(candidate.get("capabilities") or []),
        )
    ).lower()
    if "codex" in raw:
        return "codex_subscription"
    return "chatgpt_subscription"


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
    providers_cfg = get_settings().providers
    # Deployment-default excludes (canonicalised) merged with per-request set.
    excluded = set(_csv_providers(providers_cfg.knemon_exclude_providers))
    excluded |= {_canon_provider(p) for p in req.exclude_providers if p.strip()}
    allowed = CALLER_DISPATCHABLE_PROVIDERS.get((req.caller_subsystem or "").strip().lower())
    required = {cap.strip() for cap in req.require_capability if cap.strip()}
    min_context = int(max(0, req.est_tokens_in) * 1.2)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        provider = str(row.get("provider") or "").strip()
        if not provider or _canon_provider(provider) in excluded:
            continue
        # Caller-eligibility allowlist: dispatch workers (e.g. zeroclaw) can only execute providers
        # backed by a gateway alias. Callers absent from the map (api, internal) are unrestricted.
        if allowed is not None and provider.lower() not in allowed:
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
    preference = _csv_providers(providers_cfg.knemon_provider_preference)
    if preference:
        rank = {name: idx for idx, name in enumerate(preference)}
        candidates.sort(
            key=lambda row: (
                rank.get(_canon_provider(str(row.get("provider") or "")), len(preference)),
                -_to_float(row.get("graeae_weight")),
            )
        )
    else:
        candidates.sort(key=lambda row: _to_float(row.get("graeae_weight")), reverse=True)
    return candidates


def _apply_priority_ceiling(
    candidates: list[dict[str, Any]],
    priority: int,
    *,
    requested_priority: int | None = None,
) -> list[dict[str, Any]]:
    policy = get_settings().knemon
    quality_priority = max(priority, requested_priority if requested_priority is not None else priority)
    if quality_priority >= 14:
        return [row for row in candidates if row["quality"] >= policy.g1_quality_floor]
    if priority >= 10:
        return [row for row in candidates if row["tier"] in {"A", "B"} and row["quality"] >= policy.g2_quality_floor]
    eligible = [row for row in candidates if row["tier"] in {"A", "B"}]
    return sorted(eligible, key=lambda row: (row["tier"] != "A", -_to_float(row.get("graeae_weight"))))


async def _plans_by_provider(backend: Any, as_of: date | None = None) -> dict[str, list[dict[str, Any]]]:
    sql = """
        SELECT provider, plan_name, auth_method, path_kind, monthly_usd, msg_cap,
               msg_window_seconds, token_cap, token_window_seconds,
               reset_anchor, overage_pricing_per_mtok_in,
               overage_pricing_per_mtok_out, effective_from, effective_until,
               parent_plan_id
        FROM subscription_plans
        ORDER BY provider, COALESCE(monthly_usd, 0) DESC, COALESCE(msg_cap, 0) DESC, plan_name
        """
    try:
        rows = await _rows(backend, sql)
        today = as_of or datetime.now(timezone.utc).date()
        rows = [row for row in rows if _plan_is_effective(row, today)]
    except Exception as exc:
        msg = str(exc).lower()
        if (
            "coalesce" not in msg
            and "effective_from" not in msg
            and "path_kind" not in msg
            and "parent_plan_id" not in msg
        ):
            raise
        rows = await _rows(
            backend,
            """
            SELECT provider, plan_name, auth_method, monthly_usd, msg_cap,
                   msg_window_seconds, token_cap, token_window_seconds,
                   reset_anchor, overage_pricing_per_mtok_in,
                   overage_pricing_per_mtok_out
            FROM subscription_plans
            ORDER BY provider, monthly_usd DESC, msg_cap DESC, plan_name
            """,
        )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not _plan_is_effective(row, as_of):
            continue
        out.setdefault(str(row.get("provider") or "").lower(), []).append(row)
    return out


def _best_plan(
    plans: dict[str, list[dict[str, Any]]],
    provider: str,
    worker_pools: set[str] | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_plans = plans.get(provider.lower()) or []
    if worker_pools is not None:
        for plan in provider_plans:
            if _worker_has_pool(worker_pools, plan):
                return plan
    if provider_plans:
        if worker_pools is None:
            family = _candidate_plan_family(candidate)
            if family is not None:
                for plan in provider_plans:
                    if family in _subscription_pool_aliases(plan):
                        return plan
        return provider_plans[0]
    return {"provider": provider, "plan_name": "api", "auth_method": "api", "path_kind": "api"}


async def _usage_for_plan(backend: Any, plan: dict[str, Any]) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    plan_name = str(plan.get("plan_name") or "api")
    provider = str(plan.get("provider") or "")
    path_kind, legacy_path_kind = _usage_path_kinds(plan)
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
          AND (path_kind = :path_kind OR (:legacy_path_kind IS NOT NULL AND path_kind = :legacy_path_kind))
          AND plan_window_id LIKE :window_pattern
        """
    params = {
        "provider": provider,
        "plan_name": plan_name,
        "path_kind": path_kind,
        "legacy_path_kind": legacy_path_kind,
        "window_pattern": f"{window_id}%",
    }
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


def _usage_path_kinds(plan: dict[str, Any]) -> tuple[str, str | None]:
    path_kind = str(plan.get("path_kind") or "api").lower()
    auth_method = str(plan.get("auth_method") or "api").lower()
    legacy_path_kind = "api" if auth_method == "subscription" and path_kind != "api" else None
    return path_kind, legacy_path_kind


async def _session_burned(backend: Any, session_id: str | None) -> bool:
    if not session_id:
        return False
    policy = get_settings().knemon
    threshold = policy.session_burn_requests_per_hour
    if threshold <= 0:
        return False
    since = datetime.now(timezone.utc) - timedelta(seconds=policy.session_burn_window_seconds)
    rows = await _rows(
        backend,
        """
        SELECT COALESCE(SUM(request_count), 0) AS requests_used
        FROM usage_ledger
        WHERE session_id = :session_id AND ts >= :since_ts
        """,
        {"session_id": session_id, "since_ts": since},
    )
    return _to_int((rows[0] if rows else {}).get("requests_used")) >= threshold


def _downgrade_priority(priority: int) -> int:
    if priority >= 14:
        return 13
    if priority >= 10:
        return 9
    return priority


def _fallback_bucket(item: dict[str, Any]) -> int:
    policy = get_settings().knemon
    auth = str(item.get("auth_method") or "api").lower()
    util = _to_float(item.get("sub_window_utilization_pct"))
    cost = _to_float(item.get("estimated_cost_usd"))
    if auth == "free":
        return 0
    if auth == "subscription" and util < policy.subscription_preferred_utilization_pct:
        return 1
    if auth in {"api", "token"} and cost < policy.low_priority_api_cost_ceiling_usd:
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

    session_burned = await _session_burned(backend, req.caller_session_id)
    effective_priority = _downgrade_priority(req.priority) if session_burned else req.priority
    candidates = _apply_priority_ceiling(candidates, effective_priority, requested_priority=req.priority)
    if not candidates:
        raise NoModelAvailable("no model satisfies priority tier and quality constraints")

    plans = await _plans_by_provider(backend)
    worker_pools = await _worker_pools_for_session(backend, req.caller_session_id)
    policy = get_settings().knemon
    enriched: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    reasons: list[str] = []
    blocked_subscription_keys: set[tuple[str, str]] = set()
    for index, row in enumerate(candidates):
        plan = _best_plan(plans, row["provider"], worker_pools, row)
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
            if util < policy.subscription_preferred_utilization_pct:
                selected = item
                reasons.append(
                    "selected subscription under "
                    f"{policy.subscription_preferred_utilization_pct:.0f}% utilization ({util:.2f}%)"
                )
                break
            if not session_burned and util <= policy.subscription_near_cap_pct and effective_priority >= 12:
                selected = item
                reasons.append(f"selected subscription near cap for priority {effective_priority} ({util:.2f}%)")
                break
            no_other_candidate = index == len(candidates) - 1
            if util > policy.subscription_near_cap_pct and util < 100 and effective_priority >= 14:
                selected = item
                reasons.append(
                    "selected over-"
                    f"{policy.subscription_near_cap_pct:.0f}% subscription for G1 priority "
                    f"{effective_priority} ({util:.2f}%)"
                )
                break
            if util > policy.subscription_near_cap_pct and no_other_candidate:
                selected = item
                reasons.append(
                    "selected over-"
                    f"{policy.subscription_near_cap_pct:.0f}% subscription because no alternate remained "
                    f"({util:.2f}%)"
                )
                break
            reasons.append(f"skipped subscription at {util:.2f}% utilization")
            continue
        if auth_method == "free" and effective_priority < 12:
            selected = item
            reasons.append("selected free plan for low-priority request")
            break
        if auth_method in {"api", "token"}:
            if effective_priority < 10 and item["estimated_cost_usd"] >= policy.low_priority_api_cost_ceiling_usd:
                reasons.append(f"skipped API cost ${item['estimated_cost_usd']:.4f} for low-priority request")
                continue
            selected = item
            reasons.append(f"selected {auth_method} candidate")
            break

    if selected is None:
        raise NoModelAvailable("no model survived subscription, free, and API waterfall rules")

    seen = {(row["provider"], row["model_id"]) for row in enriched}
    fallback_candidates = list(enriched)
    for row in candidates:
        if (row["provider"], row["model_id"]) in seen:
            continue
        plan = _best_plan(plans, row["provider"], worker_pools, row)
        auth_method = str(plan.get("auth_method") or "api").lower()
        path_kind = str(plan.get("path_kind") or auth_method).lower()
        util = 0.0
        if auth_method == "subscription" and not _worker_has_pool(worker_pools, plan):
            blocked_subscription_keys.add((row["provider"], row["model_id"]))
            continue
        if auth_method == "subscription":
            requests_used, tokens_used = await _usage_for_plan(backend, plan)
            util = _utilization(plan, requests_used, tokens_used)
        fallback_candidates.append(
            {
                **row,
                "auth_method": auth_method,
                "path_kind": path_kind,
                "plan_name": plan.get("plan_name", "api"),
                "sub_window_utilization_pct": util,
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
        dispatch_kind=_dispatch_kind(str(selected["provider"]), req.caller_subsystem),
        dispatch_required_capabilities=_dispatch_required_capabilities(
            selected,
            [c.strip() for c in req.require_capability if c.strip()],
            req.caller_subsystem,
        ),
    )


async def route(req: KnemonRouteRequest, backend: Any) -> KnemonRouteDecision:
    """Route a request to a provider/model using KNEMON waterfall policy."""
    if req.caller_session_id:
        lock = _SESSION_LOCKS.setdefault(req.caller_session_id, asyncio.Lock())
        async with lock:
            return await _route_locked(req, backend)
    return await _route_locked(req, backend)
