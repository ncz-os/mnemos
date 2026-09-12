"""Outbox backend-delegation regression (item 7).

Pre-item-7 this module tested the raw-SQL batch insert inside
``outbox._dispatch_on_conn`` and the post-commit NATS scheduling
helper. After item 7, ``outbox._dispatch_on_conn`` is a thin pass
through to ``backend.webhooks.dispatch_event``, so the test asserts
the new behavior: the outbox delegate returns the delivery ids the
backend produced and forwards the caller's args.
"""
from __future__ import annotations

import pytest


class _FakeBackendWebhooks:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._delivery_ids: list[str] = []

    def configure_delivery_ids(self, ids: list[str]) -> None:
        self._delivery_ids = list(ids)

    async def dispatch_event(
        self,
        conn,
        event_type,
        payload,
        *,
        owner_id=None,
        namespace=None,
    ):
        self.calls.append(
            (
                (conn, event_type, payload),
                {"owner_id": owner_id, "namespace": namespace},
            )
        )
        from mnemos.persistence.base import WebhookDeliveryIntent

        return [
            WebhookDeliveryIntent(
                delivery_id=did,
                subscription_id=f"sub-{i}",
                url="https://example.com/hook",
                namespace=namespace or "ns",
                owner_id=owner_id or "owner",
            )
            for i, did in enumerate(self._delivery_ids)
        ]


class _FakeBackend:
    def __init__(self) -> None:
        self.webhooks = _FakeBackendWebhooks()

    def __getattr__(self, name: str):
        # Provide a stub for any unexpected attr access (matches the
        # real backend's surface area)
        return _Stub()


class _Stub:
    def __call__(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_dispatch_on_conn_delegates_to_backend_with_correct_kwargs(monkeypatch) -> None:
    """``outbox._dispatch_on_conn`` must call ``backend.webhooks.dispatch_event``
    with the caller's connection and forward owner_id / namespace."""
    from mnemos.core import lifecycle
    from mnemos.webhooks import outbox

    backend = _FakeBackend()
    backend.webhooks.configure_delivery_ids(["delivery-a", "delivery-b", "delivery-c"])
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)

    conn = object()  # raw connection handle (would be asyncpg.Connection)

    delivery_ids = await outbox._dispatch_on_conn(  # noqa: WPS433
        conn,
        "memory.created",
        {"memory_id": "mem_1"},
        owner_id="owner",
        namespace="ns",
    )

    assert delivery_ids == ["delivery-a", "delivery-b", "delivery-c"]
    assert len(backend.webhooks.calls) == 1
    forwarded_args, forwarded_kwargs = backend.webhooks.calls[0]
    forwarded_conn, forwarded_event, forwarded_payload = forwarded_args
    assert forwarded_conn is conn
    assert forwarded_event == "memory.created"
    assert forwarded_payload == {"memory_id": "mem_1"}
    assert forwarded_kwargs == {"owner_id": "owner", "namespace": "ns"}


@pytest.mark.asyncio
async def test_dispatch_on_conn_returns_empty_when_no_subscriptions(monkeypatch) -> None:
    """Empty ``dispatch_event`` result must round-trip as an empty list."""
    from mnemos.core import lifecycle
    from mnemos.webhooks import outbox

    backend = _FakeBackend()
    backend.webhooks.configure_delivery_ids([])
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)

    delivery_ids = await outbox._dispatch_on_conn(  # noqa: WPS433
        object(),
        "memory.created",
        {"memory_id": "mem_2"},
        owner_id="owner",
        namespace="ns",
    )
    assert delivery_ids == []
