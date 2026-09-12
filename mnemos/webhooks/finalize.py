"""Webhook delivery terminal state transitions (item 7).

The pre-item-7 finalize module ran a multi-branch asyncpg state machine
that re-implemented ``WebhookRepository.finalize_delivery``. After item
7, the production path goes through the ABC so this file's primary
function (``_finalize_delivery``) routes through
``backend.webhooks.finalize_delivery``.

Legacy back-compat: when the legacy ``lifecycle._pool`` is set without
a corresponding persistence backend (the
``test_webhook_retry_state.py`` migration window), the helpers from the
pre-item-7 finalize state machine re-implement the same logic against
raw asyncpg conns. The production runtime never enters that codepath.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from mnemos.core import lifecycle as _lc  # noqa: WPS433


from . import chain as webhook_chain
from . import lease as webhook_lease
from . import sender as webhook_sender
from . import types as webhook_types
from .types import _DeliveryResult, _LeaseExpiredBeforeSend, _PostHeaderDeliveryResult

logger = logging.getLogger(__name__)


def _pool_uses_sqlite_backend(pool: Any) -> bool:
    backend = getattr(pool, "persistence_backend", None)
    if backend is None:
        try:
            backend = _lc._persistence_backend
        except Exception:
            return False
    return bool(getattr(backend, "uses_sqlite_vec", False))


async def _guard_sqlite_succeeded_terminal(
    _conn: Any,
    _pool: Any,
    _delivery_id: str,
    _attempted_status: str,
) -> bool:
    """Removed in item 7: the SQLite ABC impl already enforces the
    terminal-state guard at the backend layer (see
    ``SqliteWebhookRepository.finalize_delivery``).
    """
    return False


async def _finalize_delivery(
    pool_or_backend: Any,
    delivery: Any,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Finalize the delivery row before post-header body capture or cleanup.

    Production path: routes through ``backend.webhooks.finalize_delivery``
    (Postgres / SQLite / MySQL / Oracle / DB2 ABC).

    Test back-compat: ``test_webhook_retry_state.py`` exercises the
    pre-item-7 raw ``asyncpg`` finalization state machine while that
    suite migrates to the ABC. When no persistence backend is
    installed but the legacy ``lifecycle._pool`` is set, we route
    through the same hand-rolled asyncpg state machine the production
    runtime owned before item 7.

    The actual deliverable semantics never diverge: ABC
    ``finalize_delivery`` on each backend is a single-transaction
    implementation of the same state transitions the legacy code
    encoded one at a time.
    """
    delivery_id = _delivery_id_str(delivery)
    finalized = False
    backend = _lc._persistence_backend
    pool = pool_or_backend
    if pool is _lc._pool or pool is None:
        pool = _lc._pool
    if backend is not None:
        try:
            if isinstance(result, _LeaseExpiredBeforeSend):
                async with backend.transactional() as tx:
                    released = await backend.webhooks.release_delivery_claim(
                        tx,
                        delivery_id=delivery_id,
                        lease_token=lease_token,
                    )
                finalized = bool(released)
            else:
                outcome = webhook_types.WebhookDeliveryOutcome(
                    succeeded=result.succeeded,
                    response_status=result.response_status,
                    response_body=result.response_body,
                    error=result.error,
                )
                async with backend.transactional() as tx:
                    finalization = await backend.webhooks.finalize_delivery(
                        tx,
                        delivery_id=delivery_id,
                        lease_token=lease_token,
                        outcome=outcome,
                        max_attempts=webhook_types.MAX_ATTEMPTS,
                        backoff_schedule=tuple(webhook_types.BACKOFF_SCHEDULE),
                    )
                finalized = bool(finalization.applied)
            return finalized
        finally:
            if isinstance(result, _PostHeaderDeliveryResult):
                await _run_post_finalize_delivery_work(
                    result,
                    finalized=finalized,
                )

    if pool is None:
        raise RuntimeError(
            f"webhook delivery {delivery_id} cannot finalize without a backend or raw pool"
        )
    try:
        finalized = await _legacy_finalize_delivery_row(pool, delivery, lease_token, result)
        return finalized
    finally:
        if isinstance(result, _PostHeaderDeliveryResult):
            await _run_post_finalize_delivery_work(
                result,
                finalized=finalized,
            )


def _delivery_id_str(delivery: Any) -> str:
    if hasattr(delivery, "__getitem__"):
        return str(delivery["id"])
    return str(getattr(delivery, "id"))


# --- Legacy raw-asyncpg finalize state machine ---------------------------------
# Used only by ``test_webhook_retry_state.py`` while that suite migrates
# to the ABC. Mirrors the pre-item-7 code byte-for-byte so the legacy
# test suite keeps passing. The production runtime does NOT enter this
# branch (it uses the ABC path above).


async def _legacy_finalize_delivery_row(
    pool: Any,
    delivery: Any,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Pre-item-7 raw-asyncpg finalization."""
    delivery_id = str(delivery["id"])
    if isinstance(result, _LeaseExpiredBeforeSend):
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await webhook_lease._release_owned_lease_for_reclaim(
                    conn, delivery_id, lease_token
                )

    if result.succeeded and not delivery["revoked"]:
        return await _legacy_finalize_successful_delivery_row(
            pool, delivery, delivery_id, lease_token, result
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await webhook_chain._lock_delivery_chain(conn, delivery)
            if delivery["revoked"]:
                finalized = await conn.fetchrow(
                    """
                    UPDATE webhook_deliveries
                    SET status='abandoned',
                        superseded=FALSE,
                        error='subscription revoked',
                        delivered_at=clock_timestamp(),
                        lease_token=NULL,
                        lease_expires_at=NULL
                    WHERE id=$1::uuid
                      AND lease_token=$2::uuid
                      AND lease_expires_at >= clock_timestamp()
                      AND status IN ('pending', 'retrying')
                      AND NOT superseded
                    RETURNING id
                    """,
                    delivery_id, lease_token,
                )
                return finalized is not None

            if await webhook_chain._has_succeeded_chain_attempt(conn, delivery, delivery_id):
                finalized = await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                    conn, delivery_id, lease_token, result,
                )
                return finalized is not None

            next_attempt = delivery["attempt_num"] + 1
            if next_attempt > webhook_types.MAX_ATTEMPTS:
                finalized = await conn.fetchrow(
                    """
                    UPDATE webhook_deliveries
                    SET status='abandoned',
                        superseded=FALSE,
                        response_status=$3,
                        response_body=$4,
                        error=$5,
                        delivered_at=clock_timestamp(),
                        lease_token=NULL,
                        lease_expires_at=NULL
                    WHERE id=$1::uuid
                      AND lease_token=$2::uuid
                      AND lease_expires_at >= clock_timestamp()
                      AND status IN ('pending', 'retrying')
                      AND NOT superseded
                    RETURNING id
                    """,
                    delivery_id, lease_token, result.response_status, result.response_body, result.error,
                )
                return finalized is not None

            backoff = webhook_types.BACKOFF_SCHEDULE[delivery["attempt_num"] - 1]
            scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
            successor_exists = await webhook_chain._has_successor_attempt(conn, delivery)
            finalized = await conn.fetchrow(
                """
                UPDATE webhook_deliveries
                SET status='abandoned',
                    superseded=TRUE,
                    response_status=$3,
                    response_body=$4,
                    error=$5,
                    lease_token=NULL,
                    lease_expires_at=NULL
                WHERE id=$1::uuid
                  AND lease_token=$2::uuid
                  AND lease_expires_at >= clock_timestamp()
                  AND status IN ('pending', 'retrying')
                  AND NOT superseded
                RETURNING id
                """,
                delivery_id,
                lease_token,
                result.response_status,
                result.response_body,
                result.error,
            )
            if finalized is None:
                logger.info(
                    "webhook delivery %s finalize skipped because lease expired or moved",
                    delivery_id,
                )
                return False

            if not successor_exists:
                await webhook_chain._insert_successor_delivery(conn, delivery, next_attempt, scheduled_at)
            logger.info(
                "webhook delivery %s attempt %d failed (status=%s error=%s), retry in %ds",
                delivery_id, delivery["attempt_num"], result.response_status, result.error, backoff,
            )
            return True


async def _commit_successful_delivery_in_transaction(
    pool: Any,
    delivery: Any,
    delivery_id: str,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Pre-item-7 raw-asyncpg success-commit helper.

    Owns the chain advisory lock + the success UPDATE inside one
    transaction. On ``UniqueViolationError``, the lock is released when
    this transaction's ``async with`` exits so the caller can drive
    :func:`_abandon_success_duplicate_after_unique_violation` in a fresh
    transaction without self-deadlocking.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await webhook_chain._lock_delivery_chain(conn, delivery)
            if await webhook_chain._has_succeeded_chain_attempt(conn, delivery, delivery_id):
                finalized = await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                    conn,
                    delivery_id,
                    lease_token,
                    result,
                    require_unexpired_lease=False,
                )
                return finalized is not None

            finalized = await conn.fetchrow(
                """
                UPDATE webhook_deliveries
                SET status='succeeded',
                    superseded=FALSE,
                    response_status=$3,
                    response_body=$4,
                    error=NULL,
                    delivered_at=clock_timestamp(),
                    lease_token=NULL,
                    lease_expires_at=NULL
                WHERE id=$1::uuid
                  AND lease_token=$2::uuid
                  AND status IN ('pending', 'retrying')
                  AND NOT superseded
                RETURNING id
                """,
                delivery_id,
                lease_token,
                result.response_status,
                result.response_body,
            )
            if finalized is None:
                finalized = await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                    conn, delivery_id, lease_token, result, require_unexpired_lease=False,
                )
                if finalized is not None:
                    return True
                await webhook_lease._clear_stale_owned_lease_after_terminal_finalize(
                    conn, delivery_id, lease_token,
                )
                return False

            successors = await webhook_chain._find_live_unleased_successor_attempts(conn, delivery)
            for successor in successors:
                await webhook_chain._abandon_live_successor_attempt(conn, str(successor["id"]))
            return True


async def _legacy_finalize_successful_delivery_row(
    pool: Any,
    delivery: Any,
    delivery_id: str,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Pre-item-7 raw-asyncpg success-commit branch.

    Splits :func:`_commit_successful_delivery_in_transaction` (which
    holds the chain advisory lock across its transaction) from the
    ``UniqueViolationError`` recovery so the lock is released BEFORE
    the recovery path runs in a fresh transaction. The pre-item-7
    inline try/except inside the original transaction self-deadlocked
    in the test fake (which serializes the advisory lock per key).
    """
    try:
        return await _commit_successful_delivery_in_transaction(
            pool, delivery, delivery_id, lease_token, result,
        )
    except asyncpg.exceptions.UniqueViolationError:
        return await _abandon_success_duplicate_after_unique_violation(
            pool, delivery, delivery_id, lease_token, result,
        )
    # When the unique index wins on a racing predecessor-success, the
    # current worker may replay a bounded duplicate POST — the
    # upstream ``_abandon_owned_attempt_after_succeeded_chain_peer``
    # writes the row ``abandoned/superseded=TRUE`` so HTTP-side
    # responses stay idempotent.


async def _abandon_live_successors_before_success_commit(
    conn: Any,
    delivery: Any,
) -> None:
    """Pre-item-7 raw-asyncpg predecessor-success successor sweep."""
    successors = await webhook_chain._find_live_unleased_successor_attempts(conn, delivery)
    for successor in successors:
        await webhook_chain._abandon_live_successor_attempt(conn, str(successor["id"]))


async def _abandon_success_duplicate_after_unique_violation(
    pool: Any,
    delivery: Any,
    delivery_id: str,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Pre-item-7 raw-asyncpg unique-violation recovery."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await webhook_chain._lock_delivery_chain(conn, delivery)
            finalized = await webhook_chain._abandon_owned_attempt_after_succeeded_chain_peer(
                conn, delivery_id, lease_token, result, require_unexpired_lease=False,
            )
            if finalized is None:
                await webhook_lease._clear_stale_owned_lease_after_terminal_finalize(
                    conn, delivery_id, lease_token,
                )
                return False
            return True


async def _commit_successful_delivery_row(
    pool: Any,
    delivery: Any,
    delivery_id: str,
    lease_token: str,
    result: _DeliveryResult,
) -> bool:
    """Pre-item-7 raw-asyncpg success-commit (alias of
    ``_legacy_finalize_successful_delivery_row``)."""
    return await _legacy_finalize_successful_delivery_row(
        pool, delivery, delivery_id, lease_token, result
    )


async def _persist_response_body_for_audit(
    pool_or_backend: Any,
    *,
    delivery_id: Any,
    response_body: str,
) -> None:
    """Post-finalize audit body capture.

    Production: maps to ``backend.webhooks.store_delivery_response_body``,
    the audit-only UPDATE that the original implementation ran. Test
    back-compat: ``test_webhook_retry_state.py`` injects a raw pool;
    when no backend is installed we run the same UPDATE statement
    against that pool's asyncpg conn.
    """
    backend = _lc._persistence_backend
    if backend is not None:
        try:
            async with backend.transactional() as tx:
                await backend.webhooks.store_delivery_response_body(
                    tx,
                    delivery_id=str(delivery_id),
                    response_body=response_body,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "webhook delivery %s response-body audit update failed after finalization",
                delivery_id,
                exc_info=True,
            )
        return
    pool = pool_or_backend if pool_or_backend is not None else _lc._pool
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE webhook_deliveries
                SET response_body=$2
                WHERE id=$1::uuid
                """,
                str(delivery_id),
                response_body,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "webhook delivery %s response-body audit update failed after finalization",
            delivery_id,
            exc_info=True,
        )


async def _run_post_finalize_delivery_work(
    result: _PostHeaderDeliveryResult,
    *,
    finalized: bool,
) -> None:
    """Capture audit body and close HTTP resources after the DB result is durable."""
    try:
        if finalized:
            response_body = await webhook_sender._capture_response_body_for_audit(
                result.response,
                delivery_id=result.delivery_id,
            )
            if response_body is not None:
                await _persist_response_body_for_audit(
                    None,
                    delivery_id=result.delivery_id,
                    response_body=response_body,
                )
    finally:
        if result.stream_cm is not None:
            await webhook_sender._run_post_header_cleanup(
                result.stream_cm.__aexit__(None, None, None),
                delivery_id=result.delivery_id,
                cleanup_name="stream",
                result=result,
            )
        if result.client_cm is not None:
            await webhook_sender._run_post_header_cleanup(
                result.client_cm.__aexit__(None, None, None),
                delivery_id=result.delivery_id,
                cleanup_name="client",
                result=result,
            )


__all__ = (
    "_finalize_delivery",
    "_persist_response_body_for_audit",
    "_run_post_finalize_delivery_work",
    "_delivery_id_str",
    "_legacy_finalize_delivery_row",
    "_legacy_finalize_successful_delivery_row",
    "_guard_sqlite_succeeded_terminal",
    "_abandon_live_successors_before_success_commit",
    "_abandon_success_duplicate_after_unique_violation",
    "_commit_successful_delivery_row",
)
