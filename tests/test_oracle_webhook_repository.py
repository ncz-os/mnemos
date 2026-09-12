"""Integration test for ``OracleWebhookRepository``.

Item 6 of the ABC webhook persistence sequence. Verifies that the new
``WebhookRepository`` ABC methods on ``mnemos.persistence.oracle`` produce
behavior consistent with ``PostgresWebhookRepository`` against the same
logical schema (lease acquisition, ``SKIP LOCKED`` recovery,
writer-revision fence, retry-chain convergence, repair sweep, idempotent
finalize).

Requires a real Oracle instance reachable via ``MNEMOS_TEST_ORACLE``. The
test applies a minimal webhook-only schema (webhook_subscriptions +
webhook_deliveries + the v3.5-equivalent unique indexes / trigger) on
first run so it works without the full Oracle schema. When
``MNEMOS_TEST_ORACLE`` is unset the test is skipped — same pattern as
``tests/test_postgres_webhook_repository.py``.

The Oracle DSN format is ``oracle://user:pass@host:port/service``
matching ``OracleBackend.open``. Operator must have CREATE TABLE /
CREATE TRIGGER privilege on the target schema.
"""

from __future__ import annotations

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
from mnemos.persistence.oracle import OracleWebhookRepository

DSN = os.environ.get("MNEMOS_TEST_ORACLE")

# Driver availability check (deferred — see test fixtures). The
# ``import oracledb`` inside fixtures performs the real gate; this
# block exists only so the static skipif mark can render a helpful
# message before collection begins.
try:
    import oracledb  # type: ignore[import-not-found]  # noqa: F401

    _ORACLE_DRIVER_AVAILABLE = True
except ModuleNotFoundError:
    _ORACLE_DRIVER_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(
        not DSN,
        reason=(
            "set MNEMOS_TEST_ORACLE=oracle://user:pass@host:port/service to run "
            "webhook integration tests against a live Oracle instance"
        ),
    ),
    pytest.mark.skipif(
        not _ORACLE_DRIVER_AVAILABLE,
        reason=(
            "python-oracledb (oracledb) driver is not installed in this "
            "environment — install via 'pip install oracledb' to run this test"
        ),
    ),
]


WEBHOOK_SCHEMA_DDL: tuple[str, ...] = (
    # Subscription table — VARCHAR2(64) for the synthetic UUID-shape ids the
    # test inserts (Oracle thin mode doesn't auto-coerce UUID to RAW).
    """
    CREATE TABLE webhook_subscriptions (
        id              VARCHAR2(64)   PRIMARY KEY,
        url             VARCHAR2(2000) NOT NULL,
        events          CLOB           NOT NULL,
        secret          VARCHAR2(2000) NOT NULL,
        description     VARCHAR2(2000),
        owner_id        VARCHAR2(256)  NOT NULL,
        namespace       VARCHAR2(256)  NOT NULL,
        created         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        revoked         NUMBER(1)      DEFAULT 0 NOT NULL,
        revoked_at      TIMESTAMP WITH TIME ZONE,
        CONSTRAINT webhook_url_format
            CHECK (url LIKE 'http://%' OR url LIKE 'https://%')
    )
    """,
    "CREATE INDEX idx_webhook_subscriptions_owner "
    "    ON webhook_subscriptions(owner_id, namespace)",
    # Deliveries table — same logical shape as Postgres v3.5, adapted to
    # Oracle TIMESTAMP WITH TIME ZONE + NUMBER(1) for the BOOLEAN-ish
    # ``superseded`` flag (Oracle has no native BOOLEAN pre-23c).
    """
    CREATE TABLE webhook_deliveries (
        id               VARCHAR2(64)    PRIMARY KEY,
        subscription_id  VARCHAR2(64)    NOT NULL,
        event_type       VARCHAR2(256)   NOT NULL,
        payload          CLOB            NOT NULL,
        payload_hash     VARCHAR2(64)    NOT NULL,
        attempt_num      NUMBER(10)      DEFAULT 1 NOT NULL,
        status           VARCHAR2(32)    DEFAULT 'pending' NOT NULL,
        response_status  NUMBER(10),
        response_body    CLOB,
        error            VARCHAR2(2000),
        scheduled_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        delivered_at     TIMESTAMP WITH TIME ZONE,
        created          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        lease_token      VARCHAR2(64),
        lease_expires_at TIMESTAMP WITH TIME ZONE,
        writer_revision  NUMBER(10)      DEFAULT 0 NOT NULL,
        status_updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        superseded       NUMBER(1)       DEFAULT 0 NOT NULL
    )
    """,
    "CREATE INDEX idx_webhook_deliveries_subscription "
    "    ON webhook_deliveries(subscription_id, created DESC)",
    "CREATE INDEX idx_webhook_deliveries_pending "
    "    ON webhook_deliveries(scheduled_at)",
    "CREATE INDEX idx_webhook_deliveries_lease_expires_at "
    "    ON webhook_deliveries(lease_expires_at)",
    # The two unique indexes that encode the v3.5 chain invariant.
    # Oracle doesn't support partial indexes directly — we use the
    # ``DBMS_LOB.INSTR(events, ...) > 0`` style NULL-trick: rows that
    # don't need to participate store NULL in the indexed expression
    # column, leaving the unique constraint effectively partial. We use
    # functional unique indexes over CASE expressions that evaluate to
    # NULL when the row is terminal/superseded (Oracle treats NULL
    # values in unique indexes as non-conflicting).
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
    # Oracle BEFORE UPDATE trigger that mirrors Postgres's
    # ``webhook_deliveries_enforce_succeeded_terminal``: never transition
    # away from 'succeeded' to a non-succeeded status.
    """
    CREATE OR REPLACE TRIGGER trg_webhook_deliveries_succeeded_terminal
    BEFORE UPDATE ON webhook_deliveries
    FOR EACH ROW
    BEGIN
        IF :OLD.status = 'succeeded' AND :NEW.status <> 'succeeded' THEN
            RAISE_APPLICATION_ERROR(
                -20001,
                'webhook_deliveries: cannot transition status away from succeeded '
                );
        END IF;
    END;
    """,
    # Mirrors the Postgres status-updated-at trigger: only auto-bump on
    # actual status change. We use SYSTIMESTAMP inside the trigger body.
    """
    CREATE OR REPLACE TRIGGER trg_webhook_deliveries_status_updated_at
    BEFORE UPDATE ON webhook_deliveries
    FOR EACH ROW
    BEGIN
        IF :OLD.status IS NULL OR :OLD.status <> :NEW.status THEN
            :NEW.status_updated_at := SYSTIMESTAMP;
        END IF;
    END;
    """,
)


def _split_oracle_statements(sql: str) -> list[str]:
    """Split a multi-statement DDL on ``;`` outside string literals.

    Oracle's python-oracledb thin client does not accept multiple
    statements in one ``execute()`` call, so we must hand it one
    statement at a time. The schema DDL here doesn't contain any
    stored-procedure bodies that wrap with ``BEGIN ... END`` carrying
    their own ``;``, so a naive ``split(';')`` is sufficient — but we
    still strip ``--`` comments defensively.
    """
    out: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    for ch in sql:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == ";" and not in_single and not in_double:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


async def _ensure_webhook_schema(pool: Any) -> None:
    """Idempotent schema provisioning.

    The fixture using this is function-scoped (one call per test), so
    CREATE TABLE/INDEX statements hit ORA-00955 ("name is already used
    by an existing object") on every test after the first. Oracle has
    no ``CREATE TABLE IF NOT EXISTS`` pre-23c, so swallow ORA-00955
    specifically (already-exists) and let any other error propagate —
    that's the Oracle-idiomatic equivalent of IF NOT EXISTS here.
    """
    import oracledb as _oracledb

    async with pool.acquire() as conn:
        cur = conn.cursor()
        try:
            # Each WEBHOOK_SCHEMA_DDL entry is already exactly one
            # statement (including the two CREATE TRIGGER bodies, whose
            # internal BEGIN...END semicolons are NOT statement
            # separators) — do not run these through
            # _split_oracle_statements, which mis-splits trigger bodies
            # on their internal ``;`` and produces ORA-00900.
            for ddl in WEBHOOK_SCHEMA_DDL:
                try:
                    await cur.execute(ddl)
                except _oracledb.DatabaseError as exc:
                    (error_obj,) = exc.args
                    if getattr(error_obj, "code", None) != 955:
                        raise
        finally:
            cur.close()
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


def _parse_oracle_dsn(dsn: str) -> dict[str, Any]:
    """Parse ``oracle://user:pass@host:port/service`` into kwargs."""
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    if parsed.scheme != "oracle":
        raise ValueError(f"unexpected scheme {parsed.scheme!r}; need 'oracle://'")
    return {
        "user": parsed.username,
        "password": parsed.password,
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 1521,
        "service_name": (parsed.path or "/").lstrip("/") or "FREEPDB1",
    }


@pytest_asyncio.fixture
async def oracle_pool() -> AsyncIterator[Any]:
    """Yield a REAL production pool (mnemos.persistence.oracle.create_oracle_pool)
    and provision the webhook schema.

    Uses the actual production pool-creation helper, not an ad-hoc bare
    pool, so this test exercises the real session_callback (NLS pinning +
    the UTC TIME_ZONE pin) -- a bare pool silently skips both and can hide
    real timezone-dependent bugs in the timestamp comparisons this test
    is supposed to be verifying.
    """
    pytest.importorskip("oracledb")  # noqa: F811
    from mnemos.persistence.oracle import create_oracle_pool

    pool = await create_oracle_pool(DSN, min_size=1, max_size=4
    )
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
                cur.close()
            await conn.commit()
        # python-oracledb's async pool exposes ``close()`` which returns
        # a coroutine that drains + closes all live connections.
        await pool.close()


@pytest_asyncio.fixture
async def repo(oracle_pool) -> OracleWebhookRepository:
    """Yield a bare ``OracleWebhookRepository`` (no Backend wrapper needed).

    The repository's public API only takes a ``Transaction`` handle, so
    we instantiate it directly. The transaction fixture below does the
    real Oracle pool acquire + transaction wrap.
    """
    return OracleWebhookRepository()


@asynccontextmanager
async def _tx(oracle_pool: Any):
    """Yield an Oracle transaction wrapped around a real pool acquire.

    Uses the internal ``_OracleTransaction`` from
    ``mnemos.persistence.oracle`` so the ``OracleWebhookRepository``
    methods get a fully-functional ``Transaction`` (with
    ``_conn_from_tx`` resolution).
    """
    from mnemos.persistence.oracle import _OracleTransaction

    async with oracle_pool.acquire() as conn:
        # oracledb has no explicit conn.begin() -- a transaction starts
        # implicitly with the first DML statement and ends at commit/rollback.
        tx = _OracleTransaction(conn)
        try:
            yield tx
            if not tx.closed:
                await tx.commit()
        except BaseException:
            if not tx.closed:
                await tx.rollback()
            raise


async def _direct_execute(oracle_pool: Any, sql: str, *args: Any, **kwargs: Any) -> int:
    """Run raw DML against the pool outside a fixture transaction.

    Accepts either positional binds (for ``:1``-style SQL) or named binds
    as kwargs (for ``:name``-style SQL, which every caller in this file
    actually uses) -- Oracle SQL here is written with named binds.
    """
    async with oracle_pool.acquire() as conn:
        cur = conn.cursor()
        try:
            await cur.execute(sql, kwargs or (args or None))
            affected = int(getattr(cur, "rowcount", 0) or 0)
        finally:
            cur.close()
        await conn.commit()
    return affected


# ─────────────────────────────────────────────────────────────────────────
# subscription surface
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get_subscription_roundtrip(repo, oracle_pool):
    subscription_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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

    async with _tx(oracle_pool) as tx:
        fetched = await repo.get_subscription(
            tx,
            subscription_id=subscription_id,
            owner_id="webhook_repo_create_user",
            namespace="default",
        )
    assert fetched == record


@pytest.mark.asyncio
async def test_list_subscriptions_partial_scope_is_rejected(repo, oracle_pool):
    with pytest.raises(ValueError, match="both"):
        async with _tx(oracle_pool) as tx:
            await repo.list_subscriptions(
                tx,
                owner_id="alice",
                namespace=None,
                include_revoked=False,
                limit=10,
            )
    with pytest.raises(ValueError, match="both"):
        async with _tx(oracle_pool) as tx:
            await repo.list_subscriptions(
                tx,
                owner_id=None,
                namespace="ns1",
                include_revoked=False,
                limit=10,
            )


@pytest.mark.asyncio
async def test_list_subscriptions_respects_revoked_flag_and_owner_scope(
    repo, oracle_pool
):
    sub_a = str(uuid.uuid4())
    sub_b = str(uuid.uuid4())
    sub_c = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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

    async with _tx(oracle_pool) as tx:
        await repo.revoke_subscription(
            tx,
            subscription_id=sub_b,
            owner_id="webhook_repo_list_user",
            namespace="ns1",
        )

    async with _tx(oracle_pool) as tx:
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
async def test_revoke_subscription_is_idempotent_and_scoped(repo, oracle_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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

    async with _tx(oracle_pool) as tx:
        first = await repo.revoke_subscription(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_revoke_user",
            namespace="default",
        )
    assert first is True

    async with _tx(oracle_pool) as tx:
        second = await repo.revoke_subscription(
            tx,
            subscription_id=sub_id,
            owner_id="webhook_repo_revoke_user",
            namespace="default",
        )
    assert second is False

    # Wrong owner/namespace — must not return True.
    async with _tx(oracle_pool) as tx:
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
async def test_dispatch_event_creates_pending_deliveries(repo, oracle_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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

    async with _tx(oracle_pool) as tx:
        _intents = await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "abc", "count": 7},
            owner_id="webhook_repo_dispatch_user",
            namespace="default",)
        delivery_ids = [intent.delivery_id for intent in _intents]
    assert len(delivery_ids) == 1

    async with _tx(oracle_pool) as tx:
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
    repo, oracle_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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

    async with _tx(oracle_pool) as tx:
        [delivery_id] = [intent.delivery_id for intent in await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "x"},
            owner_id="webhook_repo_claim_user",
            namespace="default",)]

    async with _tx(oracle_pool) as tx:
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

    # Second claim with a different token must return None while the
    # first lease is still live.
    async with _tx(oracle_pool) as tx:
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
async def test_claim_due_deliveries_returns_pending_in_order(repo, oracle_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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

    async with _tx(oracle_pool) as tx:
        [d_id_a] = [intent.delivery_id for intent in await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "abc"},
            owner_id="webhook_repo_due_user",
            namespace="default",)]

    # dispatch_event returns one delivery per matching subscription; in
    # this test we only have one sub so we get one. Generate the other
    # two via direct inserts with explicit attempt_num + scheduled_at.
    extra1 = str(uuid.uuid4())
    extra2 = str(uuid.uuid4())
    await _direct_execute(
        oracle_pool,
        """
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload, payload_hash,
             attempt_num, status, scheduled_at, writer_revision, status_updated_at)
        VALUES
            (:id, :sub, 'memory.created', :payload, :phash,
             1, 'pending', SYSTIMESTAMP - INTERVAL '0.01' SECOND, :wrev, SYSTIMESTAMP)
        """,
        id=extra1,
        sub=sub_id,
        payload="payload-1",
        phash="hash-1",
        wrev=NEW_CODE_WRITER_REVISION,
    )
    await _direct_execute(
        oracle_pool,
        """
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload, payload_hash,
             attempt_num, status, scheduled_at, writer_revision, status_updated_at)
        VALUES
            (:id, :sub, 'memory.created', :payload, :phash,
             1, 'pending', SYSTIMESTAMP + INTERVAL '1' HOUR, :wrev, SYSTIMESTAMP)
        """,
        id=extra2,
        sub=sub_id,
        payload="payload-2",
        phash="hash-2",
        wrev=NEW_CODE_WRITER_REVISION,
    )

    async with _tx(oracle_pool) as tx:
        claims = await repo.claim_due_deliveries(
            tx,
            lease_token="due-lease",
            limit=10,
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    claimed_ids = {c.delivery.id for c in claims}
    # extra1 + d_id_a are due; extra2 is in the future and must be skipped.
    assert extra2 not in claimed_ids
    assert {d_id_a, extra1}.issubset(claimed_ids)


# ─────────────────────────────────────────────────────────────────────────
# finalize + chain repair
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_success_marks_row_succeeded_and_returns_applied(
    repo, oracle_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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

    async with _tx(oracle_pool) as tx:
        [delivery_id] = [intent.delivery_id for intent in await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "abc"},
            owner_id="webhook_repo_fin_user",
            namespace="default",)]

    async with _tx(oracle_pool) as tx:
        claim = await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="lease-fin",
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )
    assert claim is not None

    async with _tx(oracle_pool) as tx:
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

    async with _tx(oracle_pool) as tx:
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
async def test_finalize_wrong_lease_token_returns_not_applied(repo, oracle_pool):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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
    async with _tx(oracle_pool) as tx:
        [delivery_id] = [intent.delivery_id for intent in await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "x"},
            owner_id="webhook_repo_fin2_user",
            namespace="default",)]
    async with _tx(oracle_pool) as tx:
        await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="real-lease",
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )

    async with _tx(oracle_pool) as tx:
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
    repo, oracle_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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
    async with _tx(oracle_pool) as tx:
        [delivery_id] = [intent.delivery_id for intent in await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "x"},
            owner_id="webhook_repo_fin3_user",
            namespace="default",)]

    async with _tx(oracle_pool) as tx:
        await repo.claim_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="retry-lease",
            lease_seconds=30,
            max_attempts=3,
            writer_revision=NEW_CODE_WRITER_REVISION,
        )

    async with _tx(oracle_pool) as tx:
        result = await repo.finalize_delivery(
            tx,
            delivery_id=delivery_id,
            lease_token="retry-lease",
            outcome=WebhookDeliveryOutcome(
                succeeded=False, response_status=503, error="upstream-down"
            ),
            max_attempts=3,
            backoff_schedule=[1, 2, 5],
        )
    assert result.applied is True
    assert result.successor_delivery_id is not None
    assert result.successor_delivery_id != delivery_id

    async with _tx(oracle_pool) as tx:
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
    repo, oracle_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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
    # first is currently ``retrying`` with attempt_num=1 and live; the
    # successor at attempt_num=2 makes first obsolete. Repair must
    # terminalize first.
    await _direct_execute(
        oracle_pool,
        """
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload, payload_hash,
             attempt_num, status, scheduled_at, writer_revision, status_updated_at)
        VALUES
            (:id, :sub, 'memory.created', :payload, :phash,
             1, 'retrying', SYSTIMESTAMP, :wrev, SYSTIMESTAMP)
        """,
        id=first_id,
        sub=sub_id,
        payload=body,
        phash=body_hash,
        wrev=NEW_CODE_WRITER_REVISION,
    )
    await _direct_execute(
        oracle_pool,
        """
        INSERT INTO webhook_deliveries
            (id, subscription_id, event_type, payload, payload_hash,
             attempt_num, status, scheduled_at, writer_revision, status_updated_at)
        VALUES
            (:id, :sub, 'memory.created', :payload, :phash,
             2, 'pending', SYSTIMESTAMP, :wrev, SYSTIMESTAMP)
        """,
        id=successor_id,
        sub=sub_id,
        payload=body,
        phash=body_hash,
        wrev=NEW_CODE_WRITER_REVISION,
    )

    async with _tx(oracle_pool) as tx:
        repaired = await repo.repair_delivery_chains(tx)

    assert repaired >= 1
    async with _tx(oracle_pool) as tx:
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
    repo, oracle_pool
):
    sub_id = str(uuid.uuid4())
    async with _tx(oracle_pool) as tx:
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
    async with _tx(oracle_pool) as tx:
        [delivery_id] = [intent.delivery_id for intent in await repo.dispatch_event(
            tx,
            "memory.created",
            {"memory_id": "body"},
            owner_id="webhook_repo_body_user",
            namespace="default",)]
    body = '{"hello":"world"}'
    async with _tx(oracle_pool) as tx:
        stored = await repo.store_delivery_response_body(
            tx,
            delivery_id=delivery_id,
            response_body=body,
        )
    assert stored is True

    async with _tx(oracle_pool) as tx:
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
