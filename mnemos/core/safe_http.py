"""SSRF-safe HTTP client helpers shared by webhook delivery and federation pull.

Both outbound HTTP paths (webhook delivery, federation pull) carry
credentials (a bearer/HMAC token) and must not be redirectable to
internal/metadata endpoints by DNS rebinding or HTTP redirect. This module
centralizes the validate-then-pin pattern from webhook delivery so
federation pull reuses the same defense: validate the URL against the
SSRF blocklist at call time, resolve DNS once, and build an httpx client
whose transport is pinned to the validated IP and that does not follow
redirects.

Adversarial review 2026-06-28 (F1): federation pull previously used plain
``httpx.AsyncClient``, validated only at peer-registration time. A DNS
rebinding TOCTOU between registration and a later sync could route the
authenticated pull (carrying the peer bearer token) to an internal or
cloud-metadata endpoint. Webhook delivery was already defended; this
makes federation pull equivalent.

The SSRF validator (``validate_webhook_url``) lives in
``mnemos.core.net_validation``; it is imported lazily inside
``make_safe_client`` to avoid a circular import (importing this module must
not trigger ``mnemos.webhooks.__init__``, which loads the webhook sender,
which imports this module).
"""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING, Any

import httpcore
from httpcore._backends.auto import AutoBackend
import httpx

if TYPE_CHECKING:
    from mnemos.core.net_validation import ValidatedWebhookURL


class _PinnedDNSBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that pins one hostname to a validated IP."""

    def __init__(self, hostname: str, resolved_ip: str):
        self._hostname = hostname.lower().rstrip(".")
        self._resolved_ip = resolved_ip
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        target_host = self._resolved_ip if host.lower().rstrip(".") == self._hostname else host
        return await self._backend.connect_tcp(
            host=target_host,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path=path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedDNSAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport whose resolver cannot rebind after URL validation."""

    def __init__(self, *, hostname: str, resolved_ip: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        pool = self._pool
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=getattr(pool, "_ssl_context", ssl.create_default_context()),
            max_connections=getattr(pool, "_max_connections", 100),
            max_keepalive_connections=getattr(pool, "_max_keepalive_connections", 20),
            keepalive_expiry=getattr(pool, "_keepalive_expiry", 5.0),
            http1=getattr(pool, "_http1", True),
            http2=getattr(pool, "_http2", False),
            retries=getattr(pool, "_retries", 0),
            local_address=getattr(pool, "_local_address", None),
            uds=getattr(pool, "_uds", None),
            socket_options=getattr(pool, "_socket_options", None),
            network_backend=_PinnedDNSBackend(hostname, resolved_ip),
        )


async def make_safe_client(
    url: str,
    *,
    timeout: float,
    allow_private: bool | None = None,
) -> tuple[httpx.AsyncClient, ValidatedWebhookURL]:
    """Validate ``url`` against SSRF, resolve+pin DNS, return a pinned client.

    Returns ``(client, validated_url)``. The caller owns the client's
    lifecycle (use ``async with``). The client does not follow redirects
    and its resolver cannot rebind after validation, so a DNS rebinding
    TOCTOU between this validation and the subsequent request cannot
    redirect the authenticated call to an internal/metadata endpoint.

    ``allow_private`` controls whether private/loopback/link-local IPs
    are permitted. ``None`` defers to the webhook setting
    (``WEBHOOK_ALLOW_PRIVATE_HOSTS``); pass an explicit bool to override
    (e.g. federation uses ``FEDERATION_ALLOW_PRIVATE``).
    """
    from mnemos.core.net_validation import validate_webhook_url

    validated = await validate_webhook_url(url, allow_private=allow_private)
    transport = PinnedDNSAsyncHTTPTransport(
        hostname=validated.hostname,
        resolved_ip=validated.resolved_ip,
    )
    client = httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
    )
    return client, validated
