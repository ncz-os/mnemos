"""Webhook lifespan worker loops and recovery claiming (item 7).

The pre-item-7 worker took an ``asyncpg.Pool`` and ran a hand-rolled
``claim_recoverable_deliveries`` CTE (``FOR UPDATE SKIP LOCKED`` on
Postgres). Item 7 folds the equivalent state machine into
``backend.webhooks.claim_due_deliveries``, which returns the same
claimed-row shape (the per-delivery ``url``, ``secret``, ``revoked``,
``owner_id``, ``namespace``, ``lease_expires_at``, ``claim_db_now``
fields) joined in a single round-trip; backends that don't speak
``SKIP LOCKED`` (SQLite, Oracle non-FOR-UPDATE) implement the same
external contract via internal serialization.

The three perpetual loops (repair / recovery / delivery) become thin
schedulers around the ABC call. Their cancellation semantics, sleep
cadence, and exception-isolation behavior are unchanged — only the
persistent-state operation inside the recovery loop was migrated.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Optional

from mnemos.core import lifecycle as _lc  # noqa: WPS433

from . import repair as webhook_repair
from . import types as webhook_types
from .lease import _RecordView
from .types import _ClaimedDelivery, _get_send_semaphore

logger = logging.getLogger(__name__)


async def repair_worker_loop(pool_or_backend: Any) -> None:
    """Background loop: repair superseded retry rows on its own cadence.

    Accepts either a legacy ``asyncpg.Pool`` or the persistence backend.
    The parameter is forwarded to
    :func:`webhook_repair._repair_superseded_retrying_deliveries_safely`
    so the legacy ``asyncpg`` fallback path used while
    ``test_webhook_retry_state.py`` migrates keeps receiving the test
    pool. Production callers that pass the backend directly still work
    because the ``safe`` wrapper checks ``_lc._persistence_backend``
    first and ignores the pool argument when a backend is installed.
    """
    logger.info("webhook retry repair worker started")
    loop = asyncio.get_running_loop()
    repair_burst_deadline = loop.time() + webhook_types.REPAIR_BURST_SECONDS
    next_repair_at = loop.time()
    while True:
        try:
            now = loop.time()
            if now >= next_repair_at:
                in_burst = now < repair_burst_deadline
                await webhook_repair._repair_superseded_retrying_deliveries_safely(
                    pool_or_backend,
                    phase="burst" if in_burst else "periodic",
                )
                next_repair_at = now + (
                    webhook_types.REPAIR_BURST_INTERVAL if in_burst else webhook_types.REPAIR_PERIODIC_INTERVAL
                )

            await asyncio.sleep(max(0.0, next_repair_at - loop.time()))
        except asyncio.CancelledError:
            logger.info("webhook retry repair worker cancelled")
            raise
        except Exception:  # pragma: no cover - log and keep running
            logger.exception("webhook retry repair worker iteration failed")
            await asyncio.sleep(webhook_types.REPAIR_BURST_INTERVAL)


async def delivery_worker_loop(_pool_or_backend: Any = None) -> None:
    """Background loop: picks up pending deliveries whose scheduled_at has arrived."""
    logger.info("webhook delivery recovery worker started")
    pool_or_backend = _pool_or_backend
    while True:
        try:
            await _recover_due_deliveries(pool_or_backend)
            await asyncio.sleep(webhook_types.RECOVERY_POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("webhook delivery recovery worker cancelled")
            raise
        except Exception:  # pragma: no cover - log and keep running
            logger.exception("webhook delivery recovery worker iteration failed")
            await asyncio.sleep(webhook_types.RECOVERY_POLL_INTERVAL)


async def recovery_worker_loop(pool_or_backend: Any = None) -> None:
    """Compatibility wrapper for the delivery recovery loop."""
    await delivery_worker_loop(pool_or_backend)


async def _recover_due_deliveries(_pool_or_backend: Any = None, *, limit: int = 50) -> int:
    """Recover due deliveries by scheduling lifecycle-tracked send attempts.

    Production: walks ``backend.webhooks.claim_due_deliveries`` (item 7
    batch claim) and schedules lifecycle-tracked send tasks.

    Test back-compat: ``test_webhook_retry_state.py`` injects a raw
    ``asyncpg.Pool`` via ``monkeypatch.setattr(lc, '_pool', pool)``
    without installing a backend. When called from the dispatcher
    legacy-attr shim with that pool as the positional arg, we fall
    back to the pre-item-7 raw ``asyncpg`` claim path and the
    pre-item-7 sender fallback (see ``sender._attempt_delivery``).
    """
    claim_limit = min(50, limit, _semaphore_available())
    if claim_limit <= 0:
        return 0

    backend = _lc._persistence_backend
    if backend is None:
        pool = _lc._pool if _pool_or_backend is None else _pool_or_backend
        if pool is None:
            return 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                claimed_deliveries = await _claim_recoverable_deliveries(
                    conn, limit=claim_limit
                )
        from mnemos.core.lifecycle import _schedule_delivery_attempt  # noqa: WPS433
        from .sender import _attempt_delivery

        for claimed in claimed_deliveries:
            _schedule_delivery_attempt(
                _attempt_delivery(str(claimed.delivery["id"]), pool=pool, claimed=claimed)
            )
        recovered = len(claimed_deliveries)
        if recovered:
            await asyncio.sleep(0)
        return recovered

    lease_seconds = webhook_types.WEBHOOK_LEASE_SECONDS
    import uuid as _uuid

    lease_token = str(_uuid.uuid4())
    pre_claim_monotonic = time.monotonic()
    async with backend.transactional() as tx:
        claimed = await backend.webhooks.claim_due_deliveries(
            tx,
            lease_token=lease_token,
            limit=claim_limit,
            lease_seconds=lease_seconds,
            max_attempts=webhook_types.MAX_ATTEMPTS,
            writer_revision=webhook_types.NEW_CODE_WRITER_REVISION,
        )

    from mnemos.core.lifecycle import _schedule_delivery_attempt  # noqa: WPS433
    from .sender import _attempt_delivery

    for claim in claimed:
        record_view = _RecordView(claim.delivery)
        record_view.lease_expires_at = claim.lease_expires_at  # type: ignore[attr-defined]
        record_view.claim_db_now = claim.claim_db_now  # type: ignore[attr-defined]
        wrapped = _ClaimedDelivery(
            delivery=record_view,
            lease_token=lease_token,
            pre_claim_monotonic=pre_claim_monotonic,
        )
        _schedule_delivery_attempt(
            _attempt_delivery(str(claim.delivery.id), claimed=wrapped)
        )
    recovered = len(claimed)
    if recovered:
        await asyncio.sleep(0)
    return recovered


def _semaphore_available() -> int:
    """Return the current best-effort free slot count for recovery batch sizing."""
    return max(0, _get_send_semaphore()._value)


# --- Legacy raw-asyncpg helpers used by ``test_webhook_retry_state.py`` ---------
# Mirrors the pre-item-7 hand-rolled claim + diagnostic-peek queries so
# the existing 3636-line state-machine test suite keeps compiling and
# passing during its migration to the ABC. Production runtime does
# NOT enter this branch (it routes through
# ``backend.webhooks.claim_due_deliveries``).


async def _claim_recoverable_deliveries(
    conn: Any,
    *,
    limit: int = 50,
    lease_seconds: Optional[int] = None,
) -> list[_ClaimedDelivery]:
    """Pre-item-7 raw-asyncpg batch claim."""
    import uuid as _uuid_mod

    if limit <= 0:
        return []
    if lease_seconds is None:
        lease_seconds = webhook_types.WEBHOOK_LEASE_SECONDS
    lease_token = str(_uuid_mod.uuid4())
    pre_claim_monotonic = time.monotonic()
    rows = await conn.fetch(
        """
        WITH claim_clock AS (
          SELECT clock_timestamp() AS claim_now
        ),
        recoverable AS (
          SELECT d.id
          FROM webhook_deliveries d, claim_clock
          WHERE d.scheduled_at <= claim_clock.claim_now
            AND d.attempt_num <= $1
            AND d.status NOT IN ('succeeded', 'abandoned')
            AND NOT d.superseded
            AND d.status IN ('pending', 'retrying')
            AND (d.lease_token IS NULL OR d.lease_expires_at < claim_clock.claim_now)
            AND d.writer_revision = $5
            AND NOT EXISTS (
              SELECT 1
              FROM webhook_deliveries peer
              WHERE peer.subscription_id = d.subscription_id
                AND peer.event_type = d.event_type
                AND peer.payload_hash = d.payload_hash
                AND peer.status = 'succeeded'
            )
            AND (
              d.status = 'pending'
              OR (
                d.status = 'retrying'
                AND NOT EXISTS (
                  SELECT 1
                  FROM webhook_deliveries newer
                  WHERE newer.subscription_id = d.subscription_id
                    AND newer.event_type = d.event_type
                    AND newer.payload_hash = d.payload_hash
                    AND newer.attempt_num > d.attempt_num
                )
              )
            )
          ORDER BY d.scheduled_at
          LIMIT $3
          FOR UPDATE SKIP LOCKED
        )
        UPDATE webhook_deliveries d
        SET lease_token=$2::uuid,
            lease_expires_at=claim_clock.claim_now + ($4::int * INTERVAL '1 second'),
            status=CASE WHEN d.status = 'pending' THEN 'retrying' ELSE d.status END
        FROM recoverable, webhook_subscriptions s, claim_clock
        WHERE d.id = recoverable.id
          AND s.id = d.subscription_id
        RETURNING d.id, d.subscription_id, d.event_type, d.payload,
                  d.payload_hash, d.attempt_num, d.status,
                  d.lease_expires_at, claim_clock.claim_now AS claim_db_now,
                  $2::uuid AS lease_token,
                  s.url, s.secret, s.revoked, s.owner_id, s.namespace
        """,
        webhook_types.MAX_ATTEMPTS,
        lease_token,
        limit,
        lease_seconds,
        webhook_types.NEW_CODE_WRITER_REVISION,
    )
    return [
        _ClaimedDelivery(
            delivery=row,
            lease_token=lease_token,
            pre_claim_monotonic=pre_claim_monotonic,
        )
        for row in rows
    ]


async def _recoverable_delivery_ids(
    conn: Any,
    *,
    limit: int = 50,
) -> Iterable[Any]:
    """Pre-item-7 raw-asyncpg diagnostic peek (no claim)."""
    return await conn.fetch(
        """
        SELECT d.id FROM webhook_deliveries d
        WHERE d.scheduled_at <= clock_timestamp()
          AND d.attempt_num <= $1
          AND d.status NOT IN ('succeeded', 'abandoned')
          AND NOT d.superseded
          AND d.status IN ('pending', 'retrying')
          AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())
          AND d.writer_revision = $3
          AND NOT EXISTS (
            SELECT 1
            FROM webhook_deliveries peer
            WHERE peer.subscription_id = d.subscription_id
              AND peer.event_type = d.event_type
              AND peer.payload_hash = d.payload_hash
              AND peer.status = 'succeeded'
          )
          AND (
            d.status = 'pending'
            OR (
              d.status = 'retrying'
              AND NOT EXISTS (
                SELECT 1
                FROM webhook_deliveries newer
                WHERE newer.subscription_id = d.subscription_id
                  AND newer.event_type = d.event_type
                  AND newer.payload_hash = d.payload_hash
                  AND newer.attempt_num > d.attempt_num
              )
            )
          )
        ORDER BY d.scheduled_at
        LIMIT $2
        """,
        webhook_types.MAX_ATTEMPTS, limit, webhook_types.NEW_CODE_WRITER_REVISION,
    )


__all__ = (
    "repair_worker_loop",
    "delivery_worker_loop",
    "recovery_worker_loop",
    "_recover_due_deliveries",
    "_semaphore_available",
    "_claim_recoverable_deliveries",
    "_recoverable_delivery_ids",
)
