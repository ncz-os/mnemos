"""Webhook delivery lease acquisition, validation, and release (item 7).

The pre-item-7 lease module ran a hand-rolled ``UPDATE ... RETURNING``
plus multiple ``webhook_chain.*`` follow-up reads against raw asyncpg
rows. Item 7 folds the equivalent state machine into
``backend.webhooks.claim_delivery`` (cold path) and
``backend.webhooks.guard_delivery_claim`` (warm path), so this module
keeps:

1. ``_claim_remaining_send_window_seconds`` and ``_as_aware_utc`` —
   pure datetime math, untouched by the migration.
2. ``_ClaimedDelivery`` and the ``_RecordView`` adapter — they translate
   the ABC's ``WebhookDeliveryRecord`` into the subscript-style access
   the sender's HTTP pipeline still uses (``delivery["url"]``,
   ``delivery["payload"]``, etc).
3. ``_claim_delivery`` / ``_guard_preclaimed_delivery_before_send``
   that pick between the ABC route (production) and a pre-item-7 raw
   ``asyncpg`` route (test back-compat while ``test_webhook_retry_state.py``
   migrates to the ABC).

All raw asyncpg and chain.py helpers used by the legacy lease path are
re-exported at the bottom of this file so the test suite keeps
importing them. The migrated runtime does not enter the legacy code.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from mnemos.core import lifecycle as _lc  # noqa: WPS433

from . import types as webhook_types

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ClaimedDelivery:
    """One claimed delivery row plus its lease token.

    Item 7: ``delivery`` is no longer an asyncpg ``Record`` — it is
    whatever the persistence backend's claim method returned. The
    sender reads it through the ``lease._RecordView`` adapter
    (subscript-style) so the existing HTTP/DNS code keeps working
    without raw DBAPI handles anywhere in the production runtime.
    """

    delivery: Any
    lease_token: str
    pre_claim_monotonic: float


def _as_mapping(record: Any) -> Mapping[str, Any]:
    """Backend-neutral view of a claimed delivery record."""
    if isinstance(record, Mapping):
        return record

    class _RecordMapping(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            try:
                return getattr(record, key)
            except AttributeError as exc:
                raise KeyError(key) from exc

        def __iter__(self):  # type: ignore[no-untyped-def]
            for field in (
                "id",
                "subscription_id",
                "event_type",
                "payload",
                "payload_hash",
                "attempt_num",
                "status",
                "response_status",
                "response_body",
                "error",
                "scheduled_at",
                "delivered_at",
                "created",
                "status_updated_at",
                "superseded",
                "lease_token",
                "lease_expires_at",
                "writer_revision",
                "url",
                "secret",
                "revoked",
                "owner_id",
                "namespace",
            ):
                yield field

        def __len__(self) -> int:
            return 22

    return _RecordMapping()


class _RecordView:
    """Subscript-style proxy over ``WebhookDeliveryRecord`` (and any
    raw asyncpg-Record shape during the test migration window).
    """

    __slots__ = ("_record",)

    def __init__(self, record: Any) -> None:
        self._record = record

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self._record, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self._record, key)

    def __iter__(self):
        return iter(_as_mapping(self._record))

    def __len__(self) -> int:
        return len(_as_mapping(self._record))


async def _claim_delivery(
    pool_or_backend: Any,
    delivery_id: str,
    *,
    lease_token: str,
    lease_seconds: int | None = None,
) -> Optional[_ClaimedDelivery]:
    """Acquire a lease for one due delivery row.

    Production path: routes through
    :meth:`backend.webhooks.claim_delivery` (Postgres / SQLite / MySQL /
    Oracle / DB2 ABC).

    Test back-compat: when no persistence backend is installed but the
    legacy ``asyncpg.Pool`` singleton is set on lifecycle, run the
    pre-item-7 hand-rolled ``UPDATE ... RETURNING`` claim — this is
    what the ``test_webhook_retry_state.py`` suite relies on while it
    migrates to the ABC over the next item or two.
    """
    if lease_seconds is None:
        lease_seconds = webhook_types.WEBHOOK_LEASE_SECONDS

    backend = _lc._persistence_backend
    if backend is not None:
        pre_claim_monotonic = time.monotonic()
        async with backend.transactional() as tx:
            claim = await backend.webhooks.claim_delivery(
                tx,
                delivery_id=delivery_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
                max_attempts=webhook_types.MAX_ATTEMPTS,
                writer_revision=webhook_types.NEW_CODE_WRITER_REVISION,
            )
        if claim is None:
            return None
        record_view = _RecordView(claim.delivery)
        record_view.lease_expires_at = claim.lease_expires_at  # type: ignore[attr-defined]
        record_view.claim_db_now = claim.claim_db_now  # type: ignore[attr-defined]
        return _ClaimedDelivery(
            delivery=record_view,
            lease_token=lease_token,
            pre_claim_monotonic=pre_claim_monotonic,
        )

    pool = pool_or_backend or _lc._pool
    if pool is None:
        raise RuntimeError(
            f"webhook delivery {delivery_id} cannot run without a supported persistence handle"
        )
    return await _legacy_claim_delivery(pool, delivery_id, lease_token=lease_token, lease_seconds=lease_seconds)


async def _guard_preclaimed_delivery_before_send(
    pool_or_backend: Any,
    delivery: Any,
    lease_token: str,
) -> bool:
    """Fence a recovery-preclaimed attempt.

    Production: ``backend.webhooks.guard_delivery_claim``. Test back-compat:
    legacy pre-item-7 chain path.
    """
    delivery_id = str(delivery["id"]) if hasattr(delivery, "__getitem__") else str(delivery.id)
    backend = _lc._persistence_backend
    if backend is not None:
        async with backend.transactional() as tx:
            return bool(
                await backend.webhooks.guard_delivery_claim(
                    tx,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                )
            )
    pool = pool_or_backend or _lc._pool
    if pool is None:
        raise RuntimeError(
            f"webhook delivery {delivery_id} cannot run without a supported persistence handle"
        )
    return await _legacy_guard_preclaimed_delivery_before_send(pool, delivery, lease_token)


# --- legacy raw-asyncpg helpers used by test_webhook_retry_state.py --------------
# These mirror the pre-item-7 chain.sqlite-style helpers so the legacy
# test suite keeps passing. They are NOT touched by the production
# runtime; they only fire when the legacy `_pool` is set on lifecycle
# without a corresponding persistence backend. Safe to delete once the
# test suite migrates to the ABC (next item).


async def _legacy_claim_delivery(
    pool: Any,
    delivery_id: str,
    *,
    lease_token: str,
    lease_seconds: int,
) -> Optional[_ClaimedDelivery]:
    """Pre-item-7 ``asyncpg`` claim."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            delivery = await _load_delivery_for_claim(conn, delivery_id)
            if not delivery:
                return None
            await _lock_delivery_chain(conn, delivery)
            if await _has_succeeded_chain_attempt(conn, delivery, delivery_id):
                await _abandon_current_attempt_after_succeeded_chain_peer(conn, delivery_id)
                return None
            if delivery["status"] == "retrying" and await _has_successor_attempt(conn, delivery):
                await conn.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status='abandoned',
                        superseded=TRUE,
                        lease_token=NULL,
                        lease_expires_at=NULL
                    WHERE id=$1::uuid AND status='retrying' AND NOT superseded
                    """,
                    delivery_id,
                )
                return None

            pre_claim_monotonic = time.monotonic()
            claimed = await conn.fetchrow(
                """
                UPDATE webhook_deliveries d
                SET lease_token=$2::uuid,
                    lease_expires_at=claim_clock.claim_now + ($3::int * INTERVAL '1 second'),
                    status=CASE WHEN d.status = 'pending' THEN 'retrying' ELSE d.status END
                FROM webhook_subscriptions s,
                     (SELECT clock_timestamp() AS claim_now) claim_clock
                WHERE s.id = d.subscription_id
                  AND d.id=$1::uuid
                  AND d.scheduled_at <= claim_clock.claim_now
                  AND d.attempt_num <= $4
                  AND NOT d.superseded
                  AND d.status IN ('pending', 'retrying')
                  AND (d.lease_token IS NULL OR d.lease_expires_at < claim_clock.claim_now)
                  AND d.writer_revision = $5
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
                RETURNING d.id, d.subscription_id, d.event_type, d.payload,
                          d.payload_hash, d.attempt_num, d.status,
                          d.lease_expires_at, claim_clock.claim_now AS claim_db_now,
                          s.url, s.secret, s.revoked, s.owner_id, s.namespace
                """,
                delivery_id,
                lease_token,
                lease_seconds,
                webhook_types.MAX_ATTEMPTS,
                webhook_types.NEW_CODE_WRITER_REVISION,
            )
            if claimed is None:
                return None
            return _ClaimedDelivery(
                delivery=claimed,
                lease_token=lease_token,
                pre_claim_monotonic=pre_claim_monotonic,
            )


async def _legacy_guard_preclaimed_delivery_before_send(
    pool: Any,
    delivery: Any,
    lease_token: str,
) -> bool:
    """Pre-item-7 ``asyncpg`` warm-path guard."""
    delivery_id = str(delivery["id"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_delivery_chain(conn, delivery)
            if not await _preclaimed_delivery_is_live_and_owned(conn, delivery_id, lease_token):
                await _release_owned_lease_for_reclaim(conn, delivery_id, lease_token)
                return False
            if await _has_succeeded_chain_attempt(conn, delivery, delivery_id):
                await _abandon_owned_attempt_after_succeeded_chain_peer(
                    conn,
                    delivery_id,
                    lease_token,
                    webhook_types._DeliveryResult(
                        succeeded=False,
                        error="succeeded-chain-peer-before-send",
                    ),
                )
                return False
            if await _has_live_successor_attempt(conn, delivery):
                await _abandon_owned_attempt_after_live_successor(
                    conn,
                    delivery_id,
                    lease_token,
                )
                return False
            return True


async def _load_delivery_for_claim(conn: Any, delivery_id: str) -> Optional[Any]:
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
        delivery_id, webhook_types.MAX_ATTEMPTS,
    )


async def _lock_delivery_chain(conn: Any, delivery: Any) -> None:
    """Stable signed-int64 advisory lock per chain (Postgres)."""
    if _is_sqlite_connection(conn):
        return
    await conn.execute("SELECT pg_advisory_xact_lock($1)", _delivery_chain_lock_key(delivery))


def _delivery_chain_lock_key(delivery: Any) -> int:
    """Mirror of PostgresWebhookRepository._delivery_chain_lock_key.

    The Postgres backend's ABC impl derives the SAME int with this
    algorithm so in-flight leases held by old code and the new code
    mutually exclude each other during a rolling deploy.
    """
    digest = hashlib.sha256(
        (
            "webhook-chain:"
            f"{delivery['subscription_id']}:{delivery['event_type']}:{delivery['payload_hash']}"
        ).encode("utf-8")
    ).digest()[:8]
    key = int.from_bytes(digest, "big", signed=False)
    if key >= 2**63:
        key -= 2**64
    return key


def _is_sqlite_connection(conn: Any) -> bool:
    module = type(conn).__module__
    return module.startswith("sqlite3") or module.startswith("aiosqlite")


async def _has_successor_attempt(conn: Any, delivery: Any) -> bool:
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
    conn: Any, delivery: Any, delivery_id: str
) -> bool:
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
    conn: Any, delivery_id: str, lease_token: str
) -> Optional[Any]:
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
    conn: Any, delivery_id: str
) -> None:
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


async def _preclaimed_delivery_is_live_and_owned(
    conn: Any, delivery_id: str, lease_token: str
) -> bool:
    return await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM webhook_deliveries
          WHERE id=$1::uuid
            AND lease_token=$2::uuid
            AND lease_expires_at > clock_timestamp()
            AND status IN ('pending', 'retrying')
            AND NOT superseded
        )
        """,
        delivery_id,
        lease_token,
    )


async def _release_owned_lease_for_reclaim(
    conn: Any, delivery_id: str, lease_token: str
) -> bool:
    released = await conn.fetchrow(
        """
        UPDATE webhook_deliveries
        SET lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND lease_token=$2::uuid
        RETURNING id, status, superseded
        """,
        delivery_id,
        lease_token,
    )
    return (
        released is not None
        and released["status"] in webhook_types.LIVE_DELIVERY_STATUSES
        and not released["superseded"]
    )


def _claim_remaining_send_window_seconds(
    delivery: Any,
    *,
    pre_claim_monotonic: float,
) -> float:
    lease_expires_at = _coerce_datetime(_read_field(delivery, "lease_expires_at"))
    claim_db_now = _coerce_datetime(_read_field(delivery, "claim_db_now"))
    db_claim_window = (lease_expires_at - claim_db_now).total_seconds()
    now_monotonic = time.monotonic()
    elapsed_since_pre_claim = max(0.0, now_monotonic - pre_claim_monotonic)
    return db_claim_window - elapsed_since_pre_claim - webhook_types.WEBHOOK_FINALIZE_BUFFER_SECONDS


def _read_field(record: Any, name: str) -> Any:
    if hasattr(record, "__getitem__") and not isinstance(record, type):
        try:
            value = record[name]
        except (KeyError, TypeError):
            value = getattr(record, name, None)
        if value is not None:
            return value
    return getattr(record, name, None)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _as_aware_utc(value)
    raise TypeError(f"expected datetime, got {type(value).__name__}")


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize DB timestamp values for arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record_value(record: Any, key: str) -> Any:
    try:
        if hasattr(record, "__getitem__") and not isinstance(record, type):
            return record[key]
        return getattr(record, key)
    except (KeyError, AttributeError, TypeError):
        return None


async def _clear_stale_owned_lease_after_terminal_finalize(
    conn: Any, delivery_id: str, lease_token: str
) -> None:
    """Pre-item-7 stale-lease release used in the success-commit
    log-and-clear fallback path when the row no longer matches the
    current writer.
    """
    cleared = await conn.fetchrow(
        """
        UPDATE webhook_deliveries
        SET lease_token=NULL,
            lease_expires_at=NULL
        WHERE id=$1::uuid
          AND lease_token=$2::uuid
        RETURNING id, status, superseded
        """,
        delivery_id,
        lease_token,
    )
    if cleared is None:
        logger.warning(
            "webhook delivery %s success finalize found no live owned row; stale lease was already gone",
            delivery_id,
        )
        return
    if cleared["status"] not in webhook_types.LIVE_DELIVERY_STATUSES or cleared["superseded"]:
        logger.warning(
            "webhook delivery %s was already terminal at success finalize time; stale lease cleared",
            delivery_id,
        )
        return
    logger.warning(
        "webhook delivery %s success finalize found no live owned row; stale lease cleared",
        delivery_id,
    )


__all__ = (
    "_claim_delivery",
    "_guard_preclaimed_delivery_before_send",
    "_load_delivery_for_claim",
    "_lock_delivery_chain",
    "_delivery_chain_lock_key",
    "_has_successor_attempt",
    "_has_live_successor_attempt",
    "_has_succeeded_chain_attempt",
    "_abandon_owned_attempt_after_live_successor",
    "_abandon_current_attempt_after_succeeded_chain_peer",
    "_abandon_owned_attempt_after_succeeded_chain_peer",
    "_release_owned_lease_for_reclaim",
    "_claim_remaining_send_window_seconds",
    "_as_aware_utc",
    "_ClaimedDelivery",
    "_RecordView",
    "_preclaimed_delivery_is_live_and_owned",
    "_record_value",
)
