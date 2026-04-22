"""Webhook subscription CRUD — /v1/webhooks.

Outbound notifications on memory and consultation events. Delivery is handled
by `api.webhook_dispatcher`; this handler is CRUD only.
"""
import ipaddress
import logging
import os
import secrets
import socket
import uuid as _uuid
from typing import List
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException


def _parse_uuid_or_404(value: str, what: str = "resource") -> str:
    """Validate a UUID path parameter. Raises 404 on malformed input so we
    don't surface internal driver errors (`InvalidTextRepresentation`) as 500s."""
    try:
        _uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return value

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from api.models import (
    VALID_WEBHOOK_EVENTS,
    WebhookCreateRequest,
    WebhookCreateResponse,
    WebhookDelivery,
    WebhookDeliveryListResponse,
    WebhookItem,
    WebhookListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


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


_WEBHOOK_ALLOW_PRIVATE = os.getenv("WEBHOOK_ALLOW_PRIVATE_HOSTS", "false").lower() == "true"


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """SSRF defense: block loopback, private, link-local, multicast, reserved."""
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_webhook_url(url: str) -> None:
    """Validate a webhook URL: scheme + host not pointing at internal services.

    Called at both subscription create time (handler) and dispatch time
    (webhook_dispatcher). Raises HTTPException(422) on bad input.
    Set WEBHOOK_ALLOW_PRIVATE_HOSTS=true to permit private/loopback targets
    (useful for local testing; unsafe in production).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="url must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=422, detail="url must include a host")

    if _WEBHOOK_ALLOW_PRIVATE:
        return

    # Reject cloud metadata endpoints by hostname.
    if host in ("metadata.google.internal", "metadata.goog"):
        raise HTTPException(status_code=422, detail="url host is not permitted")

    # If host is already an IP literal, check it directly. Otherwise resolve
    # and check every returned address family.
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise HTTPException(status_code=422, detail="url host resolves to a non-routable address")
        return
    except ValueError:
        pass  # not a literal IP — resolve DNS

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(status_code=422, detail="url host could not be resolved")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise HTTPException(status_code=422, detail="url host resolves to a non-routable address")


# Kept as `_validate_url` alias for callers inside this module.
_validate_url = validate_webhook_url


def _to_item(row) -> WebhookItem:
    return WebhookItem(
        id=str(row["id"]),
        url=row["url"],
        events=list(row["events"]),
        description=row["description"],
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        created=row["created"].isoformat(),
        revoked=row["revoked"],
        revoked_at=row["revoked_at"].isoformat() if row["revoked_at"] else None,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("", response_model=WebhookCreateResponse, status_code=201)
async def create_webhook(
    request: WebhookCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create a webhook subscription. Returns the HMAC secret exactly once."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    _validate_url(request.url)
    _validate_events(request.events)

    secret = secrets.token_urlsafe(32)
    namespace = request.namespace or user.namespace or "default"

    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_subscriptions
              (url, events, secret, description, owner_id, namespace)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, url, events, description, owner_id, namespace, created, revoked
            """,
            request.url,
            request.events,
            secret,
            request.description,
            user.user_id,
            namespace,
        )

    logger.info(
        "webhook created id=%s owner=%s events=%s",
        row["id"], user.user_id, list(row["events"]),
    )

    return WebhookCreateResponse(
        id=str(row["id"]),
        url=row["url"],
        events=list(row["events"]),
        description=row["description"],
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        created=row["created"].isoformat(),
        revoked=row["revoked"],
        secret=secret,
    )


@router.get("", response_model=WebhookListResponse)
async def list_webhooks(
    user: UserContext = Depends(get_current_user),
    include_revoked: bool = False,
):
    """List the caller's webhook subscriptions. Secrets are never returned."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        if include_revoked:
            rows = await conn.fetch(
                """
                SELECT id, url, events, description, owner_id, namespace,
                       created, revoked, revoked_at
                FROM webhook_subscriptions
                WHERE owner_id = $1
                ORDER BY created DESC
                """,
                user.user_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, url, events, description, owner_id, namespace,
                       created, revoked, revoked_at
                FROM webhook_subscriptions
                WHERE owner_id = $1 AND NOT revoked
                ORDER BY created DESC
                """,
                user.user_id,
            )

    return WebhookListResponse(
        count=len(rows), webhooks=[_to_item(r) for r in rows]
    )


@router.get("/{webhook_id}", response_model=WebhookItem)
async def get_webhook(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
):
    _parse_uuid_or_404(webhook_id, "webhook")
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, url, events, description, owner_id, namespace,
                   created, revoked, revoked_at
            FROM webhook_subscriptions
            WHERE id = $1::uuid AND owner_id = $2
            """,
            webhook_id, user.user_id,
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
    _parse_uuid_or_404(webhook_id, "webhook")
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE webhook_subscriptions
            SET revoked = TRUE, revoked_at = NOW()
            WHERE id = $1::uuid AND owner_id = $2 AND NOT revoked
            RETURNING id
            """,
            webhook_id, user.user_id,
        )
    if not row:
        raise HTTPException(
            status_code=404, detail="webhook not found or already revoked"
        )
    logger.info("webhook revoked id=%s owner=%s", webhook_id, user.user_id)


@router.get("/{webhook_id}/deliveries", response_model=WebhookDeliveryListResponse)
async def list_deliveries(
    webhook_id: str,
    user: UserContext = Depends(get_current_user),
    limit: int = 50,
):
    """List recent delivery attempts for a subscription."""
    _parse_uuid_or_404(webhook_id, "webhook")
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        sub = await conn.fetchrow(
            "SELECT id FROM webhook_subscriptions WHERE id=$1::uuid AND owner_id=$2",
            webhook_id, user.user_id,
        )
        if not sub:
            raise HTTPException(status_code=404, detail="webhook not found")
        rows = await conn.fetch(
            """
            SELECT id, subscription_id, event_type, attempt_num, status,
                   response_status, response_body, error,
                   scheduled_at, delivered_at, created
            FROM webhook_deliveries
            WHERE subscription_id = $1::uuid
            ORDER BY created DESC
            LIMIT $2
            """,
            webhook_id, limit,
        )

    deliveries = [
        WebhookDelivery(
            id=str(r["id"]),
            subscription_id=str(r["subscription_id"]),
            event_type=r["event_type"],
            attempt_num=r["attempt_num"],
            status=r["status"],
            response_status=r["response_status"],
            response_body=r["response_body"],
            error=r["error"],
            scheduled_at=r["scheduled_at"].isoformat(),
            delivered_at=r["delivered_at"].isoformat() if r["delivered_at"] else None,
            created=r["created"].isoformat(),
        )
        for r in rows
    ]
    return WebhookDeliveryListResponse(count=len(deliveries), deliveries=deliveries)
