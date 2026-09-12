"""Static contract tests for the staged backend-neutral webhook repository."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields
from typing import Any, get_args

import pytest

from mnemos.persistence.base import (
    WEBHOOK_LIVE_STATUSES,
    WEBHOOK_TERMINAL_STATUSES,
    WebhookDeliveryClaim,
    WebhookDeliveryIntent,
    WebhookDeliveryOutcome,
    WebhookDeliveryRecord,
    WebhookDeliveryStatus,
    WebhookFinalizationResult,
    WebhookRepository,
    WebhookSubscriptionRecord,
)


STAGED_METHODS = frozenset(
    {
        "create_subscription",
        "list_subscriptions",
        "get_subscription",
        "revoke_subscription",
        "list_deliveries",
        "claim_delivery",
        "claim_due_deliveries",
        "guard_delivery_claim",
        "release_delivery_claim",
        "finalize_delivery",
        "store_delivery_response_body",
        "repair_delivery_chains",
    }
)


class _DispatchOnlyWebhookRepository(WebhookRepository):
    async def dispatch_event(
        self,
        tx: Any,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[WebhookDeliveryIntent]:
        return []


def test_canonical_status_vocabulary_is_closed() -> None:
    statuses = frozenset(get_args(WebhookDeliveryStatus))
    assert statuses == frozenset({"pending", "retrying", "succeeded", "abandoned"})
    assert WEBHOOK_LIVE_STATUSES == frozenset({"pending", "retrying"})
    assert WEBHOOK_TERMINAL_STATUSES == frozenset({"succeeded", "abandoned"})
    assert WEBHOOK_LIVE_STATUSES | WEBHOOK_TERMINAL_STATUSES == statuses
    assert not WEBHOOK_LIVE_STATUSES & WEBHOOK_TERMINAL_STATUSES


def test_canonical_value_objects_expose_the_required_fields() -> None:
    assert {field.name for field in fields(WebhookSubscriptionRecord)} == {
        "id",
        "url",
        "events",
        "description",
        "owner_id",
        "namespace",
        "created",
        "revoked",
        "revoked_at",
    }
    assert {field.name for field in fields(WebhookDeliveryRecord)} == {
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
    }
    assert {field.name for field in fields(WebhookDeliveryClaim)} == {
        "delivery",
        "lease_token",
        "lease_expires_at",
        "claim_db_now",
        "url",
        "secret",
        "subscription_revoked",
        "owner_id",
        "namespace",
    }
    assert {field.name for field in fields(WebhookFinalizationResult)} == {
        "applied",
        "status",
        "successor_delivery_id",
    }


def test_webhook_outcome_is_an_immutable_backend_neutral_value() -> None:
    outcome = WebhookDeliveryOutcome(succeeded=False, response_status=503, error="unavailable")
    with pytest.raises(FrozenInstanceError):
        outcome.error = "changed"  # type: ignore[misc]


def test_staged_repository_surface_is_async_and_keeps_backends_instantiable() -> None:
    assert WebhookRepository.__abstractmethods__ == frozenset({"dispatch_event"})
    assert STAGED_METHODS <= set(vars(WebhookRepository))
    assert all(inspect.iscoroutinefunction(getattr(WebhookRepository, name)) for name in STAGED_METHODS)
    assert isinstance(_DispatchOnlyWebhookRepository(), WebhookRepository)


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        (
            "create_subscription",
            {
                "subscription_id": "subscription-1",
                "url": "https://example.com/hook",
                "events": ("memory.created",),
                "secret": "secret",
                "description": None,
                "owner_id": "owner",
                "namespace": "default",
            },
        ),
        (
            "list_subscriptions",
            {"owner_id": "owner", "namespace": "default", "include_revoked": False, "limit": 50},
        ),
        (
            "get_subscription",
            {"subscription_id": "subscription-1", "owner_id": "owner", "namespace": "default"},
        ),
        (
            "revoke_subscription",
            {"subscription_id": "subscription-1", "owner_id": "owner", "namespace": "default"},
        ),
        (
            "list_deliveries",
            {"subscription_id": "subscription-1", "owner_id": "owner", "namespace": "default", "limit": 50},
        ),
        (
            "claim_delivery",
            {
                "delivery_id": "delivery-1",
                "lease_token": "lease-1",
                "lease_seconds": 60,
                "max_attempts": 4,
                "writer_revision": 1,
            },
        ),
        (
            "claim_due_deliveries",
            {
                "lease_token": "lease-1",
                "limit": 50,
                "lease_seconds": 60,
                "max_attempts": 4,
                "writer_revision": 1,
            },
        ),
        ("guard_delivery_claim", {"delivery_id": "delivery-1", "lease_token": "lease-1"}),
        ("release_delivery_claim", {"delivery_id": "delivery-1", "lease_token": "lease-1"}),
        (
            "finalize_delivery",
            {
                "delivery_id": "delivery-1",
                "lease_token": "lease-1",
                "outcome": WebhookDeliveryOutcome(succeeded=False, error="unavailable"),
                "max_attempts": 4,
                "backoff_schedule": (60, 300, 1800),
            },
        ),
        ("store_delivery_response_body", {"delivery_id": "delivery-1", "response_body": "body"}),
        ("repair_delivery_chains", {}),
    ],
)
@pytest.mark.asyncio
async def test_staged_methods_fail_explicitly_until_backend_items_implement_them(
    method_name: str,
    kwargs: dict[str, Any],
) -> None:
    repo = _DispatchOnlyWebhookRepository()
    with pytest.raises(NotImplementedError, match=method_name):
        await getattr(repo, method_name)(object(), **kwargs)


def test_finalization_contract_keeps_policy_inputs_keyword_only() -> None:
    signature = inspect.signature(WebhookRepository.finalize_delivery)
    assert list(signature.parameters) == [
        "self",
        "tx",
        "delivery_id",
        "lease_token",
        "outcome",
        "max_attempts",
        "backoff_schedule",
    ]
    for name in ("delivery_id", "lease_token", "outcome", "max_attempts", "backoff_schedule"):
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
