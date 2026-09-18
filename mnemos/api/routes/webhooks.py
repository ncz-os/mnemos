"""Webhook subscription CRUD — /v1/webhooks.

Outbound notifications on memory and consultation events. Delivery is handled
by `mnemos.webhooks.dispatcher`; this handler is CRUD only.
"""
import logging
import secrets
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import maybe_set_pg_rls, require_webhooks_backend
from mnemos.core.ids import parse_uuid_or_404
from mnemos.domain.models import (
    VALID_WEBHOOK_EVENTS,
    WebhookCreateRequest,
    WebhookCreateResponse,
    WebhookDelivery,
    WebhookDeliveryListResponse,
    WebhookItem,
    WebhookListResponse,
)
from mnemos.webhooks.validation import validate_webhook_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

# The pre-ABC route returned every subscription.  Repositories require an
# explicit bound, so use the largest portable signed-32-bit LIMIT to preserve
# that public behavior without adding an unrelated pagination change here.
_ALL_SUBSCRIPTIONS_LIMIT = 2_147_483_647


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_events(events: List[str]) -> None:
    if not events:
        raise HTTPException(status_code=422, detail="events must not be empty")
    bad = [e for e in events if e not in VALID_WEBHOOK_EVENTS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"unknown events: {bad}. valid events: {sorted(VALID_WEBHOOK_EVENTS)}",
        )


# Kept as `_validate_url` alias for callers inside this module.
_validate_url = validate_webhook_url


def _to_item(row) -> WebhookItem:
    return WebhookItem(
        id=row.id,
        url=row.url,
        events=list(row.events),
        description=row.description,
        owner_id=row.owner_id,
        namespace=row.namespace,
        created=row.created.isoformat(),
        revoked=row.revoked,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", response_model=WebhookCreateResponse, status_code=201)
async def create_webhook(
    request: WebhookCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create a webhook subscription. Returns the HMAC secret exactly once."""
    backend = require_webhooks_backend()

    await _validate_url(request.url)
    _validate_events(request.events)

    secret = secrets.token_urlsafe(32)

    # v3.2 Tier 3: non-root cannot create a webhook in a namespace
    # other than their own. Root may pass request.namespace for
    # cross-tenant support.
    if request.namespace and request.namespace != user.namespace:
        if user.role != "root":
            raise HTTPException(
                status_code=403,
                detail="cross-namespace webhook create requires root",
            )
    namespace = request.namespace or user.namespace or "default"

    subscription_id = str(uuid.uuid4())

    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        record = await backend.webhooks.create_subscription(
            tx,
            subscription_id=subscription_id,
            url=request.url,
            events=request.events,
            secret=secret,
            description=request.description,
            owner_id=user.user_id,
            namespace=namespace,
        )

    logger.info(
        "webhook created id=%s owner=%s events=%s",
        record.id,
        user.user_id,
        list(record.events),
    )

    # NATS publish after commit (outside transaction context)
    from mnemos.nats import publish_event as _nats_publish_event
    from mnemos.nats.client import get_node_name as _nats_get_node_name
    safe_ns = (namespace or "default").replace(".", "_")
    await _nats_publish_event(
        f"mnemos.webhook.subscription.created.{safe_ns}",
        {
            "webhook_id": record.id,
            "url": record.url,
            "event_types": list(record.events),
            "namespace": record.namespace,
            "owner_id": record.owner_id,
            "source_node": _nats_get_node_name(),
        },
        msg_id=f"webhook.{record.id}.subscription.created",
    )

    return WebhookCreateResponse(
        id=record.id,
        url=record.url,
        events=list(record.events),
        description=record.description,
        owner_id=record.owner_id,
        namespace=record.namespace,
        created=record.created.isoformat(),
        revoked=record.revoked,
        secret=secret,
    )


@router.get("", response_model=WebhookListResponse)
async def list_webhooks(
    user: UserContext = Depends(get_current_user),
    include_revoked: bool = False,
):
    """List the caller's webhook subscriptions. Secrets are never returned."""
    backend = require_webhooks_backend()

    # v3.2 Tier 3: scope by owner_id + namespace. Root sees all
    # (no owner / namespace filter) so ops can audit cross-tenant.
    is_root = user.role == "root"

    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        rows = await backend.webhooks.list_subscriptions(
            tx,
            owner_id=user.user_id if not is_root else None,
            namespace=user.namespace if not is_root else None,
            include_revoked=include_revoked,
            limit=_ALL_SUBSCRIPTIONS_LIMIT,
        )

    return WebhookListResponse(
        count=len(rows), webhooks=[_to_item(r) for r in rows]
    )


@router.get("/{webhook_id}", response_model=WebhookItem)
async def get_webhook(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Get a webhook subscription by ID. Secrets are never returned."""
    webhook_id = parse_uuid_or_404(webhook_id, "webhook")
    backend = require_webhooks_backend()

    # v3.2 Tier 3: non-root must match owner AND namespace.
    # Root reads any webhook.
    is_root = user.role == "root"

    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        row = await backend.webhooks.get_subscription(
            tx,
            subscription_id=webhook_id,
            owner_id=user.user_id if not is_root else None,
            namespace=user.namespace if not is_root else None,
        )
    if not row:
        raise HTTPException(status_code=404, detail="webhook not found")
    return _to_item(row)


@router.delete("/{webhook_id}", status_code=204)
async def revoke_webhook(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Soft-delete: marks the subscription revoked. Delivery log preserved."""
    webhook_id = parse_uuid_or_404(webhook_id, "webhook")
    backend = require_webhooks_backend()

    # v3.2 Tier 3: non-root must match owner AND namespace. Root
    # can revoke any webhook.
    is_root = user.role == "root"

    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        success = await backend.webhooks.revoke_subscription(
            tx,
            subscription_id=webhook_id,
            owner_id=user.user_id if not is_root else None,
            namespace=user.namespace if not is_root else None,
        )
    if not success:
        raise HTTPException(
            status_code=404, detail="webhook not found or already revoked"
        )
    logger.info("webhook revoked id=%s owner=%s", webhook_id, user.user_id)


@router.get("/{webhook_id}/deliveries", response_model=WebhookDeliveryListResponse)
async def list_deliveries(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
    # #205: cap caller-controlled limit. The default-50 case is the
    # operational shape; a 200 ceiling matches the bound on adjacent
    # listing endpoints and prevents an authenticated caller from
    # asking for a million-row delivery dump in one round-trip.
    limit: int = Query(50, ge=1, le=200),
):
    """List recent delivery attempts for a subscription."""
    webhook_id = parse_uuid_or_404(webhook_id, "webhook")
    backend = require_webhooks_backend()

    # v3.2 Tier 3: subscription must belong to caller's owner AND
    # namespace. Root bypasses both.
    is_root = user.role == "root"

    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        # Verify subscription visibility
        sub = await backend.webhooks.get_subscription(
            tx,
            subscription_id=webhook_id,
            owner_id=user.user_id if not is_root else None,
            namespace=user.namespace if not is_root else None,
        )
        if not sub:
            raise HTTPException(status_code=404, detail="webhook not found")
        rows = await backend.webhooks.list_deliveries(
            tx,
            subscription_id=webhook_id,
            owner_id=user.user_id if not is_root else None,
            namespace=user.namespace if not is_root else None,
            limit=limit,
        )

    deliveries = [
        WebhookDelivery(
            id=str(r.id),
            subscription_id=str(r.subscription_id),
            event_type=r.event_type,
            attempt_num=r.attempt_num,
            status=r.status,
            superseded=r.superseded,
            response_status=r.response_status,
            response_body=r.response_body,
            error=r.error,
            scheduled_at=r.scheduled_at.isoformat(),
            delivered_at=r.delivered_at.isoformat() if r.delivered_at else None,
            created=r.created.isoformat(),
        )
        for r in rows
    ]
    return WebhookDeliveryListResponse(count=len(deliveries), deliveries=deliveries)
