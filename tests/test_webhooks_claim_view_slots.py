"""Regression test for the webhook claim view's ``__slots__`` contract.

The F-06 slice-3 branch (9ef832f4) dropped ``__slots__`` from
``mnemos.webhooks.lease._RecordView`` because the ABC claim path assigns
``lease_expires_at`` / ``claim_db_now`` onto the view it hands to the
sender — and a slotted class rejects that assignment with an
``AttributeError``, which turned every ABC claim on every backend into a
delivery crash.

Dropping ``__slots__`` was not necessary: the two database-clock values
live on the ``WebhookDeliveryClaim`` wrapper, so the claim path can carry
them on a dedicated slotted subclass
(``lease._ClaimedRecordView``) and leave ``_RecordView`` slotted exactly as
it was.

The first test in this module is the regression test: it fails on the
pre-slice tree (master) with ``AttributeError: '_RecordView' object has no
attribute 'lease_expires_at'`` and passes once the claim path uses the
slotted claim view.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_abc_claim_attaches_the_claim_clock_values_to_the_view(
    monkeypatch, tmp_path
) -> None:
    """An ABC claim on the SQLite backend must not raise, and the claimed
    delivery view must expose the claim's ``lease_expires_at`` and
    ``claim_db_now``.

    On the pre-slice tree this raises ``AttributeError`` from the
    assignment onto the slotted ``_RecordView`` — the defect the
    ``__slots__`` removal was working around.
    """
    import mnemos.core.lifecycle as lifecycle
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.webhooks import lease as webhook_lease

    backend = SqliteBackend(tmp_path / "claim_view_slots.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
        monkeypatch.setattr(lifecycle, "_pool", None)

        repo = backend.webhooks
        subscription_id = str(uuid.uuid4())
        async with backend.transactional() as tx:
            await repo.create_subscription(
                tx,
                subscription_id=subscription_id,
                url="https://example.com/hook",
                events=("memory.created",),
                secret="secret",
                description="claim view slots regression",
                owner_id="owner",
                namespace="default",
            )
            intents = await repo.dispatch_event(
                tx,
                "memory.created",
                {"memory_id": "slots"},
                owner_id="owner",
                namespace="default",
            )
        delivery_id = intents[0].delivery_id

        claimed = await webhook_lease._claim_delivery(
            None,
            delivery_id,
            lease_token=str(uuid.uuid4()),
        )
        assert claimed is not None
        assert claimed.delivery["id"] == delivery_id

        view = claimed.delivery
        assert getattr(view, "lease_expires_at") is not None
        assert getattr(view, "claim_db_now") is not None
        # The values the sender reads back are the claim's own clock values.
        remaining = webhook_lease._claim_remaining_send_window_seconds(
            view,
            pre_claim_monotonic=time.monotonic(),
        )
        assert remaining > 0
    finally:
        await backend.close()


def test_the_base_record_view_keeps_its_slots() -> None:
    """``_RecordView`` is still a slotted class (no instance ``__dict__``).

    The claim clock values are carried by ``_ClaimedRecordView``, so the
    base view never has to give up ``__slots__``.
    """
    from mnemos.webhooks import lease as webhook_lease

    assert webhook_lease._RecordView.__slots__ == ("_record",)
    view = webhook_lease._RecordView(SimpleNamespace(id="x"))
    assert not hasattr(view, "__dict__")
    assert issubclass(webhook_lease._ClaimedRecordView, webhook_lease._RecordView)
    assert set(webhook_lease._ClaimedRecordView.__slots__) == {
        "lease_expires_at",
        "claim_db_now",
    }
    claimed_view = webhook_lease._ClaimedRecordView(SimpleNamespace(id="x"))
    assert not hasattr(claimed_view, "__dict__")
