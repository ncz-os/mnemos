"""Optional NATS consumer for PANTHEON routing audit events."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Mapping

from mnemos.core.config import Settings, get_settings
from mnemos.core.extras import is_extra_installed
from mnemos.domain.pantheon.routing_log import (
    PANTHEON_ROUTING_SUBJECT,
    insert_routing_audit_record,
)
from mnemos.nats.backoff import ReconnectBackoff

logger = logging.getLogger("mnemos.workers.pantheon_routing_audit_consumer")

STREAM = "MNEMOS_PANTHEON"
DURABLE = "mnemos_pantheon_routing_audit"

_AUDIT_FIELDS = (
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
    "payload",
)

_INSERT_SQL_POSTGRES = """
INSERT INTO pantheon_routing_audit
       (request_id, tenant_user_id, alias_or_model, resolved_to, outcome,
        latency_ms, tokens_in, tokens_out, cost_usd, error_class, payload)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
"""

_INSERT_SQL_ORACLE = """
INSERT INTO pantheon_routing_audit
       (request_id, tenant_user_id, alias_or_model, resolved_to, outcome,
        latency_ms, tokens_in, tokens_out, cost_usd, error_class, payload)
VALUES (:request_id, :tenant_user_id, :alias_or_model, :resolved_to, :outcome,
        :latency_ms, :tokens_in, :tokens_out, :cost_usd, :error_class, :payload)
"""

_INSERT_SQL_DB2 = """
INSERT INTO pantheon_routing_audit
       (request_id, tenant_user_id, alias_or_model, resolved_to, outcome,
        latency_ms, tokens_in, tokens_out, cost_usd, error_class, payload)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class PoisonMessageError(ValueError):
    """Message cannot be decoded into the PANTHEON routing audit shape."""


async def consumer_loop(
    pool: Any,
    *,
    settings: Settings | None = None,
    retry_seconds: float = 30.0,
    connect: Callable[[Settings], Awaitable[Any | None]] | None = None,
) -> None:
    """Consume PANTHEON routing events until cancelled."""
    settings = settings or get_settings()
    if not is_extra_installed("nats"):
        logger.info("PANTHEON routing audit consumer disabled (nats extra not installed)")
        return
    if not settings.nats.audit_consumer_enabled:
        logger.info("PANTHEON routing audit consumer disabled")
        return
    if not settings.nats.url:
        logger.info("PANTHEON routing audit consumer disabled (MNEMOS_NATS_URL unset)")
        return

    connect = connect or _connect
    backoff = ReconnectBackoff(base_seconds=1.0, cap_seconds=retry_seconds)

    while True:
        nc = None
        sub = None
        try:
            connect_result = await connect(settings)
            if isinstance(connect_result, tuple) and len(connect_result) == 2:
                nc, js = connect_result
            else:
                nc, js = None, connect_result
            if js is None:
                raise RuntimeError("NATS JetStream unavailable")
            logger.info("PANTHEON routing audit consumer connected subject=%s", PANTHEON_ROUTING_SUBJECT)
            sub = await _subscribe(js)
            backoff.reset()
            sub_owned = sub
            sub = None
            try:
                await _consume_subscription(pool, sub_owned)
            finally:
                await _drain_partial(nc, [sub_owned])
                nc = None
        except asyncio.CancelledError:
            logger.info("PANTHEON routing audit consumer cancelled")
            await _drain_partial(nc, [s for s in [sub] if s is not None])
            raise
        except Exception as exc:
            await _drain_partial(nc, [s for s in [sub] if s is not None])
            delay = backoff.next_delay()
            logger.warning(
                "PANTHEON routing audit consumer unavailable: %s; retrying in %.1fs",
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def handle_message(pool: Any, msg: Any) -> None:
    """Persist one PANTHEON routing event from NATS into the audit table."""
    event = _decode_payload(getattr(msg, "data", b""))
    await insert_audit_event(pool, event)


async def insert_audit_event(pool: Any, event: Mapping[str, Any]) -> None:
    """Insert a decoded routing event into ``pantheon_routing_audit``."""
    record = _audit_record(event)
    backend = _audit_persistence_backend(pool)
    if backend is not None and await insert_routing_audit_record(backend, record):
        return

    await _insert_audit_record_via_pool(pool, record)


def _audit_record(event: Mapping[str, Any]) -> dict[str, Any]:
    payload_json = json.dumps(event, sort_keys=True, default=str, separators=(",", ":"))
    return {
        "request_id": _text_field(event, "request_id"),
        "tenant_user_id": _text_field(event, "tenant_user_id"),
        "alias_or_model": _text_field(event, "alias_or_model"),
        "resolved_to": _text_field(event, "resolved_to"),
        "outcome": _text_field(event, "outcome"),
        "latency_ms": _int_field(event, "latency_ms"),
        "tokens_in": _int_field(event, "tokens_in"),
        "tokens_out": _int_field(event, "tokens_out"),
        "cost_usd": _decimal_field(event, "cost_usd"),
        "error_class": _text_field(event, "error_class"),
        "payload_json": payload_json,
    }


def _audit_persistence_backend(target: Any) -> Any | None:
    for candidate in (target, getattr(target, "persistence_backend", None)):
        if _supports_audit_repository(candidate):
            return candidate
    return None


def _supports_audit_repository(candidate: Any) -> bool:
    return callable(getattr(candidate, "transactional", None)) and callable(
        getattr(candidate, "insert_pantheon_routing_audit", None)
    )


def _record_values(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("request_id"),
        record.get("tenant_user_id"),
        record.get("alias_or_model"),
        record.get("resolved_to"),
        record.get("outcome"),
        record.get("latency_ms"),
        record.get("tokens_in"),
        record.get("tokens_out"),
        record.get("cost_usd"),
        record.get("error_class"),
        record.get("payload_json"),
    )


async def _insert_audit_record_via_pool(pool: Any, record: Mapping[str, Any]) -> None:
    values = _record_values(record)
    dialect = _audit_insert_dialect(pool)
    if dialect == "unsupported":
        logger.debug("PANTHEON routing audit insert skipped for non-Postgres-compatible backend")
        return

    async with pool.acquire() as conn:
        if dialect == "oracle":
            await _execute_cursor_insert(conn, _INSERT_SQL_ORACLE, dict(zip(_AUDIT_FIELDS, values)))
        elif dialect == "db2":
            await _execute_cursor_insert(conn, _INSERT_SQL_DB2, values)
        else:
            await conn.execute(_INSERT_SQL_POSTGRES, *values)


def _audit_insert_dialect(pool: Any) -> str:
    candidates = (
        getattr(pool, "persistence_backend", None),
        getattr(pool, "_pool", None),
        pool,
    )
    for candidate in candidates:
        if candidate is None:
            continue
        marker = f"{type(candidate).__module__}.{type(candidate).__name__}".lower()
        if "db2" in marker:
            return "db2"
        if "oracle" in marker or "oracledb" in marker:
            return "oracle"
        if "mysql" in marker or "sqlite" in marker:
            return "unsupported"
        if "asyncpg" in marker or "postgres" in marker:
            return "postgres"
    return "postgres"


async def _execute_cursor_insert(conn: Any, sql: str, params: Any) -> None:
    cursor = conn.cursor()
    try:
        await _maybe_await(cursor.execute(sql, params))
    finally:
        close = getattr(cursor, "close", None)
        if close is not None:
            await _maybe_await(close())

    commit = getattr(conn, "commit", None)
    if commit is not None:
        await _maybe_await(commit())


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


async def _consume_subscription(pool: Any, sub: Any) -> None:
    while True:
        msg = None
        try:
            msg = await sub.next_msg(timeout=1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_timeout(exc):
                continue
            logger.exception("PANTHEON routing audit receive error (escaping for reconnect): %s", exc)
            raise

        try:
            await handle_message(pool, msg)
        except asyncio.CancelledError:
            raise
        except PoisonMessageError as exc:
            logger.warning(
                "PANTHEON routing audit poison message subject=%s detail=%s",
                getattr(msg, "subject", "?"),
                exc,
            )
            await _ack_safely(msg)
            continue
        except Exception as exc:
            logger.exception(
                "PANTHEON routing audit handler error (subscription stays alive, no ack): %s",
                exc,
            )
            continue

        try:
            await _ack(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("PANTHEON routing audit ack error (escaping for reconnect): %s", exc)
            raise


async def _connect(settings: Settings):
    try:
        import nats  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("nats-py not installed") from exc

    connect_kwargs: dict[str, Any] = {"servers": [settings.nats.url]}
    if settings.nats.token:
        connect_kwargs["token"] = settings.nats.token
    nc = await nats.connect(**connect_kwargs)
    return nc, nc.jetstream()


async def _subscribe(js: Any):
    try:
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy  # type: ignore

        config = ConsumerConfig(
            durable_name=DURABLE,
            deliver_policy=DeliverPolicy.NEW,
            ack_policy=AckPolicy.EXPLICIT,
        )
    except ImportError:
        config = None

    return await js.subscribe(
        PANTHEON_ROUTING_SUBJECT,
        durable=DURABLE,
        stream=STREAM,
        config=config,
    )


async def _drain_partial(nc: Any, subscriptions: list[Any]) -> None:
    for sub in subscriptions:
        try:
            await sub.unsubscribe()
        except Exception:
            pass
    if nc is not None:
        try:
            await nc.drain()
        except Exception:
            try:
                await nc.close()
            except Exception:
                pass


def _decode_payload(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise PoisonMessageError("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PoisonMessageError("payload must be a JSON object")
    return payload


def _field(event: Mapping[str, Any], key: str) -> Any:
    if key in event:
        return event.get(key)
    metadata = event.get("metadata")
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return None


def _text_field(event: Mapping[str, Any], key: str) -> str | None:
    value = _field(event, key)
    if value is None:
        return None
    return str(value)


def _int_field(event: Mapping[str, Any], key: str) -> int | None:
    value = _field(event, key)
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _decimal_field(event: Mapping[str, Any], key: str) -> Decimal | None:
    value = _field(event, key)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_timeout(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    return isinstance(exc, asyncio.TimeoutError) or "timeout" in name


async def _ack(msg: Any) -> None:
    ack = getattr(msg, "ack", None)
    if ack is None:
        return
    result = ack()
    if hasattr(result, "__await__"):
        await result


async def _ack_safely(msg: Any) -> None:
    try:
        await _ack(msg)
    except Exception as exc:
        logger.warning("PANTHEON routing audit poison-ack failed (will be redelivered): %s", exc)


async def main() -> None:
    from mnemos.core import lifecycle

    settings = get_settings()
    _backend_type, backend = await lifecycle.build_configured_persistence_backend(settings)
    try:
        await consumer_loop(backend, settings=settings)
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
