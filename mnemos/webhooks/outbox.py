"""Transactional webhook outbox inserts."""

from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import asyncpg

import mnemos.core.lifecycle as lifecycle
from . import types as webhook_types
from .nats_events import publish_delivery_queued, publish_webhook_outbox_insert

logger = logging.getLogger(__name__)

_POST_COMMIT_VISIBILITY_TIMEOUT_SECONDS = 5.0
_POST_COMMIT_VISIBILITY_POLL_SECONDS = 0.025


async def _matching_subscriptions(
    conn: asyncpg.Connection,
    event_type: str,
    owner_id: Optional[str],
    namespace: Optional[str],
) -> Iterable[asyncpg.Record]:
    """Find non-revoked subscriptions that include this event_type.

    If owner_id/namespace are provided, filter to subscriptions with matching
    ownership. Otherwise, return all non-revoked matches (useful for
    system-level events not bound to a caller).
    """
    query = """
        SELECT id, url, events, secret, owner_id, namespace
        FROM webhook_subscriptions
        WHERE NOT revoked AND $1 = ANY(events)
    """
    args: list = [event_type]
    if owner_id is not None:
        query += " AND owner_id = $2"
        args.append(owner_id)
        if namespace is not None:
            query += " AND namespace = $3"
            args.append(namespace)
    return await conn.fetch(query, *args)


async def _publish_delivery_nats_notifications(
    deliveries: Iterable[dict[str, Any]],
    *,
    event_type: str,
    payload_hash: str,
) -> None:
    """Best-effort NATS nudges for already-durable outbox rows."""
    for delivery in deliveries:
        delivery_id = delivery["delivery_id"]
        try:
            publish_args = {
                "delivery_id": delivery_id,
                "subscription_id": delivery["subscription_id"],
                "event_type": event_type,
                "url": delivery["url"],
                "payload_hash": payload_hash,
                "namespace": delivery["namespace"],
                "owner_id": delivery["owner_id"],
            }
            await asyncio.gather(
                publish_delivery_queued(**publish_args),
                publish_webhook_outbox_insert(**publish_args),
            )
        except Exception:
            logger.warning(
                "webhook outbox: NATS publish failed for delivery %s (event=%s); "
                "delivery row is durable, polling worker will pick it up",
                delivery_id,
                event_type,
                exc_info=True,
            )


async def _visible_delivery_ids(delivery_ids: list[str]) -> set[str]:
    pool = getattr(lifecycle, "_pool", None)
    if pool is None:
        return set(delivery_ids)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id
            FROM webhook_deliveries
            WHERE id = ANY($1::uuid[])
            """,
            delivery_ids,
        )
    return {str(row["id"]) for row in rows}


async def _publish_delivery_nats_notifications_after_commit(
    deliveries: list[dict[str, Any]],
    *,
    event_type: str,
    payload_hash: str,
) -> None:
    delivery_by_id = {delivery["delivery_id"]: delivery for delivery in deliveries}
    pending_ids = list(delivery_by_id)
    deadline = asyncio.get_running_loop().time() + _POST_COMMIT_VISIBILITY_TIMEOUT_SECONDS

    while pending_ids:
        try:
            visible_ids = await _visible_delivery_ids(pending_ids)
        except Exception:
            logger.warning(
                "webhook outbox: failed to verify committed delivery rows before NATS publish; "
                "polling worker will pick them up",
                exc_info=True,
            )
            return

        visible_deliveries = [delivery_by_id[delivery_id] for delivery_id in pending_ids if delivery_id in visible_ids]
        if visible_deliveries:
            await _publish_delivery_nats_notifications(
                visible_deliveries,
                event_type=event_type,
                payload_hash=payload_hash,
            )
            pending_ids = [delivery_id for delivery_id in pending_ids if delivery_id not in visible_ids]
            continue

        if asyncio.get_running_loop().time() >= deadline:
            logger.warning(
                "webhook outbox: timed out waiting for %d delivery row(s) to become visible before NATS publish",
                len(pending_ids),
            )
            return
        await asyncio.sleep(_POST_COMMIT_VISIBILITY_POLL_SECONDS)


async def _dispatch_on_conn(
    conn: asyncpg.Connection,
    event_type: str,
    payload: Dict[str, Any],
    *,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> list[str]:
    """Insert delivery intents using an already-selected connection."""
    subs = await _matching_subscriptions(conn, event_type, owner_id, namespace)
    if not subs:
        return []

    body = json.dumps(
        {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    deliveries = [
        {
            "delivery_id": str(uuid.uuid4()),
            "subscription_id": sub["id"],
            "url": sub["url"],
            "namespace": sub["namespace"],
            "owner_id": sub["owner_id"],
        }
        for sub in subs
    ]
    values_sql: list[str] = []
    args: list[Any] = []
    for delivery in deliveries:
        offset = len(args)
        values_sql.append(
            f"(${offset + 1}::uuid, ${offset + 2}, ${offset + 3}, "
            f"${offset + 4}, ${offset + 5}, 'pending', ${offset + 6})"
        )
        args.extend(
            (
                delivery["delivery_id"],
                delivery["subscription_id"],
                event_type,
                body,
                body_hash,
                webhook_types.NEW_CODE_WRITER_REVISION,
            )
        )
    await conn.execute(
        f"""
        INSERT INTO webhook_deliveries
          (id, subscription_id, event_type, payload, payload_hash, status, writer_revision)
        VALUES {", ".join(values_sql)}
        """,
        *args,
    )

    # Outbox relay: best-effort NATS notify. Keep this off the write
    # transaction/connection path; the durable pending rows are already the
    # source of truth, and the polling worker remains the fallback.
    lifecycle._schedule_background(
        _publish_delivery_nats_notifications_after_commit(
            deliveries,
            event_type=event_type,
            payload_hash=body_hash,
        )
    )
    delivery_ids = [delivery["delivery_id"] for delivery in deliveries]
    return delivery_ids
