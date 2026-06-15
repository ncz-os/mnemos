"""Best-effort PANTHEON routing-audit writes."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import mnemos.core.lifecycle as _lc
from mnemos.core.config import get_settings
from mnemos.core.numeric import safe_float
from mnemos.domain.pantheon.router import RouteDecision
from mnemos.nats import publisher as nats_publisher

logger = logging.getLogger(__name__)

PANTHEON_ROUTING_SUBJECT = "mnemos.pantheon.routing"
PANTHEON_ROUTING_SCHEMA_VERSION = "1"
_AUDIT_RECORD_FIELDS = (
    "request_id",
    "tenant_user_id",
    "alias_or_model",
    "resolved_to",
    "outcome",
    "latency_ms",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "error_class",
    "payload_json",
)


@dataclass(frozen=True)
class _RoutingLogItem:
    payload: dict[str, Any]
    metadata: dict[str, Any]


_routing_log_queue: asyncio.Queue[_RoutingLogItem] | None = None
_routing_log_drainers = 0


def _usage_value(response: dict[str, Any] | None, key: str) -> int | None:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict) or usage.get(key) is None:
        return None
    try:
        return int(usage[key])
    except (TypeError, ValueError):
        return None


def _model_cost(decision: RouteDecision, key: str) -> float | None:
    model = decision.model or {}
    raw = model.get(key)
    if raw is None:
        raw = model.get("cost_per_mtok")
    if raw is None:
        return None
    return safe_float(raw)


def _response_cost_usd(decision: RouteDecision, response: dict[str, Any] | None) -> float | None:
    if isinstance(response, dict):
        for key in ("cost_usd", "cost"):
            raw = response.get(key)
            if raw is not None:
                return safe_float(raw)
    tokens_in = _usage_value(response, "prompt_tokens")
    tokens_out = _usage_value(response, "completion_tokens")
    if tokens_in is None and tokens_out is None:
        return None
    input_cost = _model_cost(decision, "input_cost_per_mtok")
    output_cost = _model_cost(decision, "output_cost_per_mtok")
    cost_usd = 0.0
    if tokens_in is not None and input_cost is not None:
        cost_usd += (tokens_in / 1_000_000.0) * input_cost
    if tokens_out is not None and output_cost is not None:
        cost_usd += (tokens_out / 1_000_000.0) * output_cost
    return cost_usd


def _usage_tier(decision: RouteDecision) -> str | None:
    model = decision.model or {}
    raw = model.get("usage_tier")
    return str(raw) if raw is not None else None


def routing_payload(
    *,
    request_id: str,
    tenant_user_id: str,
    session_id: str,
    decision: RouteDecision,
    outcome: str,
    latency_ms: float,
    response: dict[str, Any] | None = None,
    error_class: str | None = None,
    namespace: str | None = None,
    forwarded_user: str | None = None,
    resolved_wire_model: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    wire_model = resolved_wire_model or decision.model_id or decision.alias
    payload = {
        "request_id": request_id,
        "tenant_user_id": tenant_user_id,
        "alias_or_model": decision.alias,
        "resolved_to": wire_model,
        "outcome": outcome,
        "latency_ms": latency_ms,
        "tokens_in": _usage_value(response, "prompt_tokens"),
        "tokens_out": _usage_value(response, "completion_tokens"),
        "cost_usd": _response_cost_usd(decision, response),
        "error_class": error_class,
    }
    metadata = {
        "schema_version": PANTHEON_ROUTING_SCHEMA_VERSION,
        "pantheon_version": "0.2",
        "session_id": session_id,
        "namespace": namespace,
        "forwarded_user": forwarded_user,
        "forwarded_identity": {
            "tenant_user_id": tenant_user_id,
            "namespace": namespace,
            "session_id": session_id,
            "request_id": request_id,
            "upstream_user": forwarded_user,
        },
        "usage_tier": _usage_tier(decision),
        **payload,
    }
    return payload, metadata


def routing_event_payload(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    event = dict(payload)
    event["metadata"] = dict(metadata)
    return event


def _routing_msg_id(payload: dict[str, Any]) -> str | None:
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        return None
    return f"pantheon.routing.{request_id}"


async def publish_routing_event(payload: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Publish one routing decision to NATS, swallowing all failures."""
    if not get_settings().nats.publish_pantheon_routing:
        return
    try:
        await nats_publisher.publish_event(
            PANTHEON_ROUTING_SUBJECT,
            routing_event_payload(payload, metadata),
            msg_id=_routing_msg_id(payload),
        )
    except Exception as exc:
        logger.warning("[PANTHEON] routing NATS publish failed: %s", exc)


def _audit_values(payload: dict[str, Any], metadata: dict[str, Any]) -> tuple[Any, ...]:
    event = routing_event_payload(payload, metadata)
    cost = payload.get("cost_usd")
    return (
        str(payload.get("request_id") or ""),
        str(payload.get("tenant_user_id") or ""),
        str(payload.get("alias_or_model") or ""),
        str(payload.get("resolved_to") or ""),
        str(payload.get("outcome") or ""),
        int(round(safe_float(payload.get("latency_ms")))),
        payload.get("tokens_in"),
        payload.get("tokens_out"),
        Decimal(str(cost)) if cost is not None else None,
        payload.get("error_class"),
        json.dumps(event, sort_keys=True, default=str, separators=(",", ":")),
    )


def routing_audit_record(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return dict(zip(_AUDIT_RECORD_FIELDS, _audit_values(payload, metadata), strict=True))


async def insert_routing_audit_record(backend: Any, record: dict[str, Any]) -> bool:
    insert = getattr(backend, "insert_pantheon_routing_audit", None)
    transactional = getattr(backend, "transactional", None)
    if not callable(insert) or not callable(transactional):
        return False
    async with transactional() as tx:
        await insert(tx, record)
    return True


async def write_routing_audit(payload: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Write one routing decision to ``pantheon_routing_audit``.

    The table is the canonical PANTHEON telemetry sink; MEMORY rows are no
    longer used for routing audit events.
    """
    try:
        record = routing_audit_record(payload, metadata)
        backend = _lc._persistence_backend
        if backend is not None and await insert_routing_audit_record(backend, record):
            return

        pool = _lc._pool
        if pool is None:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pantheon_routing_audit
                       (request_id, tenant_user_id, alias_or_model, resolved_to, outcome,
                        latency_ms, tokens_in, tokens_out, cost_usd, error_class, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                """,
                record["request_id"],
                record["tenant_user_id"],
                record["alias_or_model"],
                record["resolved_to"],
                record["outcome"],
                record["latency_ms"],
                record["tokens_in"],
                record["tokens_out"],
                record["cost_usd"],
                record["error_class"],
                record["payload_json"],
            )
    except Exception as exc:
        logger.debug("[PANTHEON] routing-audit write failed: %s", exc)


async def write_routing_memory(payload: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Compatibility alias for the routing audit table writer."""
    await write_routing_audit(payload, metadata)


def _routing_log_settings() -> tuple[int, int]:
    settings = get_settings().pantheon
    return (
        max(1, int(settings.routing_log_queue_size)),
        max(1, int(settings.routing_log_drain_workers)),
    )


def _get_routing_log_queue() -> asyncio.Queue[_RoutingLogItem]:
    global _routing_log_queue
    size, _workers = _routing_log_settings()
    if _routing_log_queue is None or _routing_log_queue.maxsize != size:
        _routing_log_queue = asyncio.Queue(maxsize=size)
    return _routing_log_queue


async def _drain_routing_log_queue() -> None:
    global _routing_log_drainers
    queue = _get_routing_log_queue()
    try:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await write_routing_audit(item.payload, item.metadata)
                await publish_routing_event(item.payload, item.metadata)
            finally:
                queue.task_done()
    finally:
        _routing_log_drainers = max(0, _routing_log_drainers - 1)


def _ensure_routing_log_drainers() -> None:
    global _routing_log_drainers
    queue = _get_routing_log_queue()
    _size, workers = _routing_log_settings()
    while _routing_log_drainers < workers and not queue.empty():
        try:
            _lc._schedule_background(_drain_routing_log_queue())
        except RuntimeError as exc:
            logger.debug("[PANTHEON] routing-log scheduling failed: %s", exc)
            return
        _routing_log_drainers += 1


def schedule_routing_audit(payload: dict[str, Any], metadata: dict[str, Any]) -> None:
    queue = _get_routing_log_queue()
    item = _RoutingLogItem(dict(payload), dict(metadata))
    if queue.full():
        try:
            queue.get_nowait()
            queue.task_done()
            logger.warning("[PANTHEON] routing-log queue full; dropped oldest event")
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        logger.warning("[PANTHEON] routing-log queue full; dropped newest event")
        return
    _ensure_routing_log_drainers()


def schedule_routing_memory(payload: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Compatibility alias for scheduling routing audit writes."""
    schedule_routing_audit(payload, metadata)


# #192: removed `drain_routing_log_queue_for_tests` — exported in
# `__all__` but no test or script ever called it. The drainers
# spawned by `_ensure_routing_log_drainers` are background tasks
# that flush the queue cooperatively; tests that need to wait
# can `await asyncio.sleep(...)` or rely on `_get_routing_log_queue
# ().join()` directly if a future drain probe is needed.


__all__ = [
    "PANTHEON_ROUTING_SCHEMA_VERSION",
    "PANTHEON_ROUTING_SUBJECT",
    "publish_routing_event",
    "routing_event_payload",
    "routing_payload",
    "schedule_routing_audit",
    "schedule_routing_memory",
    "write_routing_audit",
    "write_routing_memory",
]
