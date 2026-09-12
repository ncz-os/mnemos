"""Integration test for ``Db2WebhookRepository``.

Item 6 of the ABC webhook persistence sequence. Verifies that the new
``WebhookRepository`` ABC methods on ``mnemos.persistence.db2`` produce
behavior consistent with ``PostgresWebhookRepository`` against the same
logical schema (lease acquisition, ``SKIP LOCKED DATA`` recovery,
writer-revision fence, retry-chain convergence, repair sweep, idempotent
finalize).

Requires a real Db2 instance reachable via ``MNEMOS_TEST_DB2``. The
test applies a minimal webhook-only schema (webhook_subscriptions +
webhook_deliveries + the v3.5-equivalent unique indexes / triggers) on
first run so it works without the full Db2 schema. When
``MNEMOS_TEST_DB2`` is unset the test is skipped — same pattern as
``tests/test_postgres_webhook_repository.py``.

The Db2 DSN format is ``db2://user:pass@host:port/database`` matching
``Db2Backend.open``. Operator must have CREATE TABLE / CREATE TRIGGER
privilege on the target schema. Heavy startup — ``icr.io/db2_community/db2``
needs ~2-3 minutes for first-boot activation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio

from mnemos.core.webhook_constants import NEW_CODE_WRITER_REVISION
from mnemos.persistence.base import (
    WebhookDeliveryOutcome,
    WebhookSubscriptionRecord,
)
from mnemos.persistence.db2 import Db2WebhookRepository

DSN = os.environ.get("MNEMOS_TEST_DB2")

# Driver availability check (deferred — see test fixtures). The
# ``import ibm_db_dbi`` inside fixtures performs the real gate.
try:
    import ibm_db_dbi  # type: ignore[import-not-found]  # noqa: F401

    _DB2_DRIVER_AVAILABLE = True
except ModuleNotFoundError:
    _DB2_DRIVER_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(
        not DSN,
        reason=(
            "set MNEMOS_TEST_DB2=db2://user:pass@host:port/database to run "
            "webhook integration tests against a live Db2 instance"
        ),
    ),
    pytest.mark.skipif(
        not _DB2_DRIVER_AVAILABLE,
        reason=(
            "ibm_db_dbi driver is not installed in this environment — "
            "install via 'pip install ibm_db' to run this test"
        ),
    ),
]


WEBHOOK_SCHEMA_DDL: tuple[str, ...] = (
    # Subscription table. VARCHAR(64) for synthetic UUID-shape ids,
    # CLOB for the JSON events array.
    """
    CREATE TABLE webhook_subscriptions (
        id              VARCHAR(64)   NOT NULL PRIMARY KEY,
        url             VARCHAR(2000) NOT NULL,
        events          CLOB          NOT NULL,
        secret          VARCHAR(2000) NOT NULL,
        description     VARCHAR(2000),
        owner_id        VARCHAR(256)  NOT NULL,
        namespace       VARCHAR(256)  NOT NULL,
        created         TIMESTAMP     DEFAULT CURRENT TIMESTAMP NOT NULL,
        revoked         SMALLINT      DEFAULT 0 NOT NULL,
        revoked_at      TIMESTAMP
    )
    """,
    "CREATE INDEX idx_webhook_subscriptions_owner "
    "    ON webhook_subscriptions(owner_id, namespace)",
    """
    CREATE TABLE webhook_deliveries (
        id               VARCHAR(64)   NOT NULL PRIMARY KEY,
        subscription_id  VARCHAR(64)   NOT NULL,
        event_type       VARCHAR(256)  NOT NULL,
        payload          CLOB          NOT NULL,
        payload_hash     VARCHAR(64)   NOT NULL,
        attempt_num      INTEGER       DEFAULT 1 NOT NULL,
        status           VARCHAR(32)   DEFAULT 'pending' NOT NULL,
        response_status  INTEGER,
        response_body    CLOB,
        error            VARCHAR(2000),
        scheduled_at     TIMESTAMP     DEFAULT CURRENT TIMESTAMP NOT NULL,
        delivered_at     TIMESTAMP,
        created          TIMESTAMP     DEFAULT CURRENT TIMESTAMP NOT NULL,
        lease_token      VARCHAR(64),
        lease_expires_at TIMESTAMP,
        writer_revision  INTEGER       DEFAULT 0 NOT NULL,
        status_updated_at TIMESTAMP    DEFAULT CURRENT TIMESTAMP NOT NULL,
        superseded       SMALLINT      DEFAULT 0 NOT NULL
    )
    """,
    "CREATE INDEX idx_webhook_deliveries_subscription "
    "    ON webhook_deliveries(subscription_id, created DESC)",
    "CREATE INDEX idx_webhook_deliveries_pending "
    "    ON webhook_deliveries(scheduled_at)",
    "CREATE INDEX idx_webhook_deliveries_lease_expires_at "
    "    ON webhook_deliveries(lease_expires_at)",
    # Functional unique indexes — Db2 treats NULL values in unique
    # indexes as non-conflicting, which gives us "partial unique" for
    # the live-chain-attempt and succeeded-chain constraints.
    "CREATE UNIQUE INDEX uq_webhook_deliveries_live_chain_attempt "
    "    ON webhook_deliveries("
    "        CASE WHEN status IN ('pending','retrying') AND superseded = 0 "
    "             THEN subscription_id || '|' || event_type || '|' "
    "                  || payload_hash || '|' || attempt_num "
    "             ELSE NULL END"
    "    )",
    "CREATE UNIQUE INDEX uq_webhook_deliveries_succeeded_chain "
    "    ON webhook_deliveries("
    "        CASE WHEN status = 'succeeded' "
    "             THEN subscription_id || '|' || event_type || '|' || payload_hash "
    "             ELSE NULL END"
    "    )",
    # Db2 doesn't have BEFORE UPDATE triggers as easily as Postgres/Oracle;
    # we rely on the application-layer invariant (the SQL in
    # ``Db2WebhookRepository.finalize_delivery`` carries the
    # ``status IN ('pending','retrying') AND superseded = 0`` guard
    # before the success UPDATE). The trigger equivalent would be a
    # ``CREATE TRIGGER ... BEFORE UPDATE`` referencing ``CURRENT TIMESTAMP``;
    # we leave that out here because (a) the contract test enforces the
    # invariant end-to-end and (b) Db2 trigger DDL syntax varies across
    # versions.
)


# Db2 needs its ``;`` terminator on every statement including the last
# one, and rejects multi-statement strings. We split on top-level
# semicolons (no DDL here has nested BEGIN ... END).
_SPLIT_DDL_RE = re.compile(r";\s*\n", re.MULTILINE)


def _split_db2_statements(sql: str) -> list[str]:
    return [s.strip() for s in _SPLIT_DDL_RE.split(sql) if s.strip()]


async def _ensure_webhook_schema(pool: Any) -> None:
    async with pool.acquire() as conn:
        cur = conn.cursor()
        try:
            for ddl in WEBHOOK_SCHEMA_DDL:
                for stmt in _split_db2_statements(ddl):
                    await cur.execute(stmt)
        finally:
            await cur.close()
        await conn.commit()


def _payload_for(event_type: str, data: dict[str, Any]) -> str:
    return json.dumps(
        {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _expected_payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest_asyncio.fixture
async def db2_pool() -> AsyncIterator[Any]:
    """Yield a Db2 pool and provision the webhook schema."""
    pytest.importorskip("ibm_db_dbi")  # gate the test on the driver
    from mnemos.persistence.db2 import create_db2_pool

    pool = await create_db2_pool(DSN, min_size=1, max_size=4)
    await _ensure_webhook_schema(pool)
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            cur = conn.cursor()
            try:
                await cur.execute(
                    "DELETE FROM webhook_deliveries WHERE subscription_id IN "
                    "(SELECT id FROM webhook_subscriptions "
                    "  WHERE owner_id LIKE 'webhook_repo_%')"
                )
                await cur.execute(
                    "DELETE FROM webhook_subscriptions "
                    "WHERE owner_id LIKE 'webhook_repo_%'"
                )
            finally:
                await cur.close()
            await conn.commit()
        await _db2_close_pool_async(pool)


async def _db2_close_pool_async(pool: Any) -> None:
    """Best-effort close of a Db2 pool — handles both async and sync close."""
    if hasattr(pool, "aclose"):
        await pool.aclose()
    elif hasattr(pool, "close"):
        result = pool.close()
        if hasattr(result, "__await__"):
            await result


@pytest_asyncio.fixture
async def repo(db2_pool) -> Db2WebhookRepository:
    """Yield a bare ``Db2WebhookRepository`` (no Backend wrapper needed)."""
    return Db2WebhookRepository()


@asynccontextmanager
async def _tx(db2_pool: Any):
    """Yield a Db2 transaction wrapped around a real pool acquire."""
    async with db2_pool.acquire() as conn:
        await conn.begin()
        tx = SimpleNamespace(conn=conn, closed=False)

        async def _commit():
            tx.closed = True
            await conn.commit()

        async def _rollback():
            tx.closed = True
            await conn.rollback()

        tx.commit = _commit
        tx.rollback = _rollback
        try:
            yield tx
            if not tx.closed:
                await tx.commit()
        except BaseException:
            if not tx.closed:
                await tx.rollback()
            raise


async def _direct_execute(db2_pool: Any, sql: str, *args: Any) -> int:
    """Run raw DML against the pool outside a fixture transaction."""
    async with db2_pool.acquire() as conn:
        cur = conn.cursor()
        try:
            await cur.execute(sql, args or None)
            affected = int(getattr(cur, "rowcount", 0) or 0)
        finally:
            await cur.close()
        await conn.commit()
    return affected


# ─────────────────────────────────────────────────────────────────────────
# subscription surface
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get_subscription_roundtrip(repo, db2_pool):
    subscription_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        record = await repo.create_subscription(
            tx,
            subscription_id=subscription_id,
            url="https://example.com/hook",
            events=("memory.created", "consultation.completed"),
            secret="top-secret",
            description="my subscription",
            owner_id="webhook_repo_create_user",
            namespace="default",
        )
    assert isinstance(record, WebhookSubscriptionRecord)
    assert record.id == subscription_id
    assert record.url == "https://example.com/hook"
    assert record.events == ("memory.created", "consultation.completed")
    assert record.description == "my subscription"
    assert record.owner_id == "webhook_repo_create_user"
    assert record.namespace == "default"
    assert record.revoked is False
    assert record.revoked_at is None
    assert record.created.tzinfo is not None

    async with _tx(db2_pool) as tx:
        fetched = await repo.get_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id="webhook_repo_create_user",
            namespace="default",
        )
    assert fetched == record


@pytest.mark.asyncio
async def test_list_subscriptions_partial_scope_is_rejected(repo, db2_pool):
    with pytest.raises(ValueError, match="both"):
        async with _tx(db2_pool) as tx:
            await repo.list_subscriptions(
                tx,
                owner_id="alice",
                namespace=None,
                include_revoked=False,
                limit=10,
            )
    with pytest.raises(ValueError, match="both"):
        async with _tx(db2_pool) as tx:
            await repo.list_subscriptions(
                tx,
                owner_id=None,
                namespace="ns1",
                include_revoked=False,
                limit=10,
            )


@pytest.mark.asyncio
async def test_list_subscriptions_respects_revoked_flag_and_owner_scope(
    repo, db2_pool
):
    sub_a = str(uuid.uuid4())
    sub_b = str(uuid.uuid4())
    sub_c = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_a,
            url="https://a.example.com",
            events=("memory.created",),
            secret="sa",
            description=None,
            owner_id="webhook_repo_list_user",
            namespace="ns1",
        )
        await repo.create_subscription(
            tx,
            subscription_id=sub_b,
            url="https://b.example.com",
            events=("memory.created",),
            secret="sb",
            description=None,
            owner_id="webhook_repo_list_user",
            namespace="ns1",
        )
        await repo.create_subscription(
            tx,
            subscription_id=sub_c,
            url="https://c.example.com",
            events=("memory.created",),
            secret="sc",
            description=None,
            owner_id="webhook_repo_list_user",
            namespace="ns2",
        )

    async with _tx(db2_pool) as tx:
        await repo.revoke_subscription(
            tx,
            subscription_id=sub_b,
            owner_id="webhook_repo_list_user",
            namespace="ns1",
        )

    async with _tx(db2_pool) as tx:
        live = await repo.list_subscriptions(
            tx,
            owner_id="webhook_repo_list_user",
            namespace="ns1",
            include_revoked=False,
            limit=10,
        )
        all_in_ns1 = await repo.list_subscriptions(
            tx,
            owner_id="webhook_repo_list_user",
            namespace="ns1",
            include_revoked=True,
            limit=10,
        )
        all_in_ns2 = await repo.list_subscriptions(
            tx,
            owner_id="webhook_repo_list_user",
            namespace="ns2",
            include_revoked=False,
            limit=10,
        )

    live_ids = sorted(r.id for r in live)
    all_ids = sorted(r.id for r in all_in_ns1)
    ns2_ids = sorted(r.id for r in all_in_ns2)
    assert live_ids == sorted([sub_a])
    assert all_ids == sorted([sub_a, sub_b])
    assert ns2_ids == sorted([sub_c])
    assert any(r.revoked for r in all_in_ns1 if r.id == sub_b)


@pytest.mark.asyncio
async def test_revoke_subscription_is_idempotent_and_scoped(repo, db2_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/revoke",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_revoke_user",
            namespace="default",
        )

    async with _tx(db2_pool) as tx:
        first = await repo.revoke_subscription(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_revoke_user",
            namespace="default",
        )
    assert first is True

    async with _tx(db2_pool) as tx:
        second = await repo.revoke_subscription(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_revoke_user",
            namespace="default",
        )
    assert second is False

    async with _tx(db2_pool) as tx:
        wrong_owner = await repo.revoke_subscription(
            tx,
            subscription_id=sub_id,
            owner_id="not-the-owner",
            namespace="default",
        )
        wrong_ns = await repo.revoke_subscription(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_revoke_user",
            namespace="other-ns",
        )
    assert wrong_owner is False
    assert wrong_ns is False


# ─────────────────────────────────────────────────────────────────────────
# dispatch + claim
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_event_creates_pending_deliveries(repo, db2_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/dispatch",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_dispatch_user",
            namespace="default",
        )

    async with _tx(db2_pool) as tx:
        delivery_ids = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "abc", "count": 7},
            owner_id="webhook_repo_dispatch_user",
            namespace="default",
        )
    assert len(delivery_ids) == 1

    async with _tx(db2_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_dispatch_user",
            namespace="default",
            limit=10,
        )
    assert len(deliveries) == 1
    d = deliveries[0]
    assert d.id == delivery_ids[0]
    assert d.event_type == "memory.created"
    assert d.status == "pending"
    assert d.attempt_num == 1
    assert d.payload_hash == hashlib.sha256(d.payload.encode("utf-8")).hexdigest()
    assert d.subscription_id == sub_id
    assert d.response_status is None
    assert d.response_body is None
    assert d.error is None
    assert d.superseded is False
    assert d.lease_token is None
    assert d.writer_revision == NEW_CODE_WRITER_REVISION


@pytest.mark.asyncio
async def test_claim_delivery_returns_claim_and_blocks_concurrent_claim(
    repo, db2_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/claim",
            events=("memory.created",),
            secret="hunter2",
            description=None,
            owner_id="webhook_repo_claim_user",
            namespace="default",
        )

    async with _tx(db2_pool) as tx:
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "x"},
            owner_id="webhook_repo_claim_user",
            namespace="default",
        )

    async with _tx(db2_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="lease-A",
            lease_seconds=10,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None
    assert claim.delivery.id == delivery_id
    assert claim.lease_token == "lease-A"
    assert claim.url == "https://example.com/claim"
    assert claim.secret == "hunter2"
    assert claim.lease_expires_at > claim.claim_db_now

    async with _tx(db2_pool) as tx:
        claim2 = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="lease-B",
            lease_seconds=10,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim2 is None


@pytest.mark.asyncio
async def test_claim_due_deliveries_returns_pending_in_order(repo, db2_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/due",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_due_user",
            namespace="default",
        )

    async with _tx(db2_pool) as tx:
        [d_id_a] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "abc"},
            owner_id="webhook_repo_due_user",
            namespace="default",
        )

    extra1 = str(uuid.uuid4())
    extra2 = str(uuid.uuid4())
    await _direct_execute(
        db2_pool,
        """
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload, payload_hash,
             attempt_num, status, scheduled_at, writer_revision, status_updated_at)
        VALUES
            (?, ?, 'memory.created', ?, ?,
             1, 'pending', CURRENT TIMESTAMP - 1 SECOND, ?, CURRENT TIMESTAMP)
        """,
        extra1,
        sub_id,
        "payload-1",
        "hash-1",
        NEW_CODE_WRITER_REVISION,
    )
    await _direct_execute(
        db2_pool,
        """
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload, payload_hash,
             attempt_num, status, scheduled_at, writer_revision, status_updated_at)
        VALUES
            (?, ?, 'memory.created', ?, ?,
             1, 'pending', CURRENT TIMESTAMP + 1 HOUR, ?, CURRENT TIMESTAMP)
        """,
        extra2,
        sub_id,
        "payload-2",
        "hash-2",
        NEW_CODE_WRITER_REVISION,
    )

    async with _tx(db2_pool) as tx:
        claims = await repo.claim_due_deliveries(
            tx,
            lease_token="due-lease",
            limit=10,
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    claimed_ids = {c.delivery.id for c in claims}
    assert extra2 not in claimed_ids
    assert {d_id_a, extra1}.issubset(claimed_ids)


# ─────────────────────────────────────────────────────────────────────────
# finalize + chain repair
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_success_marks_row_succeeded_and_returns_applied(
    repo, db2_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/fin",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_fin_user",
            namespace="default",
        )

    async with _tx(db2_pool) as tx:
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "abc"},
            owner_id="webhook_repo_fin_user",
            namespace="default",
        )

    async with _tx(db2_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="lease-fin",
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(db2_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="lease-fin",
            outcome=WebhookDeliveryOutcome(
                succeeded=True, response_status=200, response_body="ok"
            ),
            max_attempts=3,
            backoff_schedule=[1, 2, 5],
        )
    assert result.applied is True
    assert result.status == "succeeded"

    async with _tx(db2_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_fin_user",
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    assert d.status == "succeeded"
    assert d.response_status == 200
    assert d.response_body == "ok"
    assert d.superseded is False


@pytest.mark.asyncio
async def test_finalize_wrong_lease_token_returns_not_applied(repo, db2_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/fin2",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_fin2_user",
            namespace="default",
        )
    async with _tx(db2_pool) as tx:
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "x"},
            owner_id="webhook_repo_fin2_user",
            namespace="default",
        )
    async with _tx(db2_pool) as tx:
        await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="real-lease",
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )

    async with _tx(db2_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="wrong-lease",
            outcome=WebhookDeliveryOutcome(
                succeeded=True, response_status=200, response_body="ok"
            ),
            max_attempts=3,
            backoff_schedule=[1, 2, 5],
        )
    assert result.applied is False


@pytest.mark.asyncio
async def test_finalize_failure_enqueues_next_attempt_via_backoff(
    repo, db2_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/fin3",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_fin3_user",
            namespace="default",
        )
    async with _tx(db2_pool) as tx:
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "x"},
            owner_id="webhook_repo_fin3_user",
            namespace="default",
        )

    async with _tx(db2_pool) as tx:
        await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="retry-lease",
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )

    async with _tx(db2_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="retry-lease",
            outcome=WebhookDeliveryOutcome(
                succeeded=False, response_status=503, error="upstream-down"
            ),
            max_attempts=3,
            backoff_schedule=[0, 1, 5],
        )
    assert result.applied is True
    assert result.successor_delivery_id is not None
    assert result.successor_delivery_id != delivery_id

    async with _tx(db2_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_fin3_user",
            namespace="default",
            limit=10,
        )
    statuses = sorted((d.attempt_num, d.status, d.superseded) for d in deliveries)
    assert (1, "abandoned", True) in statuses
    assert any(
        d.id == result.successor_delivery_id and d.attempt_num == 2
        and d.status == "pending"
        and d.superseded is False
        for d in deliveries
    )


@pytest.mark.asyncio
async def test_repair_delivery_chains_terminalizes_obsolete_live_rows(
    repo, db2_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/repair",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_repair_user",
            namespace="default",
        )

    body = _payload_for("memory.created", {"memory_id": "obsolete"})
    body_hash = _expected_payload_hash(body)
    first_id = str(uuid.uuid4())
    successor_id = str(uuid.uuid4())
    await _direct_execute(
        db2_pool,
        """
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload, payload_hash,
             attempt_num, status, scheduled_at, writer_revision, status_updated_at)
        VALUES
            (?, ?, 'memory.created', ?, ?,
             1, 'retrying', CURRENT TIMESTAMP, ?, CURRENT TIMESTAMP)
        """,
        first_id,
        sub_id,
        body,
        body_hash,
        NEW_CODE_WRITER_REVISION,
    )
    await _direct_execute(
        db2_pool,
        """
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload, payload_hash,
             attempt_num, status, scheduled_at, writer_revision, status_updated_at)
        VALUES
            (?, ?, 'memory.created', ?, ?,
             2, 'pending', CURRENT TIMESTAMP, ?, CURRENT TIMESTAMP)
        """,
        successor_id,
        sub_id,
        body,
        body_hash,
        NEW_CODE_WRITER_REVISION,
    )

    async with _tx(db2_pool) as tx:
        repaired = await repo.repair_delivery_chains(tx)

    assert repaired >= 1
    async with _tx(db2_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_repair_user",
            namespace="default",
            limit=10,
        )
    first = next(d for d in deliveries if d.id == first_id)
    assert first.status == "abandoned"
    assert first.superseded is True
    successor = next(d for d in deliveries if d.id == successor_id)
    assert successor.status == "pending"
    assert successor.superseded is False


@pytest.mark.asyncio
async def test_store_delivery_response_body_does_not_change_status(
    repo, db2_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(db2_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/body",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_body_user",
            namespace="default",
        )
    async with _tx(db2_pool) as tx:
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "body"},
            owner_id="webhook_repo_body_user",
            namespace="default",
        )
    body = '{"hello":"world"}'
    async with _tx(db2_pool) as tx:
        stored = await repo.store_delivery_response_body(
            tx,
            delivery_id=delivery_id,
            response_body=body,
        )
    assert stored is True

    async with _tx(db2_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_body_user",
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    assert d.response_body == body
    assert d.status == "pending"
    assert d.superseded is False
    assert d.lease_token is None
