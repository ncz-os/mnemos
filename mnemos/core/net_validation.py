"""Webhook / federation URL SSRF validation (core network-safety primitive).

Lives in core (not webhooks) so core.safe_http can use it without violating the
core->webhooks import contract; webhooks.validation re-exports it for back-compat."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import socket
from typing import List, Union
from urllib.parse import urlparse

from fastapi import HTTPException

from mnemos.core.config import get_settings

_WEBHOOK_ALLOW_PRIVATE = get_settings().webhook.allow_private_hosts

# Cloud-provider instance-metadata hostnames we always refuse, even when
# WEBHOOK_ALLOW_PRIVATE_HOSTS=true. Includes the link-local IP literals as a
# belt check (they're also caught by the is_link_local / is_private tests).
_BLOCKED_METADATA_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "metadata.tencentyun.com",
    "100-100-100-200.cn-hangzhou.ecs.aliyuncs.com",
    "169.254.169.254",
    "100.100.100.200",
    "fd00:ec2::254",
    "fe80::a9fe:a9fe",
})

_IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


@dataclass(frozen=True)
class ValidatedWebhookURL:
    url: str
    hostname: str
    port: int
    resolved_ip: str


def _is_never_allowed_ip(ip: _IPAddress) -> bool:
    """Addresses that are never a legitimate target, on any network.

    Link-local covers the cloud instance-metadata endpoints (169.254.169.254,
    fe80::a9fe:a9fe); multicast, reserved and unspecified are not hosts you can
    federate with. These stay blocked even when private addressing is allowed,
    because "I trust my LAN" is not a reason to let a *hostname* resolve into
    the metadata service.

    Kept separate from `_is_private_ip` so relaxing private addressing cannot
    silently relax this class too: a DNS name resolving to 169.254.169.254 is
    caught here regardless of the allow-private flag, which is exactly the
    rebinding path that a literal-string blocklist misses.
    """
    return ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def _is_private_ip(ip: _IPAddress) -> bool:
    """RFC1918 / loopback: not routable publicly, but a normal LAN peer.

    Blocked by default; permitted when the caller opts in (trusted-LAN
    federation, local webhook testing).
    """
    return ip.is_loopback or ip.is_private


def _is_blocked_ip(ip: _IPAddress) -> bool:
    """Back-compat: the full strict predicate (both classes)."""
    return _is_never_allowed_ip(ip) or _is_private_ip(ip)


async def _resolve_addrs(host: str) -> List[str]:
    """Resolve host asynchronously so DNS cannot block the event loop.

    Bounded by ``WEBHOOK_DNS_TIMEOUT`` (default 10.0s,
    `_WebhookSettings.dns_timeout`). Without the timeout, slow DNS
    can stall the validation-time hop indefinitely; the lease-budget
    calc in `_derive_lease_defaults` already includes this timeout
    in its 90-sec floor, so the runtime contract assumes the cap
    is enforced here.
    """
    loop = asyncio.get_event_loop()
    timeout = get_settings().webhook.dns_timeout
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, None),
        timeout=timeout,
    )
    return [info[4][0] for info in infos]


async def validate_webhook_url(
    url: str,
    *,
    allow_private: bool | None = None,
) -> ValidatedWebhookURL:
    """Validate a webhook URL: scheme + host not pointing at internal services."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="url must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=422, detail="url must include a host")

    if host.lower() in _BLOCKED_METADATA_HOSTS:
        raise HTTPException(status_code=422, detail="url host is not permitted")

    allow_private_hosts = _WEBHOOK_ALLOW_PRIVATE if allow_private is None else allow_private

    try:
        ip = ipaddress.ip_address(host)
        if _is_never_allowed_ip(ip) or (not allow_private_hosts and _is_private_ip(ip)):
            raise HTTPException(status_code=422, detail="url host resolves to a non-routable address")
        return ValidatedWebhookURL(
            url=url,
            hostname=host,
            port=parsed.port or (443 if parsed.scheme == "https" else 80),
            resolved_ip=str(ip),
        )
    except ValueError:
        pass

    try:
        addrs = await _resolve_addrs(host)
    except asyncio.TimeoutError:
        # NB: must come BEFORE the OSError clause. In Python 3.11+
        # asyncio.TimeoutError aliases builtin TimeoutError which
        # IS an OSError subclass — without the explicit ordering,
        # `except (socket.gaierror, OSError)` swallows the timeout
        # path and the operator-facing detail loses its specificity.
        raise HTTPException(
            status_code=422,
            detail="url host DNS resolution timed out",
        )
    except (socket.gaierror, OSError):
        raise HTTPException(status_code=422, detail="url host could not be resolved")
    first_validated_addr: str | None = None
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_never_allowed_ip(ip) or (not allow_private_hosts and _is_private_ip(ip)):
            raise HTTPException(status_code=422, detail="url host resolves to a non-routable address")
        if first_validated_addr is None:
            first_validated_addr = str(ip)
    if first_validated_addr is None:
        raise HTTPException(status_code=422, detail="url host could not be resolved")
    return ValidatedWebhookURL(
        url=url,
        hostname=host,
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        resolved_ip=first_validated_addr,
    )
