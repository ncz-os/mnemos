"""Webhook retry-chain locking and successor management.

After item 7, every per-backend ``WebhookRepository`` implementation
(Postgres / SQLite / MySQL / Oracle / DB2) owns the
``claim_delivery`` / ``guard_delivery_claim`` / ``finalize_delivery``
state machine as a single atomic statement. The ABC methods subsume
the raw-asyncpg helpers that used to live here.

These legacy helpers are kept in place because:

- The ``test_webhook_retry_state.py`` suite still exercises them
  directly while it migrates to the ABC. Keeping the symbols here
  means the test suite keeps compiling; the production runtime never
  reaches them after item 7.

- The Postgres ``_delivery_chain_lock_key`` derivation is mirrored by
  ``mnemos.persistence.postgres._delivery_chain_lock_key`` so in-flight
  leases held by old code and the new ABC mutually exclude each other
  during a rolling deploy.

When the test suite is migrated, this file can be deleted. New code
should call ``backend.webhooks.*`` directly.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional


async def _load_delivery_for_claim(conn: Any, delivery_id: str) -> Optional[Any]:
    """Pre-item-7 raw-asyncpg claim loading."""
    return await conn.fetchrow(
        """
        SELECT d.id, d.subscription_id, d.event_type, d.payload,
               d.payload_hash, d.attempt_num, d.status,
               s.url, s.secret, s.revoked
        FROM webhook_deliveries d
        JOIN webhook_subscriptions s ON s.id = d.subscription_id
        WHERE d.id = $1::uuid
          AND d.scheduled_at <= clock_timestamp()
          AND d.attempt_num <= $2
          AND NOT d.superseded
          AND d.status IN ('pending', 'retrying')
          AND (d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())
        """,
        delivery_id, 4,
    )


async def _insert_successor_delivery(
    conn: Any,
    delivery: Any,
    next_attempt: int,
    scheduled_at: Any,
) -> Optional[Any]:
    """Pre-item-7 successor insert via raw asyncpg.

    The Postgres-specific ``ON CONFLICT … WHERE`` partial index relies
    on a partial unique index (subscription_id, event_type, payload_hash,
    attempt_num) WHERE status IN ('pending', 'retrying') AND NOT superseded.
    Returns the inserted delivery id (uuid) row, or ``None`` when another
    writer already won.
    """
    from .nats_events import publish_delivery_queued, publish_webhook_outbox_insert
    from . import types as webhook_types

    row = await conn.fetchrow(
        """
        INSERT INTO webhook_deliveries
          (subscription_id, event_type, payload, payload_hash,
           attempt_num, status, scheduled_at, writer_revision)
        VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7)
        ON CONFLICT (subscription_id, event_type, payload_hash, attempt_num)
          WHERE status IN ('pending', 'retrying') AND NOT superseded
        DO NOTHING
        RETURNING id
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload"],
        delivery["payload_hash"],
        next_attempt,
        scheduled_at,
        webhook_types.NEW_CODE_WRITER_REVISION,
    )
    if row is not None:
        publish_args = {
            "delivery_id": str(row["id"]),
            "subscription_id": delivery["subscription_id"],
            "event_type": delivery["event_type"],
            "url": _record_value(delivery, "url") or "",
            "payload_hash": delivery["payload_hash"],
            "namespace": _record_value(delivery, "namespace"),
            "owner_id": _record_value(delivery, "owner_id"),
        }
        await publish_delivery_queued(**publish_args)
        await publish_webhook_outbox_insert(**publish_args)
    return row


def _record_value(record: Any, key: str) -> Any:
    try:
        if hasattr(record, "__getitem__") and not isinstance(record, type):
            return record[key]
        return getattr(record, key)
    except (KeyError, AttributeError, TypeError):
        return None


def _is_sqlite_connection(conn: Any) -> bool:
    module = type(conn).__module__
    return module.startswith("sqlite3") or module.startswith("aiosqlite")


async def _lock_delivery_chain(conn: Any, delivery: Any) -> None:
    """Postgres-only transaction-scoped advisory lock per chain.

    SQLite is single-writer at the connection level so no per-chain
    lock is required (the SQLite ABC impl uses BEGIN IMMEDIATE).
    """
    if _is_sqlite_connection(conn):
        return
    await conn.execute("SELECT pg_advisory_xact_lock($1)", _delivery_chain_lock_key(delivery))


def _delivery_chain_lock_key(delivery: Any, _hashlib_mod: Any = None) -> int:
    """Stable signed-int64 advisory-lock key for one webhook retry chain.

    MUST stay byte-for-byte identical to
    :func:`mnemos.persistence.postgres._delivery_chain_lock_key` so
    in-flight leases held by old code and the new ABC code mutually
    exclude each other during a rolling deploy. The Postgres impl
    derives the same int with this algorithm.
    """
    if _hashlib_mod is None:
        _hashlib_mod = hashlib
    digest = _hashlib_mod.sha256(
        (
            "webhook-chain:"
            f"{delivery['subscription_id']}:{delivery['event_type']}:{delivery['payload_hash']}"
        ).encode("utf-8")
    ).digest()[:8]
    key = int.from_bytes(digest, "big", signed=False)
    if key >= 2**63:
        key -= 2**64
    return key


async def _has_successor_attempt(conn: Any, delivery: Any) -> bool:
    """Pre-item-7 raw-asyncpg successor-existence check."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM webhook_deliveries newer
          WHERE newer.subscription_id = $1
            AND newer.event_type = $2
            AND newer.payload_hash = $3
            AND newer.attempt_num > $4
        )
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload_hash"],
        delivery["attempt_num"],
    )


async def _has_live_successor_attempt(conn: Any, delivery: Any) -> bool:
    """Pre-item-7 raw-asyncpg live-successor check."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM webhook_deliveries newer
          WHERE newer.subscription_id = $1
            AND newer.event_type = $2
            AND newer.payload_hash = $3
            AND newer.attempt_num > $4
            AND newer.status IN ('pending', 'retrying')
            AND NOT newer.superseded
        )
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload_hash"],
        delivery["attempt_num"],
    )


async def _has_succeeded_chain_attempt(
    conn: Any,
    delivery: Any,
    delivery_id: str,
) -> bool:
    """Pre-item-7 raw-asyncpg succeeded-peer check."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM webhook_deliveries peer
          WHERE peer.subscription_id = $1
            AND peer.event_type = $2
            AND peer.payload_hash = $3
            AND peer.status = 'succeeded'
            AND peer.id <> $4::uuid
        )
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload_hash"],
        delivery_id,
    )


async def _abandon_owned_attempt_after_live_successor(
    conn: Any,
    delivery_id: str,
    lease_token: str,
) -> Optional[Any]:
    """Pre-item-7 raw-asyncpg abandoned-after-successor update."""
    return await conn.fetchrow(
        """
        UPDATE webhook_deliveries
        SET status='abandoned',
            superseded=TRUE,
            status_updated_at=clock_timestamp(),
            lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND lease_token=$2::uuid
          AND lease_expires_at > clock_timestamp()
          AND status IN ('pending', 'retrying')
          AND NOT superseded
        RETURNING id
        """,
        delivery_id,
        lease_token,
    )


async def _abandon_current_attempt_after_succeeded_chain_peer(
    conn: Any,
    delivery_id: str,
) -> None:
    """Pre-item-7 raw-asyncpg self-abandonment (no lease token)."""
    await conn.execute(
        """
        UPDATE webhook_deliveries
        SET status='abandoned',
            superseded=TRUE,
            status_updated_at=clock_timestamp(),
            lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND status IN ('pending', 'retrying')
          AND NOT superseded
          AND (lease_token IS NULL OR lease_expires_at < clock_timestamp())
        """,
        delivery_id,
    )


async def _abandon_owned_attempt_after_succeeded_chain_peer(
    conn: Any,
    delivery_id: str,
    lease_token: str,
    result: Any,
    *,
    require_unexpired_lease: bool = True,
) -> Optional[Any]:
    """Pre-item-7 raw-asyncpg abandon-after-peer (with result)."""
    if require_unexpired_lease:
        return await conn.fetchrow(
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

    return await conn.fetchrow(
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


async def _find_live_unleased_successor_attempts(
    conn: Any,
    delivery: Any,
) -> list[Any]:
    """Pre-item-7 raw-asyncpg free-successor peeker."""
    return await conn.fetch(
        """
        SELECT newer.id
        FROM webhook_deliveries newer
        WHERE newer.subscription_id = $1
          AND newer.event_type = $2
          AND newer.payload_hash = $3
          AND newer.attempt_num > $4
          AND newer.status IN ('pending', 'retrying')
          AND NOT newer.superseded
          AND (newer.lease_token IS NULL OR newer.lease_expires_at < clock_timestamp())
        ORDER BY newer.attempt_num ASC
        """,
        delivery["subscription_id"],
        delivery["event_type"],
        delivery["payload_hash"],
        delivery["attempt_num"],
    )


async def _abandon_live_successor_attempt(conn: Any, successor_id: str) -> None:
    """Pre-item-7 raw-asyncpg successor abandonment."""
    await conn.execute(
        """
        UPDATE webhook_deliveries
        SET status='abandoned',
            superseded=TRUE,
            status_updated_at=clock_timestamp(),
            lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND status IN ('pending', 'retrying')
          AND NOT superseded
          AND (lease_token IS NULL OR lease_expires_at < clock_timestamp())
        """,
        successor_id,
    )


__all__ = (
    "_load_delivery_for_claim",
    "_insert_successor_delivery",
    "_record_value",
    "_is_sqlite_connection",
    "_lock_delivery_chain",
    "_delivery_chain_lock_key",
    "_has_successor_attempt",
    "_has_live_successor_attempt",
    "_has_succeeded_chain_attempt",
    "_abandon_owned_attempt_after_live_successor",
    "_abandon_current_attempt_after_succeeded_chain_peer",
    "_abandon_owned_attempt_after_succeeded_chain_peer",
    "_find_live_unleased_successor_attempts",
    "_abandon_live_successor_attempt",
)
