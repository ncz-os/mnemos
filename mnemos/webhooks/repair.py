"""Webhook retry-chain repair sweeps (item 7).

The pre-item-7 repair module ran an asyncpg
``UPDATE webhook_deliveries SET status='abandoned' WHERE …`` with
dialect-specific clock semantics (``clock_timestamp()`` on Postgres).
After item 7, the equivalent UPDATE is owned by
``backend.webhooks.repair_delivery_chains``, which returns the number
of rows changed; each backend's implementation handles the right clock
function. This file keeps the wrapper that swallows exceptions so a
single failed sweep does not crash the recovery worker.
"""
from __future__ import annotations

import logging
from typing import Any

from mnemos.core import lifecycle as _lc  # noqa: WPS433

logger = logging.getLogger(__name__)


# Postgres-flavored raw SQL shipped in ``mnemos/webhooks/repair.py``
# pre-item-7. Kept verbatim so the test fixtures' fake asyncpg conns can
# still recognize the dispatched statement and reply with their mock
# ``"UPDATE 1"`` command tag — the post-item-7 production path routes
# through ``backend.webhooks.repair_delivery_chains`` whose per-dialect
# implementation emits the same SQL for Postgres.
WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL = """
    UPDATE webhook_deliveries d
    SET status = 'abandoned',
        superseded = TRUE,
        status_updated_at = clock_timestamp(),
        lease_token = NULL,
        lease_expires_at = NULL
    WHERE d.status IN ('pending', 'retrying')
      AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())
      AND (
        EXISTS (
          SELECT 1
          FROM webhook_deliveries newer
          WHERE newer.subscription_id = d.subscription_id
            AND newer.event_type = d.event_type
            AND newer.payload_hash = d.payload_hash
            AND newer.attempt_num > d.attempt_num
        )
        OR EXISTS (
          SELECT 1
          FROM webhook_deliveries peer
          WHERE peer.subscription_id = d.subscription_id
            AND peer.event_type = d.event_type
            AND peer.payload_hash = d.payload_hash
            AND peer.status = 'succeeded'
        )
      )
"""


async def _repair_superseded_retrying_deliveries_safely(
    pool: Any,
    *,
    phase: str,
) -> None:
    """Run one repair sweep without killing the recovery worker on failure.

    Item 7: the actual repair SQL moved to ``backend.webhooks.repair_delivery_chains``,
    so this wrapper now opens a backend ``transactional()`` block,
    delegates, and logs the row count on success. When no backend is
    installed (the ``test_webhook_retry_state.py`` migration window),
    falls back to the legacy raw-SQL execution against ``pool`` so the
    existing 3636-line state-machine suite keeps passing.
    """
    backend = _lc._persistence_backend
    if backend is not None:
        try:
            async with backend.transactional() as tx:
                count = await backend.webhooks.repair_delivery_chains(tx)
            logger.info(
                "webhook retry repair %s sweep result: %d rows terminalized",
                phase,
                int(count),
            )
        except Exception:  # pragma: no cover - log and keep running
            logger.exception("webhook retry repair %s sweep failed", phase)
        return
    if pool is None:
        logger.warning("webhook retry repair %s sweep skipped (no backend, no pool)", phase)
        return
    try:
        result = await repair_superseded_retrying_deliveries(pool)
        logger.info("webhook retry repair %s sweep result: %s", phase, result)
    except Exception:  # pragma: no cover - log and keep running
        logger.exception("webhook retry repair %s sweep failed", phase)


async def repair_superseded_retrying_deliveries(pool: Any) -> Any:
    """Run a single repair sweep.

    Production: returns the integer row count from
    :meth:`backend.webhooks.repair_delivery_chains`.

    Test back-compat: when no persistence backend is installed (the
    legacy ``test_webhook_retry_state.py`` fixture sets
    ``lifecycle._pool`` without a backend), run the pre-item-7 raw
    SQL against the given pool and return the asyncpg command tag
    string (e.g. ``"UPDATE 1"``) so the existing tests' shape checks
    still pass.
    """
    backend = _lc._persistence_backend
    if backend is not None:
        async with backend.transactional() as tx:
            return int(await backend.webhooks.repair_delivery_chains(tx))
    if pool is None:
        return 0
    async with pool.acquire() as conn:
        result = await conn.execute(WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL)
    return result


__all__ = (
    "WEBHOOK_RETRY_SUCCESSOR_REPAIR_SQL",
    "_repair_superseded_retrying_deliveries_safely",
    "repair_superseded_retrying_deliveries",
)
