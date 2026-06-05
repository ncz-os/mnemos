"""NATS event helpers for webhook delivery outbox rows.

Lives under ``mnemos.nats`` (not ``mnemos.webhooks``) so the
persistence layer can publish queued-delivery nudges without the
import-linter ``persistence has no upward deps`` contract being
violated. ``mnemos.webhooks.nats_events`` re-exports these symbols
for callers in the webhook layer.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from mnemos import nats as nats_bus
from mnemos.core.config import get_settings
from mnemos.nats import client as nats_client

logger = logging.getLogger(__name__)


def safe_namespace(namespace: str | None) -> str:
    """Return a namespace token suitable for a NATS subject segment."""
    value = namespace or "default"
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return safe or "default"


async def publish_delivery_queued(
    *,
    delivery_id: str,
    subscription_id: Any,
    event_type: str,
    url: str,
    payload_hash: str,
    namespace: str | None,
    owner_id: str | None,
) -> None:
    """Best-effort webhook delivery queued event.

    The Postgres outbox remains authoritative. This event only nudges
    dispatchers so they can attempt the existing row immediately.

    Guarded with a publish timeout so a hung NATS connection cannot
    block the outbox transaction — the delivery row is already durable.
    """
    subject = f"mnemos.webhook.delivery.queued.{safe_namespace(namespace)}"
    payload = {
        "delivery_id": str(delivery_id),
        "subscription_id": str(subscription_id),
        "event_type": event_type,
        "url": url,
        "payload_hash": payload_hash,
        "namespace": namespace,
        "owner_id": owner_id,
        "source_node": nats_client.get_node_name(),
    }
    timeout = float(get_settings().nats.publish_timeout_seconds)
    try:
        await asyncio.wait_for(
            nats_bus.publish_event(
                subject,
                payload,
                msg_id=f"webhook.delivery.{delivery_id}.queued",
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "NATS publish_delivery_queued timed out after %.3fs for %s",
            timeout, subject,
        )
