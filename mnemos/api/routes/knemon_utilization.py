"""KNEMON subscription utilization analytics."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query

from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.persistence_helpers import backend_or_503
from mnemos.api.routes.ledger import compute_plan_window_id

router = APIRouter(prefix="/v1/knemon", tags=["knemon"])


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs) if callable(value) else value
    return await result if inspect.isawaitable(result) else result


def _conn_from_tx(tx: Any) -> Any:
    return getattr(tx, "conn", tx)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


async def _rows(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    backend = backend_or_503()
    async with backend.transactional() as tx:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(cursor.execute, sql, params or {})
            fetched = await _call(cursor.fetchall)
            names = [col[0].lower() for col in cursor.description]
            return [{name: _jsonable(value) for name, value in zip(names, row)} for row in fetched or []]
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                await _call(close)


def _period_start(period: str, now: datetime) -> datetime:
    if period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        start = now - timedelta(days=now.isoweekday() - 1)
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _window_bounds(plan: dict[str, Any], now: datetime) -> tuple[datetime, datetime]:
    anchor = str(plan.get("reset_anchor") or "monthly").lower()
    seconds = int(plan.get("msg_window_seconds") or plan.get("token_window_seconds") or 0)
    if anchor in {"daily", "day"}:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if anchor in {"weekly", "week"}:
        start = _period_start("weekly", now)
        return start, start + timedelta(days=7)
    if anchor in {"rolling", "window"} and seconds > 0:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        index = int((now - day_start).total_seconds()) // seconds
        start = day_start + timedelta(seconds=index * seconds)
        return start, start + timedelta(seconds=seconds)
    start = _period_start("monthly", now)
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


def _pct(used: int, cap: Any) -> float | None:
    if cap in (None, 0):
        return None
    return round((used / float(cap)) * 100.0, 2)


def _overage_cost(overage_msgs: int, plan: dict[str, Any]) -> float:
    if overage_msgs <= 0:
        return 0.0
    price_in = float(plan.get("overage_pricing_per_mtok_in") or 0)
    price_out = float(plan.get("overage_pricing_per_mtok_out") or 0)
    if price_in == 0 and price_out == 0:
        return 0.0
    return round(overage_msgs * (price_in + price_out) / 2.0, 6)


async def _plans() -> list[dict[str, Any]]:
    sql = """
        SELECT provider, plan_name, auth_method, path_kind, monthly_usd, msg_cap,
               msg_window_seconds, token_cap, token_window_seconds,
               reset_anchor, overage_pricing_per_mtok_in,
               overage_pricing_per_mtok_out, notes, effective_from,
               effective_until, parent_plan_id
        FROM subscription_plans
        WHERE effective_from <= TRUNC(SYSTIMESTAMP)
          AND (effective_until IS NULL OR effective_until >= TRUNC(SYSTIMESTAMP))
        ORDER BY provider, plan_name
        """
    try:
        return await _rows(sql)
    except Exception as exc:
        msg = str(exc).lower()
        if "trunc" not in msg and "effective_from" not in msg and "path_kind" not in msg:
            raise
        return await _rows(
            """
            SELECT provider, plan_name, auth_method, monthly_usd, msg_cap,
                   msg_window_seconds, token_cap, token_window_seconds,
                   reset_anchor, overage_pricing_per_mtok_in,
                   overage_pricing_per_mtok_out, notes
            FROM subscription_plans
            ORDER BY provider, plan_name
            """
        )


async def _usage_for_plan(
    plan: dict[str, Any], start: datetime, end: datetime, window_id: str | None = None
) -> dict[str, Any]:
    params = {
        "provider": plan["provider"],
        "plan_name": plan["plan_name"],
        "path_kind": plan.get("path_kind") or "api",
        "start_ts": start,
        "end_ts": end,
        "window_id": window_id,
    }
    sql = """
        SELECT COUNT(*) AS row_count,
               COALESCE(SUM(request_count), 0) AS requests_used,
               COALESCE(SUM(est_cost_usd), 0) AS cost_usd,
               COALESCE(SUM(tokens_in + tokens_out + tokens_reasoning), 0) AS tokens
        FROM usage_ledger
        WHERE provider = :provider
          AND tier = :plan_name
          AND path_kind = :path_kind
          AND ((:window_id IS NOT NULL AND plan_window_id = :window_id)
               OR (ts >= :start_ts AND ts < :end_ts))
        """
    try:
        rows = await _rows(sql, params)
    except Exception as exc:
        if "path_kind" not in str(exc).lower():
            raise
        rows = await _rows(
            """
            SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(request_count), 0) AS requests_used,
                   COALESCE(SUM(est_cost_usd), 0) AS cost_usd,
                   COALESCE(SUM(tokens_in + tokens_out + tokens_reasoning), 0) AS tokens
            FROM usage_ledger
            WHERE provider = :provider
              AND tier = :plan_name
              AND ((:window_id IS NOT NULL AND plan_window_id = :window_id)
                   OR (ts >= :start_ts AND ts < :end_ts))
            """,
            params,
        )
    return rows[0] if rows else {"row_count": 0, "requests_used": 0, "cost_usd": 0, "tokens": 0}


@router.get("/utilization")
async def utilization(
    window: str = Query("current", pattern="^current$"),
    _: UserContext = Depends(require_root),
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out = []
    for plan in await _plans():
        start, end = _window_bounds(plan, now)
        window_id = compute_plan_window_id(
            str(plan["provider"]),
            str(plan["plan_name"]),
            now,
            reset_anchor=str(plan.get("reset_anchor") or "monthly"),
            window_seconds=int(plan.get("msg_window_seconds") or plan.get("token_window_seconds") or 0) or None,
        )
        usage = await _usage_for_plan(plan, start, end, window_id)
        used = int(usage.get("requests_used") or 0)
        cap = plan.get("msg_cap")
        overage = max(0, used - int(cap or 0)) if cap else 0
        out.append(
            {
                "provider": plan["provider"],
                "plan_name": plan["plan_name"],
                "auth_method": plan.get("auth_method"),
                "path_kind": plan.get("path_kind") or "api",
                "window_id": window_id,
                "window_start": _jsonable(start),
                "window_end": _jsonable(end),
                "requests_used": used,
                "msg_cap": cap,
                "utilization_pct": _pct(used, cap),
                "projected_overage_msgs": overage,
                "projected_overage_cost_usd": _overage_cost(overage, plan),
            }
        )
    return out


@router.get("/overage_projection")
async def overage_projection(
    days_ahead: int = Query(7, ge=1, le=365),
    _: UserContext = Depends(require_root),
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    out = []
    for plan in await _plans():
        usage = await _usage_for_plan(plan, since, now, None)
        daily_rate = int(usage.get("requests_used") or 0) / 7.0
        projected = int(round(daily_rate * days_ahead))
        cap = int(plan.get("msg_cap") or 0)
        overage = max(0, projected - cap) if cap else 0
        out.append(
            {
                "provider": plan["provider"],
                "plan_name": plan["plan_name"],
                "path_kind": plan.get("path_kind") or "api",
                "days_ahead": days_ahead,
                "current_7d_requests": int(usage.get("requests_used") or 0),
                "projected_requests": projected,
                "msg_cap": plan.get("msg_cap"),
                "projected_overage_msgs": overage,
                "projected_overage_cost_usd": _overage_cost(overage, plan),
            }
        )
    return out


@router.get("/by_session")
async def by_session(
    days: int = Query(7, ge=1, le=365),
    _: UserContext = Depends(require_root),
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    sql = """
        SELECT session_id,
               provider,
               tier AS plan_name,
               path_kind,
               COUNT(*) AS row_count,
               COALESCE(SUM(request_count), 0) AS requests,
               COALESCE(SUM(tokens_in + tokens_out + tokens_reasoning), 0) AS tokens,
               COALESCE(SUM(est_cost_usd), 0) AS cost_usd
        FROM usage_ledger
        WHERE ts >= :since_ts
        GROUP BY session_id, provider, tier, path_kind
        ORDER BY requests DESC, cost_usd DESC
        """
    try:
        return await _rows(sql, {"since_ts": since})
    except Exception as exc:
        if "path_kind" not in str(exc).lower():
            raise
        return await _rows(
            """
            SELECT session_id,
                   provider,
                   tier AS plan_name,
                   'api' AS path_kind,
                   COUNT(*) AS row_count,
                   COALESCE(SUM(request_count), 0) AS requests,
                   COALESCE(SUM(tokens_in + tokens_out + tokens_reasoning), 0) AS tokens,
                   COALESCE(SUM(est_cost_usd), 0) AS cost_usd
            FROM usage_ledger
            WHERE ts >= :since_ts
            GROUP BY session_id, provider, tier
            ORDER BY requests DESC, cost_usd DESC
            """,
            {"since_ts": since},
        )


@router.get("/cost_split")
async def cost_split(
    period: str = Query("monthly", pattern="^(monthly|weekly|daily)$"),
    _: UserContext = Depends(require_root),
) -> list[dict[str, Any]]:
    start = _period_start(period, datetime.now(timezone.utc))
    sql = """
        SELECT CASE
                 WHEN ul.subscription_amortized = 1 THEN 'subscription_amortized'
                 WHEN COALESCE(sp.auth_method, 'api') = 'free' THEN 'free'
                 WHEN COALESCE(ul.est_cost_usd, 0) = 0 THEN 'free'
                 ELSE 'api'
               END AS cost_bucket,
               ul.path_kind,
               COUNT(*) AS row_count,
               COALESCE(SUM(ul.request_count), 0) AS requests,
               COALESCE(SUM(ul.est_cost_usd), 0) AS cost_usd
        FROM usage_ledger ul
        LEFT JOIN subscription_plans sp
          ON sp.provider = ul.provider AND sp.plan_name = ul.tier
         AND sp.effective_from <= TRUNC(SYSTIMESTAMP)
         AND (sp.effective_until IS NULL OR sp.effective_until >= TRUNC(SYSTIMESTAMP))
        WHERE ul.ts >= :start_ts
        GROUP BY CASE
                   WHEN ul.subscription_amortized = 1 THEN 'subscription_amortized'
                   WHEN COALESCE(sp.auth_method, 'api') = 'free' THEN 'free'
                   WHEN COALESCE(ul.est_cost_usd, 0) = 0 THEN 'free'
                   ELSE 'api'
                 END,
                 ul.path_kind
        ORDER BY cost_bucket, ul.path_kind
        """
    try:
        rows = await _rows(sql, {"start_ts": start})
    except Exception as exc:
        msg = str(exc).lower()
        if "path_kind" not in msg and "trunc" not in msg and "effective_from" not in msg:
            raise
        rows = await _rows(
            """
            SELECT CASE
                     WHEN ul.subscription_amortized = 1 THEN 'subscription_amortized'
                     WHEN COALESCE(sp.auth_method, 'api') = 'free' THEN 'free'
                     WHEN COALESCE(ul.est_cost_usd, 0) = 0 THEN 'free'
                     ELSE 'api'
                   END AS cost_bucket,
                   'api' AS path_kind,
                   COUNT(*) AS row_count,
                   COALESCE(SUM(ul.request_count), 0) AS requests,
                   COALESCE(SUM(ul.est_cost_usd), 0) AS cost_usd
            FROM usage_ledger ul
            LEFT JOIN subscription_plans sp
              ON sp.provider = ul.provider AND sp.plan_name = ul.tier
            WHERE ul.ts >= :start_ts
            GROUP BY CASE
                       WHEN ul.subscription_amortized = 1 THEN 'subscription_amortized'
                       WHEN COALESCE(sp.auth_method, 'api') = 'free' THEN 'free'
                       WHEN COALESCE(ul.est_cost_usd, 0) = 0 THEN 'free'
                       ELSE 'api'
                     END
            ORDER BY cost_bucket
            """,
            {"start_ts": start},
        )
    return rows
