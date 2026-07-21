from __future__ import annotations

import pytest

from mnemos.webhooks import outbox


class _FakeConn:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchval_calls = 0

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((sql, args))
        return [
            {
                "id": "sub-1",
                "url": "https://example.com/1",
                "events": ["memory.created"],
                "secret": "secret",
                "owner_id": "owner",
                "namespace": "ns",
            },
            {
                "id": "sub-2",
                "url": "https://example.com/2",
                "events": ["memory.created"],
                "secret": "secret",
                "owner_id": "owner",
                "namespace": "ns",
            },
            {
                "id": "sub-3",
                "url": "https://example.com/3",
                "events": ["memory.created"],
                "secret": "secret",
                "owner_id": "owner",
                "namespace": "ns",
            },
        ]

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 3"

    async def fetchval(self, *_args: object) -> object:
        self.fetchval_calls += 1
        raise AssertionError("webhook outbox inserts must be batched through execute")


@pytest.mark.asyncio
async def test_dispatch_on_conn_batches_inserts_and_schedules_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn()
    scheduled: list[object] = []
    publish_calls: list[str] = []

    def _schedule(coro: object) -> object:
        scheduled.append(coro)
        return object()

    async def _publish_delivery_queued(**_kwargs: object) -> None:
        publish_calls.append("delivery_queued")

    async def _publish_outbox_insert(**_kwargs: object) -> None:
        publish_calls.append("outbox_insert")

    monkeypatch.setattr(outbox.lifecycle, "_schedule_background", _schedule)
    monkeypatch.setattr(outbox, "publish_delivery_queued", _publish_delivery_queued)
    monkeypatch.setattr(outbox, "publish_webhook_outbox_insert", _publish_outbox_insert)

    delivery_ids = await outbox._dispatch_on_conn(
        conn,
        "memory.created",
        {"memory_id": "mem_1"},
        owner_id="owner",
        namespace="ns",
    )

    assert len(delivery_ids) == 3
    assert conn.fetchval_calls == 0
    assert len(conn.fetch_calls) == 1
    assert len(conn.execute_calls) == 1
    insert_sql, insert_args = conn.execute_calls[0]
    assert "INSERT INTO webhook_deliveries" in insert_sql
    assert insert_sql.count("::uuid") == 3
    assert len(insert_args) == 18
    assert [insert_args[index] for index in (1, 7, 13)] == ["sub-1", "sub-2", "sub-3"]
    assert scheduled
    assert publish_calls == []

    await scheduled[0]
    assert publish_calls == [
        "delivery_queued",
        "outbox_insert",
        "delivery_queued",
        "outbox_insert",
        "delivery_queued",
        "outbox_insert",
    ]
