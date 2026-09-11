"""Integration test for ``MysqlWebhookRepository``.

Item 5 of the ABC webhook persistence sequence. Verifies that the new
``WebhookRepository`` ABC methods on ``mnemos.persistence.mysql`` produce
behavior consistent with what the live ``mnemos/webhooks/`` code path
already does against the same logical schema (lease acquisition,
``FOR UPDATE SKIP LOCKED`` recovery, writer-revision fence, retry-chain
convergence, repair sweep).

Requires a real MySQL or MariaDB instance reachable via ``MNEMOS_TEST_MYSQL``.
The test applies a minimal webhook-only schema (webhook_subscriptions +
webhook_deliveries + the v6.3-equivalent triggers / generated-column
partial-index emulators) on first run so it works without the full
``mnemos_test`` schema. When ``MNEMOS_TEST_MYSQL`` is unset the test is
skipped — same pattern as ``tests/test_postgres_webhook_repository.py``.

The DSN accepts both ``mysql://user:pass@host:port/db`` and
``mariadb://user:pass@host:port/db``; either backend shares the same
``aiomysql`` pool and identical SQL surface, so the schema and queries
exercise the contract against both.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio

from mnemos.core.webhook_constants import NEW_CODE_WRITER_REVISION
from mnemos.persistence.base import (
    WebhookDeliveryOutcome,
    WebhookSubscriptionRecord,
)

# Reuse the live MySQL DSN or MariaDB DSN — both are valid for the
# ``aiomysql`` pool and the SQL the contract relies on.  Operators may
# point either at a real MySQL server (8.0+) or a MariaDB server (10.6+).
DSN = os.environ.get("MNEMOS_TEST_MYSQL") or os.environ.get("MNEMOS_TEST_MARIADB")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason=(
        "set MNEMOS_TEST_MYSQL=mysql://... (or MNEMOS_TEST_MARIADB=mariadb://...) "
        "to run webhook integration tests against a live MySQL/MariaDB instance"
    ),
)


WEBHOOK_SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS webhook_subscriptions (
        id              VARCHAR(64)   NOT NULL DEFAULT (UUID()),
        url             TEXT         NOT NULL,
        events          JSON         NOT NULL,
        secret          TEXT         NOT NULL,
        description     TEXT         NULL,
        owner_id        VARCHAR(256) NOT NULL DEFAULT 'default',
        namespace       VARCHAR(256) NOT NULL DEFAULT 'default',
        created         DATETIME(6)  NOT NULL DEFAULT NOW(6),
        revoked         TINYINT(1)   NOT NULL DEFAULT 0,
        revoked_at      DATETIME(6)  NULL,
        PRIMARY KEY (id),
        INDEX idx_webhook_subscriptions_owner (owner_id, namespace)
    ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS webhook_deliveries (
        id                VARCHAR(64)   NOT NULL DEFAULT (UUID()),
        subscription_id   VARCHAR(64)   NOT NULL,
        event_type        VARCHAR(256)  NOT NULL,
        payload           LONGTEXT      NOT NULL,
        payload_hash      VARCHAR(64)   NOT NULL,
        attempt_num       INT           NOT NULL DEFAULT 1,
        status            VARCHAR(32)   NOT NULL DEFAULT 'pending',
        response_status   INT           NULL,
        response_body     LONGTEXT      NULL,
        error             TEXT          NULL,
        scheduled_at      DATETIME(6)   NOT NULL DEFAULT NOW(6),
        delivered_at      DATETIME(6)   NULL,
        created           DATETIME(6)   NOT NULL DEFAULT NOW(6),
        lease_token       VARCHAR(64)   NULL,
        lease_expires_at  DATETIME(6)   NULL,
        writer_revision   INT           NOT NULL DEFAULT 1,
        status_updated_at DATETIME(6)   NOT NULL DEFAULT NOW(6),
        superseded        TINYINT(1)    NOT NULL DEFAULT 0,
        live_chain_key    VARCHAR(768)  GENERATED ALWAYS AS (
            CASE
                WHEN status IN ('pending', 'retrying') AND superseded = 0
                THEN CONCAT(subscription_id, '|', event_type, '|', payload_hash, '|', attempt_num)
                ELSE NULL
            END
        ) STORED,
        succeeded_chain_key VARCHAR(768) GENERATED ALWAYS AS (
            CASE
                WHEN status = 'succeeded'
                THEN CONCAT(subscription_id, '|', event_type, '|', payload_hash)
                ELSE NULL
            END
        ) STORED,
        PRIMARY KEY (id),
        CONSTRAINT fk_webhook_deliveries_subscription
            FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id) ON DELETE CASCADE
    ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_subscription "
    "ON webhook_deliveries(subscription_id, created)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pending "
    "ON webhook_deliveries(scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_lease_expires_at "
    "ON webhook_deliveries(lease_expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_live_chain_attempt "
    "ON webhook_deliveries(live_chain_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_succeeded_chain "
    "ON webhook_deliveries(succeeded_chain_key)",
    """
    DROP TRIGGER IF EXISTS webhook_deliveries_set_status_updated_at
    """,
    """
    CREATE TRIGGER webhook_deliveries_set_status_updated_at
    BEFORE UPDATE ON webhook_deliveries
    FOR EACH ROW
      SET NEW.status_updated_at = IF(OLD.status <> NEW.status, NOW(6), OLD.status_updated_at)
    """,
    """
    DROP TRIGGER IF EXISTS webhook_deliveries_enforce_succeeded_terminal
    """,
    """
    CREATE TRIGGER webhook_deliveries_enforce_succeeded_terminal
    BEFORE UPDATE ON webhook_deliveries
    FOR EACH ROW
    BEGIN
      IF OLD.status = 'succeeded' AND NEW.status <> 'succeeded' THEN
        SIGNAL SQLSTATE '45000'
          SET MESSAGE_TEXT = 'webhook_deliveries: cannot transition status away from succeeded';
      END IF;
    END
    """,
)


def _split_trigger_statements(sql: str) -> list[str]:
    """Statement splitter that understands MySQL/MariaDB ``BEGIN ... END``
    trigger bodies (the shared ``split_postgres_statements`` does not,
    so the trigger CREATE would be split in half by ``;``).
    """
    from mnemos.persistence.mysql import _split_mysql_statements

    return _split_mysql_statements(sql)


async def _ensure_webhook_schema(conn: Any) -> None:
    async with conn.cursor() as cursor:
        for ddl in WEBHOOK_SCHEMA_DDL:
            for statement in _split_trigger_statements(ddl):
                await cursor.execute(statement)


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
async def mysql_pool() -> AsyncIterator[Any]:
    """Yield an aiomysql pool and provision the minimal webhook schema."""
    import aiomysql
    from urllib.parse import urlparse

    parsed = urlparse(DSN)
    kwargs: dict[str, Any] = {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "db": (parsed.path or "/mnemos_test").lstrip("/") or "mnemos_test",
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if parsed.username:
        kwargs["user"] = parsed.username
    if parsed.password:
        kwargs["password"] = parsed.password
    pool = await aiomysql.create_pool(minsize=1, maxsize=4, **kwargs)
    async with pool.acquire() as conn:
        await _ensure_webhook_schema(conn)
        await conn.commit()
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM webhook_deliveries WHERE subscription_id IN "
                    "(SELECT id FROM webhook_subscriptions WHERE owner_id LIKE 'webhook_repo_%')"
                )
                await cur.execute(
                    "DELETE FROM webhook_subscriptions WHERE owner_id LIKE 'webhook_repo_%'"
                )
            await conn.commit()
        pool.close()
        await pool.wait_closed()


@pytest_asyncio.fixture
async def repo(mysql_pool):
    from mnemos.persistence.mysql import MysqlWebhookRepository

    return MysqlWebhookRepository()


@asynccontextmanager
async def _tx(mysql_pool):
    """Yield a ``_MysqlTransaction`` wrapped around a real pool acquire."""
    from mnemos.persistence.mysql import _MysqlTransaction

    async with mysql_pool.acquire() as conn:
        await conn.begin()
        tx = _MysqlTransaction(conn)
        try:
            yield tx
            if not tx.closed:
                await tx.commit()
        except BaseException:
            if not tx.closed:
                await tx.rollback()
            raise


async def _direct_execute(mysql_pool, sql: str, *args: Any) -> int:
    """Run a raw DML against the pool outside of a fixture transaction.

    Returns affected row count.  Used to insert fixture rows with
    ``attempt_num`` values the contract layer would not normally write
    (so the test can pin an explicit chain shape).
    """
    async with mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, args or None)
            affected = int(getattr(cur, "rowcount", 0) or 0)
        await conn.commit()
    return affected


@pytest.mark.asyncio
async def test_create_and_get_subscription_roundtrip(repo, mysql_pool):
    subscription_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
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

    async with _tx(mysql_pool) as tx:
        fetched = await repo.get_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id="webhook_repo_create_user",
            namespace="default",
        )
    assert fetched == record


@pytest.mark.asyncio
async def test_list_subscriptions_partial_scope_is_rejected(repo, mysql_pool):
    with pytest.raises(ValueError, match="both"):
        async with _tx(mysql_pool) as tx:
            await repo.list_subscriptions(
                tx,
                owner_id="alice",
                namespace=None,
                include_revoked=False,
                limit=10,
            )
    with pytest.raises(ValueError, match="both"):
        async with _tx(mysql_pool) as tx:
            await repo.list_subscriptions(
                tx,
                owner_id=None,
                namespace="default",
                include_revoked=False,
                limit=10,
            )


@pytest.mark.asyncio
async def test_revoke_subscription_is_idempotent_and_scoped(repo, mysql_pool):
    subscription_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=subscription_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_revoke_user",
            namespace="default",
        )
    async with _tx(mysql_pool) as tx:
        first = await repo.revoke_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id="webhook_repo_revoke_user",
            namespace="default",
        )
        second = await repo.revoke_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id="webhook_repo_revoke_user",
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

    async with _tx(mysql_pool) as tx:
        record = await repo.get_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id="webhook_repo_revoke_user",
            namespace="default",
        )
    assert record is not None
    assert record.revoked is True
    assert record.revoked_at is not None
    assert record.revoked_at.tzinfo is not None


@pytest.mark.asyncio
async def test_dispatch_event_appends_first_attempt_pending_rows(repo, mysql_pool):
    sub_id = str(uuid.uuid4())
    other_sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_dispatch_user",
            namespace="default",
        )
        await repo.create_subscription(
            tx,
            subscription_id=other_sub_id,
            url="https://example.com/skip",
            events=("consultation.completed",),
            secret="s",
            description=None,
            owner_id="webhook_repo_dispatch_user",
            namespace="default",
        )
    payload = {"memory_id": "mem-1", "kind": "create"}
    async with _tx(mysql_pool) as tx:
        delivery_ids = await repo.dispatch_event(
            tx,
            "memory.created",
            payload,
            owner_id="webhook_repo_dispatch_user",
            namespace="default",
        )
    assert len(delivery_ids) == 1
    delivery_id = delivery_ids[0]
    assert delivery_id != sub_id

    async with _tx(mysql_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_dispatch_user",
            namespace="default",
            limit=10,
        )
        other = await repo.list_deliveries(
            tx,
            subscription_id=other_sub_id,
            owner_id="webhook_repo_dispatch_user",
            namespace="default",
            limit=10,
        )
    assert len(deliveries) == 1
    assert other == []
    d = deliveries[0]
    assert d.id == delivery_id
    assert d.event_type == "memory.created"
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
async def test_claim_delivery_assigns_lease_and_winner_takes_all(repo, mysql_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="the-secret",
            description=None,
            owner_id="webhook_repo_claim_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "claim-me"},
            owner_id="webhook_repo_claim_user",
            namespace="default",
        )

    lease_seconds = 30
    async with _tx(mysql_pool) as tx:
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
    assert claim_a.delivery.status == "retrying"
    assert claim_a.delivery.lease_token is not None
    assert claim_a.delivery.lease_expires_at is not None
    assert claim_a.lease_expires_at.tzinfo is not None
    assert claim_a.claim_db_now.tzinfo is not None
    assert claim_a.url == "https://example.com/hook"
    assert claim_a.secret == "the-secret"
    assert claim_a.subscription_revoked is False
    assert claim_a.owner_id == "webhook_repo_claim_user"
    assert claim_a.namespace == "default"

    async with _tx(mysql_pool) as tx:
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
async def test_claim_delivery_rejects_stale_writer_revision(repo, mysql_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_wrev_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "mem"},
            owner_id="webhook_repo_wrev_user",
            namespace="default",
        )
    async with _tx(mysql_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=str(uuid.uuid4()),
            lease_seconds=30,
            max_attempts=4,
            writer_revision=0,
        )
    assert claim is None


@pytest.mark.asyncio
async def test_claim_due_deliveries_skip_locked_keeps_workers_independent(
    repo, mysql_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_skip_user",
            namespace="default",
        )
        [first_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "due-0"},
            owner_id="webhook_repo_skip_user",
            namespace="default",
        )
        extra_ids = []
        for i in range(1, 4):
            [did] = await repo.dispatch_event(
                tx,
                "memory.created",
                {"memory_id": f"due-{i}"},
                owner_id="webhook_repo_skip_user",
                namespace="default",
            )
            extra_ids.append(did)
    all_ids = {first_id, *extra_ids}
    assert len(all_ids) == 4

    async with _tx(mysql_pool) as tx:
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

    async with _tx(mysql_pool) as tx:
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
async def test_release_delivery_claim_keeps_row_live(repo, mysql_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_release_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "release-me"},
            owner_id="webhook_repo_release_user",
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(mysql_pool) as tx:
        released = await repo.release_delivery_claim(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
        )
    assert released is True

    async with _tx(mysql_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_release_user",
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    assert d.lease_token is None
    assert d.lease_expires_at is None
    assert d.status == "retrying"

    async with _tx(mysql_pool) as tx:
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
async def test_release_delivery_claim_rejects_wrong_token(repo, mysql_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_relbad_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "x"},
            owner_id="webhook_repo_relbad_user",
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(mysql_pool) as tx:
        bad = await repo.release_delivery_claim(
            tx,
            delivery_id=delivery_id,
            lease_token=str(uuid.uuid4()),
        )
    assert bad is False


@pytest.mark.asyncio
async def test_guard_delivery_claim_returns_true_when_owned(repo, mysql_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_guard_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "guard-me"},
            owner_id="webhook_repo_guard_user",
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(mysql_pool) as tx:
        guarded = await repo.guard_delivery_claim(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
        )
    assert guarded is True


@pytest.mark.asyncio
async def test_finalize_delivery_success_creates_chain_terminal(repo, mysql_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_succ_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "ok"},
            owner_id="webhook_repo_succ_user",
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(mysql_pool) as tx:
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

    async with _tx(mysql_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_succ_user",
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
    repo, mysql_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_retry_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "retry-me"},
            owner_id="webhook_repo_retry_user",
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(mysql_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            outcome=WebhookDeliveryOutcome(
                succeeded=False,
                response_status=503,
                error="busy",
            ),
            max_attempts=4,
            backoff_schedule=(60, 300, 1800),
        )
    assert result.applied is True
    assert result.status == "abandoned"
    assert result.successor_delivery_id is not None

    async with _tx(mysql_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_retry_user",
            namespace="default",
            limit=10,
        )
    attempts = sorted(d.attempt_num for d in deliveries)
    assert attempts == [1, 2]


@pytest.mark.asyncio
async def test_finalize_delivery_failure_after_lease_expired_returns_not_applied(
    repo, mysql_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_expired_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "exp"},
            owner_id="webhook_repo_expired_user",
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
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

    async with _tx(mysql_pool) as tx:
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

    async with _tx(mysql_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_expired_user",
            namespace="default",
            limit=10,
        )
    [d] = deliveries
    assert d.status in ("pending", "retrying")


@pytest.mark.asyncio
async def test_finalize_delivery_success_after_lease_expired_with_matching_token(
    repo, mysql_pool
):
    """Spec: 'success may commit after expiry when the fencing token still
    matches, preserving a received 2xx'. Verify the ABC implementation
    honors that."""
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_latesucc_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "late-ok"},
            owner_id="webhook_repo_latesucc_user",
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
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

    async with _tx(mysql_pool) as tx:
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
    repo, mysql_pool
):
    """A second worker that races a 2xx into the same chain must converge
    to abandoned/superseded instead of writing a second success row.

    The Postgres / SQLite reference: exactly one succeeded row per chain.
    """
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_dupe_user",
            namespace="default",
        )
        [first_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "chain-1"},
            owner_id="webhook_repo_dupe_user",
            namespace="default",
        )

    token_a = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim_a = await repo.claim_delivery(
            tx,
            delivery_id=first_id,
            lease_token=token_a,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim_a is not None
    async with _tx(mysql_pool) as tx:
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

    async with mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT payload, payload_hash FROM webhook_deliveries WHERE id = %s",
                (first_id,),
            )
            row = await cur.fetchone()
    duplicate_id = str(uuid.uuid4())
    async with mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO webhook_deliveries
                    (id, subscription_id, event_type, payload, payload_hash,
                     attempt_num, status, scheduled_at, writer_revision)
                VALUES (%s, %s, 'memory.created', %s, %s,
                        2, 'pending', NOW(6), %s)
                """,
                (
                    duplicate_id,
                    sub_id,
                    row[0],
                    row[1],
                    NEW_CODE_WRITER_REVISION,
                ),
            )
        await conn.commit()

    token_b = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim_b = await repo.claim_delivery(
            tx,
            delivery_id=duplicate_id,
            lease_token=token_b,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim_b is not None
    async with _tx(mysql_pool) as tx:
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
    assert result_b.applied is False or result_b.status == "abandoned"

    async with _tx(mysql_pool) as tx:
        deliveries = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_dupe_user",
            namespace="default",
            limit=10,
        )
    succeeded_count = sum(1 for d in deliveries if d.status == "succeeded")
    assert succeeded_count == 1, (
        f"expected exactly one succeeded row, got {succeeded_count}: "
        f"{[(d.status, d.superseded, d.attempt_num) for d in deliveries]}"
    )


@pytest.mark.asyncio
async def test_store_delivery_response_body_does_not_change_status(repo, mysql_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_body_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "body"},
            owner_id="webhook_repo_body_user",
            namespace="default",
        )
    body = '{"hello":"world"}'
    async with _tx(mysql_pool) as tx:
        stored = await repo.store_delivery_response_body(
            tx,
            delivery_id=delivery_id,
            response_body=body,
        )
    assert stored is True

    async with _tx(mysql_pool) as tx:
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


@pytest.mark.asyncio
async def test_repair_delivery_chains_terminalizes_obsolete_live_rows(
    repo, mysql_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
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
    async with mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO webhook_deliveries
                    (id, subscription_id, event_type, payload, payload_hash,
                     attempt_num, status, scheduled_at, writer_revision)
                VALUES (%s, %s, 'memory.created', %s, %s,
                        1, 'retrying', NOW(6), %s)
                """,
                (first_id, sub_id, body, body_hash, NEW_CODE_WRITER_REVISION),
            )
            await cur.execute(
                """
                INSERT INTO webhook_deliveries
                    (id, subscription_id, event_type, payload, payload_hash,
                     attempt_num, status, scheduled_at, writer_revision)
                VALUES (%s, %s, 'memory.created', %s, %s,
                        2, 'pending', NOW(6), %s)
                """,
                (successor_id, sub_id, body, body_hash, NEW_CODE_WRITER_REVISION),
            )
        await conn.commit()

    async with _tx(mysql_pool) as tx:
        pre = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_repair_user",
            namespace="default",
            limit=10,
        )
    by_id = {d.id: d for d in pre}
    assert by_id[first_id].status == "retrying"

    async with _tx(mysql_pool) as tx:
        repaired = await repo.repair_delivery_chains(tx)
    assert repaired >= 1

    async with _tx(mysql_pool) as tx:
        post = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_repair_user",
            namespace="default",
            limit=10,
        )
    by_id = {d.id: d for d in post}
    assert by_id[first_id].status == "abandoned"
    assert by_id[first_id].superseded is True
    assert by_id[successor_id].status == "pending"


@pytest.mark.asyncio
async def test_status_transition_clock_advances_only_on_real_status_changes(
    repo, mysql_pool
):
    """Verify the BEFORE UPDATE trigger only bumps status_updated_at when
    status itself changes (audit field that should not flap on every
    response_body write)."""
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_tsclock_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "ts"},
            owner_id="webhook_repo_tsclock_user",
            namespace="default",
        )

    async with _tx(mysql_pool) as tx:
        before = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_tsclock_user",
            namespace="default",
            limit=10,
        )
    [row] = before
    initial_clock = row.status_updated_at

    # Wait long enough that a status change would visibly advance the
    # microsecond clock, then write response_body only.
    await asyncio.sleep(0.05)
    async with _tx(mysql_pool) as tx:
        await repo.store_delivery_response_body(
            tx,
            delivery_id=delivery_id,
            response_body="audit body",
        )

    async with _tx(mysql_pool) as tx:
        after = await repo.list_deliveries(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_tsclock_user",
            namespace="default",
            limit=10,
        )
    [row] = after
    assert row.status_updated_at == initial_clock, (
        "status_updated_at must not advance on a response_body-only update"
    )
    assert row.response_body == "audit body"


@pytest.mark.asyncio
async def test_succeeded_terminal_trigger_blocks_status_transition_away(
    repo, mysql_pool
):
    """Verify the BEFORE UPDATE trigger rejects a transition that would
    un-succeed a row (matches the Postgres succeeded-terminal trigger)."""
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_terminal_user",
            namespace="default",
        )
        [delivery_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "terminal"},
            owner_id="webhook_repo_terminal_user",
            namespace="default",
        )
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None
    async with _tx(mysql_pool) as tx:
        await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token=token,
            outcome=WebhookDeliveryOutcome(
                succeeded=True,
                response_status=200,
                response_body="ok",
            ),
            max_attempts=3,
            backoff_schedule=(60, 300),
        )

    # A subsequent UPDATE that would move the row back to a live status
    # must fail at the engine boundary.
    async with mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:
                await cur.execute(
                    "UPDATE webhook_deliveries SET status = 'pending' WHERE id = %s",
                    (delivery_id,),
                )
            message = str(excinfo.value).lower()
            assert "cannot transition status away from succeeded" in message


@pytest.mark.asyncio
async def test_unique_live_chain_attempt_index_blocks_duplicate_live_rows(
    repo, mysql_pool
):
    """The generated live_chain_key unique index must reject a second
    live attempt_num=N row in the same chain (parity with the Postgres
    ``uq_webhook_deliveries_live_chain_attempt`` partial unique index).

    The trick: both rows must be ``live`` (status IN ('pending','retrying')
    AND superseded=0) so the generated ``live_chain_key`` is non-NULL
    for both. Terminal rows produce NULL for that column and are free
    to repeat, matching the partial-index semantics.
    """
    sub_id = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        await repo.create_subscription(
            tx,
            subscription_id=sub_id,
            url="https://example.com/hook",
            events=("memory.created",),
            secret="s",
            description=None,
            owner_id="webhook_repo_unique_user",
            namespace="default",
        )
        [first_id] = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "unique"},
            owner_id="webhook_repo_unique_user",
            namespace="default",
        )

    # Move the first attempt into a live "retrying" status (no lease so
    # the partial unique key still computes the same value).
    token = str(uuid.uuid4())
    async with _tx(mysql_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=first_id,
            lease_token=token,
            lease_seconds=30,
            max_attempts=4,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None
    # Release the lease so we don't trip the lease-token check; the row
    # remains in 'retrying' (a live state) and live_chain_key stays
    # non-NULL.
    async with _tx(mysql_pool) as tx:
        released = await repo.release_delivery_claim(
            tx,
            delivery_id=first_id,
            lease_token=token,
        )
    assert released is True

    # A second live attempt_num=1 row in the same chain must fail.
    # Extract the exact payload + payload_hash from the first row so the
    # generated live_chain_key matches the first row's key byte-for-byte.
    async with mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT payload, payload_hash FROM webhook_deliveries WHERE id = %s",
                (first_id,),
            )
            row = await cur.fetchone()
        payload = row[0]
        payload_hash = row[1]

    async with mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:
                await cur.execute(
                    """
                    INSERT INTO webhook_deliveries
                      (id, subscription_id, event_type, payload, payload_hash,
                       attempt_num, status, scheduled_at, writer_revision)
                    VALUES (%s, %s, 'memory.created', %s, %s,
                            1, 'pending', NOW(6), %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        sub_id,
                        payload,
                        payload_hash,
                        NEW_CODE_WRITER_REVISION,
                    ),
                )
            assert "Duplicate entry" in str(excinfo.value)
        await conn.commit()
