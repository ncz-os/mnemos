"""KNEMON subscription utilization analytics."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query

from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.persistence_helpers import backend_or_503
from mnemos.core.plan_windows import compute_plan_window_id

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


def _positive_cap(value: Any) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _cap_basis(plan: dict[str, Any], usage: dict[str, Any]) -> tuple[str | None, int, Any]:
    if _positive_cap(plan.get("msg_cap")):
        return "messages", int(usage.get("requests_used") or 0), plan.get("msg_cap")
    if _positive_cap(plan.get("token_cap")):
        return "tokens", int(usage.get("tokens") or 0), plan.get("token_cap")
    return None, int(usage.get("requests_used") or 0), None


def _usage_path_kinds(plan: dict[str, Any]) -> tuple[str, str | None]:
    path_kind = str(plan.get("path_kind") or "api").lower()
    auth_method = str(plan.get("auth_method") or "api").lower()
    legacy_path_kind = "api" if auth_method == "subscription" and path_kind != "api" else None
    return path_kind, legacy_path_kind


def _overage_cost(overage_msgs: int, plan: dict[str, Any]) -> float:
    if overage_msgs <= 0:
        return 0.0
    price_in = float(plan.get("overage_pricing_per_mtok_in") or 0)
    price_out = float(plan.get("overage_pricing_per_mtok_out") or 0)
    if price_in == 0 and price_out == 0:
        return 0.0
    return round(overage_msgs * (price_in + price_out) / 2.0, 6)


def _token_overage_cost(overage_tokens: int, plan: dict[str, Any]) -> float:
    if overage_tokens <= 0:
        return 0.0
    price_in = float(plan.get("overage_pricing_per_mtok_in") or 0)
    price_out = float(plan.get("overage_pricing_per_mtok_out") or 0)
    if price_in == 0 and price_out == 0:
        return 0.0
    return round((overage_tokens / 1_000_000.0) * (price_in + price_out) / 2.0, 6)


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
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _plan_effective_on(row: dict[str, Any], today: date) -> bool:
    effective_from = _to_date(row.get("effective_from"))
    effective_until = _to_date(row.get("effective_until"))
    if effective_from is not None and effective_from > today:
        return False
    if effective_until is not None and effective_until < today:
        return False
    return True


async def _plans() -> list[dict[str, Any]]:
    sql = """
        SELECT provider, plan_name, auth_method, path_kind, monthly_usd, msg_cap,
               msg_window_seconds, token_cap, token_window_seconds,
               reset_anchor, overage_pricing_per_mtok_in,
               overage_pricing_per_mtok_out, notes, effective_from,
               effective_until, parent_plan_id
        FROM subscription_plans
        ORDER BY provider, plan_name
        """
    try:
        rows = await _rows(sql)
        today = datetime.now(timezone.utc).date()
        return [row for row in rows if _plan_effective_on(row, today)]
    except Exception as exc:
        msg = str(exc).lower()
        if "effective_from" not in msg and "path_kind" not in msg and "parent_plan_id" not in msg:
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
    path_kind, legacy_path_kind = _usage_path_kinds(plan)
    params = {
        "provider": plan["provider"],
        "plan_name": plan["plan_name"],
        "path_kind": path_kind,
        "legacy_path_kind": legacy_path_kind,
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
          AND (path_kind = :path_kind OR (:legacy_path_kind IS NOT NULL AND path_kind = :legacy_path_kind))
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
        requests_used = int(usage.get("requests_used") or 0)
        tokens_used = int(usage.get("tokens") or 0)
        cap_unit, used, cap = _cap_basis(plan, usage)
        overage_msgs = 0
        overage_tokens = 0
        if cap_unit == "messages":
            overage_msgs = max(0, used - int(float(cap or 0)))
            overage_cost = _overage_cost(overage_msgs, plan)
        elif cap_unit == "tokens":
            overage_tokens = max(0, used - int(float(cap or 0)))
            overage_cost = _token_overage_cost(overage_tokens, plan)
        else:
            overage_cost = 0.0
        out.append(
            {
                "provider": plan["provider"],
                "plan_name": plan["plan_name"],
                "auth_method": plan.get("auth_method"),
                "path_kind": plan.get("path_kind") or "api",
                "window_id": window_id,
                "window_start": _jsonable(start),
                "window_end": _jsonable(end),
                "requests_used": requests_used,
                "tokens_used": tokens_used,
                "msg_cap": plan.get("msg_cap"),
                "token_cap": plan.get("token_cap"),
                "cap_unit": cap_unit,
                "notes": plan.get("notes"),
                "utilization_pct": _pct(used, cap),
                "projected_overage_msgs": overage_msgs,
                "projected_overage_tokens": overage_tokens,
                "projected_overage_cost_usd": overage_cost,
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
        current_requests = int(usage.get("requests_used") or 0)
        current_tokens = int(usage.get("tokens") or 0)
        projected_requests = int(round((current_requests / 7.0) * days_ahead))
        projected_tokens = int(round((current_tokens / 7.0) * days_ahead))
        cap_unit, projected_used, cap = _cap_basis(
            plan,
            {"requests_used": projected_requests, "tokens": projected_tokens},
        )
        overage_msgs = 0
        overage_tokens = 0
        if cap_unit == "messages":
            overage_msgs = max(0, projected_used - int(float(cap or 0)))
            overage_cost = _overage_cost(overage_msgs, plan)
        elif cap_unit == "tokens":
            overage_tokens = max(0, projected_used - int(float(cap or 0)))
            overage_cost = _token_overage_cost(overage_tokens, plan)
        else:
            overage_cost = 0.0
        out.append(
            {
                "provider": plan["provider"],
                "plan_name": plan["plan_name"],
                "path_kind": plan.get("path_kind") or "api",
                "days_ahead": days_ahead,
                "current_7d_requests": current_requests,
                "current_7d_tokens": current_tokens,
                "projected_requests": projected_requests,
                "projected_tokens": projected_tokens,
                "msg_cap": plan.get("msg_cap"),
                "token_cap": plan.get("token_cap"),
                "cap_unit": cap_unit,
                "notes": plan.get("notes"),
                "projected_overage_msgs": overage_msgs,
                "projected_overage_tokens": overage_tokens,
                "projected_overage_cost_usd": overage_cost,
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
    plans = {
        (str(plan.get("provider")), str(plan.get("plan_name"))): str(plan.get("auth_method") or "api").lower()
        for plan in await _plans()
    }
    sql = """
        SELECT ul.provider,
               ul.tier AS plan_name,
               ul.path_kind,
               ul.subscription_amortized,
               COUNT(*) AS row_count,
               COALESCE(SUM(ul.request_count), 0) AS requests,
               COALESCE(SUM(ul.est_cost_usd), 0) AS cost_usd
        FROM usage_ledger ul
        WHERE ul.ts >= :start_ts
        GROUP BY ul.provider, ul.tier, ul.path_kind, ul.subscription_amortized
        """
    try:
        rows = await _rows(sql, {"start_ts": start})
    except Exception as exc:
        msg = str(exc).lower()
        if "path_kind" not in msg:
            raise
        rows = await _rows(
            """
            SELECT ul.provider,
                   ul.tier AS plan_name,
                   'api' AS path_kind,
                   ul.subscription_amortized,
                   COUNT(*) AS row_count,
                   COALESCE(SUM(ul.request_count), 0) AS requests,
                   COALESCE(SUM(ul.est_cost_usd), 0) AS cost_usd
            FROM usage_ledger ul
            WHERE ul.ts >= :start_ts
            GROUP BY ul.provider, ul.tier, ul.subscription_amortized
            """,
            {"start_ts": start},
        )
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        auth_method = plans.get((str(row.get("provider")), str(row.get("plan_name"))), "api")
        if int(row.get("subscription_amortized") or 0) == 1:
            cost_bucket = "subscription_amortized"
        elif auth_method == "free" or float(row.get("cost_usd") or 0) == 0:
            cost_bucket = "free"
        else:
            cost_bucket = "api"
        path_kind = str(row.get("path_kind") or "api")
        item = grouped.setdefault(
            (cost_bucket, path_kind),
            {
                "cost_bucket": cost_bucket,
                "path_kind": path_kind,
                "row_count": 0,
                "requests": 0,
                "cost_usd": 0.0,
            },
        )
        item["row_count"] += int(row.get("row_count") or 0)
        item["requests"] += int(row.get("requests") or 0)
        item["cost_usd"] = round(float(item["cost_usd"]) + float(row.get("cost_usd") or 0), 6)
    return [grouped[key] for key in sorted(grouped)]
