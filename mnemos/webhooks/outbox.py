"""Transactional webhook outbox inserts."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import asyncpg

from . import types as webhook_types
from .nats_events import publish_delivery_queued, publish_webhook_outbox_insert

logger = logging.getLogger(__name__)


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

    body = json.dumps({
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }, separators=(",", ":"), sort_keys=True)
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    delivery_ids: list[str] = []
    for sub in subs:
        delivery_id = await conn.fetchval(
            """
            INSERT INTO webhook_deliveries
              (subscription_id, event_type, payload, payload_hash, status, writer_revision)
            VALUES ($1, $2, $3, $4, 'pending', $5)
            RETURNING id
            """,
            sub["id"], event_type, body, body_hash, webhook_types.NEW_CODE_WRITER_REVISION,
        )
        # ── Outbox relay: best-effort NATS notify ──
        # Adversarial-review CHILD-E gate (2026-06-05): the legacy
        # _dispatch_on_conn path operates on raw asyncpg without access
        # to PostgresTransaction.add_after_commit. If NATS is down, the
        # publish can raise and roll back the delivery row. Wrapping
        # in try/except ensures the delivery row survives even when
        # NATS is unreachable — the polling worker picks it up on the
        # next cycle. (The PostgresWebhookRepository.dispatch_event path
        # in postgres.py uses proper after_commit for the same guarantee.)
        try:
            await publish_delivery_queued(
                delivery_id=str(delivery_id),
                subscription_id=sub["id"],
                event_type=event_type,
                url=sub["url"],
                payload_hash=body_hash,
                namespace=sub["namespace"],
                owner_id=sub["owner_id"],
            )
            await publish_webhook_outbox_insert(
                delivery_id=str(delivery_id),
                subscription_id=sub["id"],
                event_type=event_type,
                url=sub["url"],
                payload_hash=body_hash,
                namespace=sub["namespace"],
                owner_id=sub["owner_id"],
            )
        except Exception:
            logger.warning(
                "webhook outbox: NATS publish failed for delivery %s (event=%s); "
                "delivery row is durable, polling worker will pick it up",
                delivery_id,
                event_type,
                exc_info=True,
            )
        delivery_ids.append(str(delivery_id))
    return delivery_ids
