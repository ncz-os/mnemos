"""Integration test for ``PostgresWebhookRepository``.

Item 3 of the ABC webhook persistence sequence. Verifies that the new
``WebhookRepository`` ABC methods on ``mnemos.persistence.postgres`` produce
behavior consistent with what the live ``mnemos/webhooks/`` code path
already does against the same schema (lease acquisition, SKIP LOCKED recovery,
writer-revision fence, retry-chain convergence, repair sweep).

Requires a real PostgreSQL instance reachable via ``MNEMOS_TEST_DB``. The
test applies a minimal webhook-only schema (users + webhook_subscriptions +
webhook_deliveries + the v3.5 webhook migrations) on first run so it works
without the full ``pgvector``/memories schema. When ``MNEMOS_TEST_DB`` is
unset the test is skipped — same pattern as
``tests/test_webhooks.py::TestWebhookIntegration``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from mnemos.core.webhook_constants import NEW_CODE_WRITER_REVISION
from mnemos.persistence.base import (
    WebhookDeliveryOutcome,
    WebhookSubscriptionRecord,
)
from mnemos.persistence.postgres import PostgresBackend, PostgresWebhookRepository

PG_URL = os.environ.get("MNEMOS_TEST_DB")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="set MNEMOS_TEST_DB=postgres://... to run webhook integration tests",
)


WEBHOOK_SCHEMA_DDL: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    """
    CREATE TABLE IF NOT EXISTS users (
        id          TEXT         PRIMARY KEY,
        username    TEXT         NOT NULL,
        role        TEXT         NOT NULL DEFAULT 'user',
        namespace   TEXT         NOT NULL DEFAULT 'default'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS webhook_subscriptions (
        id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
        url             TEXT         NOT NULL,
        events          TEXT[]       NOT NULL,
        secret          TEXT         NOT NULL,
        description     TEXT,
        owner_id        TEXT         NOT NULL DEFAULT 'default'
                                REFERENCES users(id) ON DELETE CASCADE,
        namespace       TEXT         NOT NULL DEFAULT 'default',
        created         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        revoked         BOOLEAN      NOT NULL DEFAULT FALSE,
        revoked_at      TIMESTAMPTZ,
        CONSTRAINT webhook_url_format
            CHECK (url LIKE 'http://%' OR url LIKE 'https://%'),
        CONSTRAINT webhook_events_nonempty
            CHECK (array_length(events, 1) > 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_owner "
    "    ON webhook_subscriptions(owner_id) WHERE NOT revoked",
    "CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_events "
    "    ON webhook_subscriptions USING gin(events) WHERE NOT revoked",
    """
    CREATE TABLE IF NOT EXISTS webhook_deliveries (
        id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
        subscription_id  UUID         NOT NULL
                                REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
        event_type       TEXT         NOT NULL,
        payload          TEXT         NOT NULL,
        payload_hash     TEXT         NOT NULL,
        attempt_num      INTEGER      NOT NULL DEFAULT 1,
        status           TEXT         NOT NULL DEFAULT 'pending',
        response_status  INTEGER,
        response_body    TEXT,
        error            TEXT,
        scheduled_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        delivered_at     TIMESTAMPTZ,
        created          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_subscription "
    "    ON webhook_deliveries(subscription_id, created DESC)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pending "
    "    ON webhook_deliveries(scheduled_at) "
    "    WHERE status IN ('pending', 'retrying')",
    "ALTER TABLE webhook_deliveries "
    "    ADD COLUMN IF NOT EXISTS lease_token UUID NULL",
    "ALTER TABLE webhook_deliveries "
    "    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_lease_expires_at "
    "    ON webhook_deliveries(lease_expires_at)",
    "ALTER TABLE webhook_deliveries "
    "    ADD COLUMN IF NOT EXISTS writer_revision INTEGER DEFAULT 0",
    "ALTER TABLE webhook_deliveries "
    "    ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMPTZ",
    "ALTER TABLE webhook_deliveries "
    "    ALTER COLUMN status_updated_at SET DEFAULT clock_timestamp(),"
    "    ALTER COLUMN status_updated_at SET NOT NULL",
    """
    CREATE OR REPLACE FUNCTION webhook_deliveries_set_status_updated_at()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF OLD.status IS DISTINCT FROM NEW.status THEN
            NEW.status_updated_at = clock_timestamp();
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS trg_webhook_deliveries_status_updated_at ON webhook_deliveries",
    "CREATE TRIGGER trg_webhook_deliveries_status_updated_at "
    "    BEFORE UPDATE ON webhook_deliveries "
    "    FOR EACH ROW "
    "    EXECUTE FUNCTION webhook_deliveries_set_status_updated_at()",
    "ALTER TABLE webhook_deliveries "
    "    ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_live_chain_attempt "
    "    ON webhook_deliveries(subscription_id, event_type, payload_hash, attempt_num) "
    "    WHERE status IN ('pending', 'retrying') AND NOT superseded",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_succeeded_chain "
    "    ON webhook_deliveries(subscription_id, event_type, payload_hash) "
    "    WHERE status = 'succeeded'",
    """
    CREATE OR REPLACE FUNCTION webhook_deliveries_enforce_succeeded_terminal()
    RETURNS TRIGGER AS $$
    BEGIN
        IF OLD.status = 'succeeded' AND NEW.status IS DISTINCT FROM 'succeeded' THEN
            RAISE EXCEPTION
                'webhook_deliveries: cannot transition status away from succeeded (id=%, attempted new status=%)',
                OLD.id, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS webhook_deliveries_succeeded_terminal ON webhook_deliveries",
    "CREATE TRIGGER webhook_deliveries_succeeded_terminal "
    "    BEFORE UPDATE ON webhook_deliveries "
    "    FOR EACH ROW "
    "    EXECUTE FUNCTION webhook_deliveries_enforce_succeeded_terminal()",
)


async def _ensure_webhook_schema(conn: asyncpg.Connection) -> None:
    for statement in WEBHOOK_SCHEMA_DDL:
        await conn.execute(statement)


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
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await _ensure_webhook_schema(conn)
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM webhook_deliveries WHERE subscription_id IN "
                "(SELECT id FROM webhook_subscriptions WHERE owner_id LIKE 'webhook_repo_%')"
            )
            await conn.execute(
                "DELETE FROM webhook_subscriptions WHERE owner_id LIKE 'webhook_repo_%'"
            )
            await conn.execute("DELETE FROM users WHERE id LIKE 'webhook_repo_%'")
        await pool.close()


@pytest_asyncio.fixture
async def repo(pg_pool) -> PostgresWebhookRepository:
    backend = PostgresBackend(pg_pool, SimpleNamespace())
    return backend.webhooks


@asynccontextmanager
async def _tx(pg_pool: asyncpg.Pool):
    """Yield a PostgresTransaction wrapped around a real pool acquire."""
    async with pg_pool.acquire() as conn:
        raw_tx = conn.transaction()
        await raw_tx.start()
        from mnemos.persistence.postgres import PostgresTransaction

        tx = PostgresTransaction(conn, raw_tx)
        try:
            yield tx
            if not tx.closed:
                await tx.commit()
        except BaseException:
            if not tx.closed:
                await tx.rollback()
            raise


@pytest_asyncio.fixture
async def make_user(pg_pool):
    async def _make(user_id: str) -> None:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (id, username, role, namespace)
                VALUES ($1, $1, 'user', 'default')
                ON CONFLICT (id) DO NOTHING
                """,
                user_id,
            )

    return _make


@pytest.mark.asyncio
async def test_create_and_get_subscription_roundtrip(repo, pg_pool, make_user):
    user_id = "webhook_repo_create_user"
    await make_user(user_id)
    subscription_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        record = await repo.create_subscription(
            tx,
            subscription_id=subscription_id,
            url="https://example.com/hook",
            events=("memory.created", "consultation.completed"),
            secret="top-secret",
            description="my subscription",
            owner_id=user_id,
            namespace="default",
        )
    assert isinstance(record, WebhookSubscriptionRecord)
    assert record.id == subscription_id
    assert record.url == "https://example.com/hook"
    assert record.events == ("memory.created", "consultation.completed")
    assert record.description == "my subscription"
    assert record.owner_id == user_id
    assert record.namespace == "default"
    assert record.revoked is False
    assert record.revoked_at is None
    assert record.created.tzinfo is not None

    async with _tx(pg_pool) as tx:
        fetched = await repo.get_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id=user_id,
            namespace="default",
        )
    assert fetched == record


@pytest.mark.asyncio
async def test_list_subscriptions_partial_scope_is_rejected(repo, pg_pool):
    with pytest.raises(ValueError, match="both"):
        async with _tx(pg_pool) as tx:
            await repo.list_subscriptions(
                tx,
                owner_id="alice",
                namespace=None,
                include_revoked=False,
                limit=10,
            )
    with pytest.raises(ValueError, match="both"):
        async with _tx(pg_pool) as tx:
            await repo.list_subscriptions(
                tx,
                owner_id=None,
                namespace="default",
                include_revoked=False,
                limit=10,
            )


@pytest.mark.asyncio
async def test_revoke_subscription_is_idempotent_and_scoped(repo, pg_pool, make_user):
    user_id = "webhook_repo_revoke_user"
    await make_user(user_id)
    subscription_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=subscription_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
    async with _tx(pg_pool) as tx:
        first = await repo.revoke_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id=user_id,
            namespace="default",
        )
        second = await repo.revoke_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id=user_id,
            namespace="default",
        )
        wrong_owner = await repo.revoke_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id="someone-else",
            namespace="default",
        )
    assert first is True
    assert second is False
    assert wrong_owner is False

    async with _tx(pg_pool) as tx:
        record = await repo.get_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id=user_id,
            namespace="default",
        )
    assert record is not None
    assert record.revoked is True
    assert record.revoked_at is not None
    assert record.revoked_at.tzinfo is not None


@pytest.mark.asyncio
async def test_dispatch_event_appends_first_attempt_pending_rows(repo, pg_pool, make_user):
    user_id = "webhook_repo_dispatch_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    other_sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        await repo.create_subscription(
            tx,
            subscription_id=other_sub_id,
            url="https://example.com/skip",
            events=("consultation.completed",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
    payload = {"memory_id": "mem-1", "kind": "create"}
    async with _tx(pg_pool) as tx:
        delivery_ids = await repo.dispatch_event(
            tx,
            "memory.created",
            payload,
            owner_id=user_id,
            namespace="default",
        )
    assert len(delivery_ids) == 1
    delivery_id = delivery_ids[0]
    assert delivery_id != sub_id  # delivery ids are generated UUIDs

    async with _tx(pg_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
        other = await repo.list_deliveries(
            tx,
            subscription_id=other_sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    assert len(deliveries) == 1
    assert other == []
    d = deliveries[0]
    assert d.id == delivery_id
    assert d.event_type == "memory.created"
    # The persisted payload must match the dispatcher contract: the JSON
    # body for the wire + the SHA-256 of that body. Compare parsed JSON
    # to ignore the timestamp microsecond drift between dispatch_event
    # and the test's recomputation.
    parsed = json.loads(d.payload)
    assert parsed["event"] == "memory.created"
    assert parsed["data"] == payload
    assert "timestamp" in parsed
    assert d.payload_hash == _expected_payload_hash(d.payload)
    assert d.attempt_num == 1
    assert d.status == "pending"
    assert d.superseded is False
    assert d.lease_token is None
    assert d.lease_expires_at is None
    assert d.writer_revision == NEW_CODE_WRITER_REVISION
    assert d.scheduled_at.tzinfo is not None
    assert d.delivered_at is None


@pytest.mark.asyncio
async def test_claim_delivery_assigns_lease_and_winner_takes_all(repo, pg_pool, make_user):
    user_id = "webhook_repo_claim_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="the-secret",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "claim-me"},
            owner_id=user_id,
            namespace="default",
        )

    lease_seconds = 30
    async with _tx(pg_pool) as tx:
        claim_a = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=str(uuid.uuid4()),
            lease_seconds=lease_seconds,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim_a is not None
    assert claim_a.delivery.id == delivery_id
    assert claim_a.delivery.status == "retrying"  # pending -> retrying on claim
    assert claim_a.delivery.lease_token is not None
    assert claim_a.delivery.lease_expires_at is not None
    assert claim_a.lease_expires_at.tzinfo is not None
    assert claim_a.claim_db_now.tzinfo is not None
    assert claim_a.url == "https://example.com/hook"
    assert claim_a.secret == "the-secret"
    assert claim_a.subscription_revoked is False
    assert claim_a.owner_id == user_id
    assert claim_a.namespace == "default"

    async with _tx(pg_pool) as tx:
        claim_b = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=str(uuid.uuid4()),
            lease_seconds=lease_seconds,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim_b is None


@pytest.mark.asyncio
async def test_claim_delivery_rejects_stale_writer_revision(repo, pg_pool, make_user):
    user_id = "webhook_repo_wrev_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "mem"},
            owner_id=user_id,
            namespace="default",
        )
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=str(uuid.uuid4()),
            lease_seconds=30,
            max_attempts=4,
            writer_revision=0,  # legacy revision; row was written at 1
        )
    assert claim is None


@pytest.mark.asyncio
async def test_claim_due_deliveries_skip_locked_keeps_workers_independent(
    repo, pg_pool, make_user
):
    user_id = "webhook_repo_skip_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [first_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "due-0"},
            owner_id=user_id,
            namespace="default",
        )
        extra_ids = []
        for i in range(1, 4):
            [did] = await repo.dispatch_event(
                tx,
                "memory.created",
                {"memory_id": f"due-{i}"},
                owner_id=user_id,
                namespace="default",
            )
            extra_ids.append(did)
    all_ids = {first_id, *extra_ids}
    assert len(all_ids) == 4

    # Worker A claims two.
    async with _tx(pg_pool) as tx:
        a_claims = await repo.claim_due_deliveries(
            tx,
            lease_token=str(uuid.uuid4()),
            limit=2,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    a_ids = {claim.delivery.id for claim in a_claims}
    assert len(a_ids) == 2
    assert a_ids.issubset(all_ids)

    # Worker B claims the rest with its own lease token.
    async with _tx(pg_pool) as tx:
        b_claims = await repo.claim_due_deliveries(
            tx,
            lease_token=str(uuid.uuid4()),
            limit=2,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    b_ids = {claim.delivery.id for claim in b_claims}
    assert len(b_ids) == 2
    assert a_ids.isdisjoint(b_ids)
    assert a_ids | b_ids == all_ids

    for claim in a_claims + b_claims:
        assert claim.delivery.lease_token == claim.lease_token
        assert claim.delivery.lease_expires_at is not None


@pytest.mark.asyncio
async def test_release_delivery_claim_keeps_row_live(repo, pg_pool, make_user):
    user_id = "webhook_repo_release_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "release-me"},
            owner_id=user_id,
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(pg_pool) as tx:
        released = await repo.release_delivery_claim(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
        )
    assert released is True

    async with _tx(pg_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    assert d.lease_token is None
    assert d.lease_expires_at is None
    assert d.status == "retrying"  # promotion kept, lease cleared

    async with _tx(pg_pool) as tx:
        again = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=str(uuid.uuid4()),
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert again is not None


@pytest.mark.asyncio
async def test_release_delivery_claim_rejects_wrong_token(repo, pg_pool, make_user):
    user_id = "webhook_repo_relbad_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "x"},
            owner_id=user_id,
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(pg_pool) as tx:
        bad = await repo.release_delivery_claim(
            tx,
            delivery_id=delivery_id,
            lease_token=str(uuid.uuid4()),
        )
    assert bad is False


@pytest.mark.asyncio
async def test_guard_delivery_claim_returns_true_when_owned(repo, pg_pool, make_user):
    user_id = "webhook_repo_guard_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "guard-me"},
            owner_id=user_id,
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(pg_pool) as tx:
        guarded = await repo.guard_delivery_claim(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
        )
    assert guarded is True


@pytest.mark.asyncio
async def test_finalize_delivery_success_creates_chain_terminal(repo, pg_pool, make_user):
    user_id = "webhook_repo_succ_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "ok"},
            owner_id=user_id,
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(pg_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            outcome=WebhookDeliveryOutcome(
                succeeded=True,
                response_status=200,
                response_body="ok",
            ),
            max_attempts=4,
            backoff_schedule=(60, 300, 1800),
        )
    assert result.applied is True
    assert result.status == "succeeded"
    assert result.successor_delivery_id is None

    async with _tx(pg_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    assert d.status == "succeeded"
    assert d.response_status == 200
    assert d.lease_token is None
    assert d.lease_expires_at is None
    assert d.delivered_at is not None


@pytest.mark.asyncio
async def test_finalize_delivery_retryable_failure_schedules_successor(
    repo, pg_pool, make_user
):
    user_id = "webhook_repo_retry_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "retry-me"},
            owner_id=user_id,
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(pg_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            outcome=WebhookDeliveryOutcome(
                succeeded=False,
                response_status=503,
                response_body="upstream busy",
                error="503 service unavailable",
            ),
            max_attempts=4,
            backoff_schedule=(60, 300, 1800),
        )
    assert result.applied is True
    assert result.status == "abandoned"
    assert result.successor_delivery_id is not None

    async with _tx(pg_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    assert len(deliveries) == 2
    attempts = sorted(deliveries, key=lambda d: d.attempt_num)
    assert [a.attempt_num for a in attempts] == [1, 2]
    assert attempts[0].status == "abandoned"
    assert attempts[0].superseded is True
    assert attempts[0].response_status == 503
    assert attempts[0].lease_token is None
    assert attempts[1].status == "pending"
    assert attempts[1].superseded is False
    assert attempts[1].payload == attempts[0].payload
    assert attempts[1].payload_hash == attempts[0].payload_hash
    now_utc = datetime.now(timezone.utc)
    assert attempts[1].scheduled_at.replace(tzinfo=timezone.utc) > now_utc
    assert attempts[1].scheduled_at.replace(tzinfo=timezone.utc) - now_utc <= timedelta(
        seconds=120
    )


@pytest.mark.asyncio
async def test_finalize_delivery_exhaustion_ends_abandoned_no_successor(
    repo, pg_pool, make_user
):
    user_id = "webhook_repo_exhaust_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "exhaust"},
            owner_id=user_id,
            namespace="default",
        )
    # Promote the row to attempt_num=max_attempts so any failure exhausts.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE webhook_deliveries SET attempt_num = 4 WHERE id = $1::uuid",
            delivery_id,
        )
    token = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None
    async with _tx(pg_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            outcome=WebhookDeliveryOutcome(
                succeeded=False,
                response_status=503,
                error="exhaust",
            ),
            max_attempts=4,
            backoff_schedule=(60, 300, 1800),
        )
    assert result.applied is True
    assert result.status == "abandoned"
    assert result.successor_delivery_id is None

    async with _tx(pg_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    assert d.status == "abandoned"
    assert d.superseded is False  # final failure, not supersede
    assert d.response_status == 503


@pytest.mark.asyncio
async def test_finalize_delivery_failure_after_lease_expired_returns_not_applied(
    repo, pg_pool, make_user
):
    user_id = "webhook_repo_expired_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "exp"},
            owner_id=user_id,
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=1,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None
    # Wait past the lease expiry; failure finalization must refuse a stale
    # token even when the row is still live and unlocked.
    await asyncio.sleep(1.5)

    async with _tx(pg_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            outcome=WebhookDeliveryOutcome(
                succeeded=False,
                response_status=503,
                error="lease-already-gone",
            ),
            max_attempts=4,
            backoff_schedule=(60, 300, 1800),
        )
    assert result.applied is False

    async with _tx(pg_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    # Row state must be one a recovery worker could legitimately claim;
    # this loser must NOT have advanced the chain.
    assert d.status in ("pending", "retrying")


@pytest.mark.asyncio
async def test_finalize_delivery_success_after_lease_expired_with_matching_token(
    repo, pg_pool, make_user
):
    """Spec: 'success may commit after expiry when the fencing token still
    matches, preserving a received 2xx'. Verify the ABC implementation
    honors that."""
    user_id = "webhook_repo_latesucc_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "late-ok"},
            owner_id=user_id,
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=1,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None
    await asyncio.sleep(1.5)

    async with _tx(pg_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            outcome=WebhookDeliveryOutcome(
                succeeded=True,
                response_status=200,
                response_body="ok",
            ),
            max_attempts=4,
            backoff_schedule=(60, 300, 1800),
        )
    assert result.applied is True
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_finalize_delivery_duplicate_success_converges_as_abandoned(
    repo, pg_pool, make_user
):
    """A second worker that races a 2xx into the same chain must converge
    to abandoned/superseded instead of writing a second success row.

    The live ``mnemos/webhooks/finalize.py`` path: when a success commits,
    it abandons all live unleased successors in the same transaction. A
    second worker that later tries to finalize a same-chain attempt as
    success therefore finds the row already abandoned (or in pending
    with a now-succeeded peer) and converges via the same
    ``abandon_owned_after_peer`` path. The end state is: exactly one
    succeeded row per chain.
    """
    user_id = "webhook_repo_dupe_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [first_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "chain-1"},
            owner_id=user_id,
            namespace="default",
        )

    # Worker A claims + succeeds attempt_num=1.
    token_a = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim_a = await repo.claim_delivery(
            tx,
            delivery_id=first_id,
            lease_token=token_a,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim_a is not None
    async with _tx(pg_pool) as tx:
        result_a = await repo.finalize_delivery(
            tx,
            delivery_id=first_id,
            lease_token=token_a,
            outcome=WebhookDeliveryOutcome(
                succeeded=True,
                response_status=200,
                response_body="first",
            ),
            max_attempts=4,
            backoff_schedule=(60, 300, 1800),
        )
    assert result_a.applied is True
    assert result_a.status == "succeeded"

    # Now insert a fresh attempt_num=2 row with the SAME chain key
    # (the test demonstrates what happens when a recovery worker creates
    # a successor after attempt 1 has already succeeded; the live code
    # abandons it immediately on the success commit, but a worker that
    # races against that commit must still converge).
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload, payload_hash FROM webhook_deliveries WHERE id = $1::uuid",
            first_id,
        )
    duplicate_id = str(uuid.uuid4())
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO webhook_deliveries
                (id, subscription_id, event_type, payload, payload_hash,
                 attempt_num, status, scheduled_at, writer_revision)
            VALUES (
                $1::uuid, $2::uuid, 'memory.created', $3, $4,
                2, 'pending', NOW(), $5
            )
            """,
            duplicate_id,
            sub_id,
            row["payload"],
            row["payload_hash"],
            NEW_CODE_WRITER_REVISION,
        )

    # Worker B claims the fresh row and finalizes as success. The chain
    # already has a succeeded peer; the success UPDATE must converge
    # via the peer-succeeded path to abandoned/superseded.
    token_b = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        claim_b = await repo.claim_delivery(
            tx,
            delivery_id=duplicate_id,
            lease_token=token_b,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim_b is not None
    async with _tx(pg_pool) as tx:
        result_b = await repo.finalize_delivery(
            tx,
            delivery_id=duplicate_id,
            lease_token=token_b,
            outcome=WebhookDeliveryOutcome(
                succeeded=True,
                response_status=200,
                response_body="duplicate",
            ),
            max_attempts=4,
            backoff_schedule=(60, 300, 1800),
        )
    # Either path is acceptable per the contract: the worker may detect
    # the peer BEFORE attempting the success UPDATE (returns
    # applied=True, status='abandoned'), or it may attempt the success
    # UPDATE first, find the row already abandoned by the success-commit
    # abandon-successors sweep (returns applied=False). The invariant:
    # exactly ONE succeeded row in the chain.
    assert result_b.applied is False or result_b.status == "abandoned"

    async with _tx(pg_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    succeeded_count = sum(1 for d in deliveries if d.status == "succeeded")
    assert succeeded_count == 1, (
        f"expected exactly one succeeded row, got {succeeded_count}: "
        f"{[(d.status, d.superseded, d.attempt_num) for d in deliveries]}"
    )


@pytest.mark.asyncio
async def test_store_delivery_response_body_does_not_change_status(repo, pg_pool, make_user):
    user_id = "webhook_repo_body_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "body"},
            owner_id=user_id,
            namespace="default",
        )
    body = '{"hello":"world"}'
    async with _tx(pg_pool) as tx:
        stored = await repo.store_delivery_response_body(
            tx,
            delivery_id=delivery_id,
            response_body=body,
        )
    assert stored is True

    async with _tx(pg_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    assert d.response_body == body
    assert d.status == "pending"
    assert d.superseded is False
    assert d.lease_token is None


@pytest.mark.asyncio
async def test_repair_delivery_chains_terminalizes_obsolete_live_rows(
    repo, pg_pool, make_user
):
    user_id = "webhook_repo_repair_user"
    await make_user(user_id)
    sub_id = str(uuid.uuid4())
    async with _tx(pg_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id=user_id,
            namespace="default",
        )
    body = _payload_for("memory.created", {"memory_id": "obsolete"})
    body_hash = _expected_payload_hash(body)
    first_id = str(uuid.uuid4())
    successor_id = str(uuid.uuid4())
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO webhook_deliveries
                (id, subscription_id, event_type, payload, payload_hash,
                 attempt_num, status, scheduled_at, writer_revision)
            VALUES (
                $1::uuid, $2::uuid, 'memory.created', $3, $4,
                1, 'retrying', NOW(), $5
            )
            """,
            first_id,
            sub_id,
            body,
            body_hash,
            NEW_CODE_WRITER_REVISION,
        )
        await conn.execute(
            """
            INSERT INTO webhook_deliveries
                (id, subscription_id, event_type, payload, payload_hash,
                 attempt_num, status, scheduled_at, writer_revision)
            VALUES (
                $1::uuid, $2::uuid, 'memory.created', $3, $4,
                2, 'pending', NOW(), $5
            )
            """,
            successor_id,
            sub_id,
            body,
            body_hash,
            NEW_CODE_WRITER_REVISION,
        )

    async with _tx(pg_pool) as tx:
        pre = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    by_id = {d.id: d for d in pre}
    assert by_id[first_id].status == "retrying"

    async with _tx(pg_pool) as tx:
        repaired = await repo.repair_delivery_chains(tx)
    assert repaired >= 1

    async with _tx(pg_pool) as tx:
        post = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id=user_id,
            namespace="default",
            limit=10,
        )
    by_id = {d.id: d for d in post}
    assert by_id[first_id].status == "abandoned"
    assert by_id[first_id].superseded is True
    assert by_id[successor_id].status == "pending"
