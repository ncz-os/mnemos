"""Real SQLite coverage for the backend-neutral webhook repository contract."""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mnemos.core import webhook_constants
from mnemos.persistence.base import WebhookDeliveryOutcome
from mnemos.persistence.sqlite import SqliteBackend, _execute


@pytest.mark.asyncio
async def test_sqlite_webhook_claim_retry_finalize_and_repair(tmp_path, monkeypatch):
    import mnemos.nats.webhook_events as webhook_events

    monkeypatch.setattr(webhook_events, "publish_delivery_queued", AsyncMock())
    backend = SqliteBackend(tmp_path / "webhooks.sqlite3", SimpleNamespace())
    await backend.open()
    repo = backend._webhooks
    subscription_id = str(uuid.uuid4())

    try:
        async with backend.transactional() as tx:
            subscription = await repo.create_subscription(
                tx,
                subscription_id=subscription_id,
                url="https://example.com/hook",
                events=("memory.created",),
                secret="secret",
                description="SQLite integration",
                owner_id="owner",
                namespace="default",
            )
            delivery_ids = await repo.dispatch_event(
                tx,
                "memory.created",
                {"memory_id": "mem-1"},
                owner_id="owner",
                namespace="default",
            )

        assert subscription.description == "SQLite integration"
        assert len(delivery_ids) == 1
        delivery_id = delivery_ids[0]

        async with backend.transactional() as tx:
            assert (
                await repo.claim_delivery(
                    tx,
                    delivery_id=delivery_id,
                    lease_token="wrong-writer-revision",
                    lease_seconds=30,
                    max_attempts=3,
                    writer_revision=webhook_constants.NEW_CODE_WRITER_REVISION + 1,
                )
                is None
            )
            first_claim = await repo.claim_delivery(
                tx,
                delivery_id=delivery_id,
                lease_token="lease-1",
                lease_seconds=30,
                max_attempts=3,
                writer_revision=webhook_constants.NEW_CODE_WRITER_REVISION,
            )
            assert first_claim is not None
            assert first_claim.delivery.status == "retrying"
            assert (
                await repo.guard_delivery_claim(
                    tx,
                    delivery_id=delivery_id,
                    lease_token="lease-1",
                )
                is True
            )
            assert (
                await repo.claim_delivery(
                    tx,
                    delivery_id=delivery_id,
                    lease_token="lease-2",
                    lease_seconds=30,
                    max_attempts=3,
                    writer_revision=webhook_constants.NEW_CODE_WRITER_REVISION,
                )
                is None
            )

            # A second writer may reclaim the row after the database lease expires.
            await _execute(
                tx.conn,
                "UPDATE webhook_deliveries SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (delivery_id,),
            )
            reclaimed = await repo.claim_delivery(
                tx,
                delivery_id=delivery_id,
                lease_token="lease-2",
                lease_seconds=30,
                max_attempts=3,
                writer_revision=webhook_constants.NEW_CODE_WRITER_REVISION,
            )
            assert reclaimed is not None
            assert reclaimed.lease_token == "lease-2"
            assert (
                await repo.release_delivery_claim(
                    tx,
                    delivery_id=delivery_id,
                    lease_token="lease-2",
                )
                is True
            )
            reclaimed = await repo.claim_delivery(
                tx,
                delivery_id=delivery_id,
                lease_token="lease-2",
                lease_seconds=30,
                max_attempts=3,
                writer_revision=webhook_constants.NEW_CODE_WRITER_REVISION,
            )
            assert reclaimed is not None

            retry = await repo.finalize_delivery(
                tx,
                delivery_id=delivery_id,
                lease_token="lease-2",
                outcome=WebhookDeliveryOutcome(
                    succeeded=False,
                    response_status=503,
                    response_body="busy",
                    error="upstream unavailable",
                ),
                max_attempts=3,
                backoff_schedule=(1, 1),
            )
            assert retry.applied is True
            assert retry.status == "abandoned"
            assert retry.successor_delivery_id is not None

        async with backend.transactional() as tx:
            rows = await repo.list_deliveries(
                tx,
                subscription_id=subscription_id,
                owner_id="owner",
                namespace="default",
                limit=20,
            )
            assert sorted(row.attempt_num for row in rows) == [1, 2]
            successor_id = retry.successor_delivery_id
            assert successor_id is not None
            await _execute(
                tx.conn,
                "UPDATE webhook_deliveries SET scheduled_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (successor_id,),
            )
            due = await repo.claim_due_deliveries(
                tx,
                lease_token="lease-3",
                limit=10,
                lease_seconds=30,
                max_attempts=3,
                writer_revision=webhook_constants.NEW_CODE_WRITER_REVISION,
            )
            assert [claim.delivery.id for claim in due] == [successor_id]
            assert due[0].delivery.attempt_num == 2
            success = await repo.finalize_delivery(
                tx,
                delivery_id=successor_id,
                lease_token="lease-3",
                outcome=WebhookDeliveryOutcome(
                    succeeded=True,
                    response_status=204,
                    response_body="ok",
                ),
                max_attempts=3,
                backoff_schedule=(1, 1),
            )
            assert success == type(success)(applied=True, status="succeeded")
            assert (
                await repo.store_delivery_response_body(
                    tx,
                    delivery_id=successor_id,
                    response_body="audit body",
                )
                is True
            )

        payload = json.dumps({"repair": True}, separators=(",", ":"), sort_keys=True)
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        older_id = str(uuid.uuid4())
        newer_id = str(uuid.uuid4())
        async with backend.transactional() as tx:
            await _execute(
                tx.conn,
                "INSERT INTO webhook_deliveries "
                "(id, subscription_id, event_type, payload, payload_hash, attempt_num, "
                "status, scheduled_at, writer_revision) VALUES (?, ?, ?, ?, ?, 1, 'pending', ?, 1)",
                (older_id, subscription_id, "repair.test", payload, payload_hash, "2000-01-01T00:00:00+00:00"),
            )
            await _execute(
                tx.conn,
                "INSERT INTO webhook_deliveries "
                "(id, subscription_id, event_type, payload, payload_hash, attempt_num, "
                "status, scheduled_at, writer_revision) VALUES (?, ?, ?, ?, ?, 2, 'pending', ?, 1)",
                (newer_id, subscription_id, "repair.test", payload, payload_hash, "2000-01-01T00:00:00+00:00"),
            )
            assert await repo.repair_delivery_chains(tx) == 1
            repaired = await repo.list_deliveries(
                tx,
                subscription_id=subscription_id,
                owner_id=None,
                namespace=None,
                limit=20,
            )
            repaired_by_id = {row.id: row for row in repaired}
            assert repaired_by_id[older_id].status == "abandoned"
            assert repaired_by_id[older_id].superseded is True
            assert repaired_by_id[newer_id].status == "pending"
            assert repaired_by_id[successor_id].status == "succeeded"
            assert repaired_by_id[successor_id].response_body == "audit body"
    finally:
        await backend.close()
