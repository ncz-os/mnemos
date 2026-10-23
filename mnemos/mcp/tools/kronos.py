"""MCP KRONOS tool handlers.

F-06 slice 2 (§0b ABC-first): KRONOS is classified **Postgres-only**, not
routed through the persistence ABC. The domain functions take a pool and
their SQL is Postgres-specific (``date_trunc``, ``AT TIME ZONE``,
``NOW() - n * INTERVAL``, and a two-stage ``memory_recall_log`` ->
``memories`` fallback), so the pool is the pool's own owner here — not a
bypassed ABC. The direct ``_lc._pool`` read below is retained with that
justification; on non-Postgres backends the tools return a null-valued,
reason-bearing error instead of a fabricated 0-count success.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.auth_context import UserContext
from mnemos.core.config import get_settings
from mnemos.domain.kronos import detect_recall_anomalies, forecast_recall_load

from ._runtime import (
    _bounded_int,
    _mcp_is_root,
    _mcp_user_required,
    _rest_get,
    _safe_path_value,
    _tool,
)

MCP_KRONOS_HOURS_AHEAD_MAX = 24 * 30


async def tool_kronos_anomalies(
    namespace: str,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Return recall anomalies for a namespace without cross-namespace leaks."""
    _safe_path_value(namespace, label="namespace", max_length=128)
    if user is None:
        return await _rest_get("/admin/kronos/anomalies", params={"namespace": namespace})
    if not get_settings().kronos.enabled:
        return {"success": False, "error": "KRONOS disabled"}

    try:
        user = _mcp_user_required(user)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    if not _mcp_is_root(user) and user.namespace != namespace:
        return {"success": True, "namespace": namespace, "count": 0, "anomalies": []}

    # KRONOS is genuinely Postgres-only (see module docstring) — the pool is
    # its own owner, not a bypassed ABC.
    # F-06 slice 2 classification (c): permanently Postgres-only; retained.
    pool = _lc._pool
    if pool is not None:
        settings = get_settings().kronos
        anomalies = await detect_recall_anomalies(
            pool,
            namespace,
            lookback_hours=settings.default_lookback_hours,
            sensitivity=settings.default_sensitivity,
        )
        return {
            "success": True,
            "namespace": namespace,
            "count": len(anomalies),
            "anomalies": [asdict(item) for item in anomalies],
        }
    return _kronos_unsupported("kronos_anomalies")


async def tool_kronos_forecast(
    namespace: str,
    hours_ahead: int = 24,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Return a recall-load forecast for a namespace."""
    _safe_path_value(namespace, label="namespace", max_length=128)
    hours_ahead = _bounded_int(
        hours_ahead,
        label="hours_ahead",
        minimum=1,
        maximum=MCP_KRONOS_HOURS_AHEAD_MAX,
    )
    if user is None:
        return await _rest_get(
            "/admin/kronos/forecast",
            params={"namespace": namespace, "hours_ahead": hours_ahead},
        )
    if not get_settings().kronos.enabled:
        return {"success": False, "error": "KRONOS disabled"}

    try:
        user = _mcp_user_required(user)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    if not _mcp_is_root(user) and user.namespace != namespace:
        return {
            "success": True,
            "namespace": namespace,
            "forecast_window_hours": hours_ahead,
            "predicted_total_recalls": 0.0,
            "predicted_p95_per_hour": 0.0,
            "ci_lower": 0.0,
            "ci_upper": 0.0,
        }

    # KRONOS is genuinely Postgres-only (see module docstring) — the pool is
    # its own owner, not a bypassed ABC.
    # F-06 slice 2 classification (c): permanently Postgres-only; retained.
    pool = _lc._pool
    if pool is not None:
        result = await forecast_recall_load(pool, namespace, hours_ahead=hours_ahead)
        return {"success": True, **asdict(result)}
    return _kronos_unsupported("kronos_forecast")


def _kronos_unsupported(tool_name: str) -> dict[str, Any]:
    """Null-valued, reason-bearing error for non-Postgres deployments.

    The KRONOS domain SQL is Postgres-specific; reporting
    ``{"success": True, "count": 0, ...}`` here would fabricate an "all
    quiet" answer the backend cannot actually supply. A backend that cannot
    supply a value reports null with a reason, never a fabricated default.
    """
    from fastapi import HTTPException

    from mnemos.core.lifecycle import get_persistence_backend

    try:
        backend = get_persistence_backend()
    except HTTPException:
        backend = None
    backend_name = type(backend).__name__ if backend is not None else "no backend"
    return {
        "success": False,
        "error": (
            f"{tool_name} requires the Postgres backend; "
            f"this deployment is configured with {backend_name}. "
            f"KRONOS recall analysis is Postgres-only."
        ),
    }


TOOLS: dict[str, dict[str, Any]] = {
    "kronos_anomalies": _tool(
        "Detect recall-pattern anomalies for a namespace.",
        {
            "namespace": {"type": "string", "description": "Namespace to inspect"},
        },
        ["namespace"],
        tool_kronos_anomalies,
    ),
    "kronos_forecast": _tool(
        "Forecast recall load for a namespace.",
        {
            "namespace": {"type": "string", "description": "Namespace to forecast"},
            "hours_ahead": {
                "type": "integer",
                "default": 24,
                "minimum": 1,
                "maximum": MCP_KRONOS_HOURS_AHEAD_MAX,
            },
        },
        ["namespace"],
        tool_kronos_forecast,
    ),
}
