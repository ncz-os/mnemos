"""Webhook / federation URL SSRF validation (core network-safety primitive).

Lives in core (not webhooks) so core.safe_http can use it without violating the
core->webhooks import contract; webhooks.validation re-exports it for back-compat.

Address classes
---------------

Two classes of address are refused, and they are refused for different
reasons:

``never allowed``
    The cloud instance-metadata service and its kin — link-local
    (169.254.169.254, fe80::a9fe:a9fe), multicast, reserved and
    unspecified — plus any IPv6 address that EMBEDS one of those IPv4
    addresses (6to4, NAT64, IPv4-mapped). "I trust my LAN" is not a
    reason to reach the metadata service, so ``allow_private`` does NOT
    open this class.

``private``
    RFC1918, loopback and CGNAT (100.64.0.0/10, RFC 6598). These are
    ordinary LAN / overlay peers: closed by default, opened by
    ``allow_private``.

The escape hatch
----------------

``allow_private=True`` (or ``WEBHOOK_ALLOW_PRIVATE_HOSTS=true`` for the
webhook sender, ``FEDERATION_ALLOW_PRIVATE=true`` for federation peers)
is the documented way to reach a LAN peer, a Tailscale/other overlay
address — including a CGNAT (100.64.0.0/10) one — or a local test
endpoint. It opens the ``private`` class only. The metadata service
stays unreachable in every configuration.
"""
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

# Carrier-grade NAT (RFC 6598). ``ipaddress`` deliberately reports
# 100.64.0.0/10 as neither private nor global, so it slips through both
# the private-address check and any "is it routable" reasoning. Inside a
# cloud VPC (or behind a Tailscale/other overlay gateway) 100.64.0.0/10
# addresses the carrier network — reaching one from MNEMOS is a way out of
# the intended network boundary, not a legitimate webhook peer.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# Well-known NAT64 prefix (RFC 6052 §2.1) and the RFC 8215 local-use
# translation prefix. Both carry an IPv4 address inside an IPv6 one, so a
# NAT64 address can reach a blocked IPv4 target through a translator the
# operator does not control.
#
# RFC 6052 §2.2 places the embedded IPv4 at a position that depends on
# the prefix length, with a reserved "u" octet at bits 64..71 separating
# the halves of a split address:
#
#   /32 -> bits 32..63     /40 -> bits 40..63 + 72..79
#   /48 -> bits 48..63 + 72..87 (bytes 6..7 + bytes 9..10)
#   /56 -> bits 56..63 + 72..95
#   /64 -> bits 72..103    /96 -> bits 96..127 (bytes 12..15)
#
# The well-known /96 is unambiguous. RFC 8215's /48 deliberately leaves
# the embedded location UNDEFINED ("IPv6 nodes and applications must not
# make any assumptions regarding the syntax or properties of those
# addresses (e.g., the existence and location of embedded IPv4
# addresses)"), so both plausible layouts are checked for it.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.ip_network("64:ff9b:1::/48")


def _nat64_embedded_ipv4(ip: ipaddress.IPv6Address) -> tuple[ipaddress.IPv4Address, ...]:
    """Return the IPv4 address(es) a NAT64 address embeds (RFC 6052 §2.3).

    The well-known prefix has exactly one layout. The RFC 8215 local-use
    prefix has none by definition, so both the RFC 6052 /48 split layout
    and the informal "IPv4 in the low 32 bits" layout are returned — the
    caller blocks the address if ANY of them is a forbidden target.
    """
    if ip in _NAT64_WELL_KNOWN:
        # /96: the IPv4 address is the last 32 bits.
        return (ipaddress.IPv4Address(ip.packed[12:16]),)
    if ip in _NAT64_LOCAL_USE:
        # /48: the RFC 6052 layout splits the address around the reserved
        # "u" octet (bits 64..71 / byte 8); the informal layout puts the
        # whole IPv4 address in the low 32 bits.
        return (
            ipaddress.IPv4Address(ip.packed[6:8] + ip.packed[9:11]),
            ipaddress.IPv4Address(ip.packed[12:16]),
        )
    return ()


@dataclass(frozen=True)
class ValidatedWebhookURL:
    url: str
    hostname: str
    port: int
    resolved_ip: str


def _embedded_ipv4_candidates(ip: _IPAddress) -> tuple[ipaddress.IPv4Address, ...]:
    """Return the IPv4 address(es) an IP address can reach, if any.

    Covers the transition mechanisms that carry an IPv4 address inside an
    IPv6 one:

    * 6to4 (``2002::/16``) — the embedded IPv4 sits in bits 16..48;
    * NAT64 (``64:ff9b::/96`` and ``64:ff9b:1::/48``) — the embedded IPv4
      sits where the prefix length puts it;
    * IPv4-mapped (``::ffff:0:0/96``) — the embedded IPv4 sits in the low
      32 bits;
    * Teredo (``2001::/32``) — the embedded (server) IPv4 sits in the low
      32 bits, with the client IPv4 XOR-obfuscated in the middle.

    A 6to4 address of the cloud metadata endpoint
    (``2002:a9fe:a9fe::`` = ``169.254.169.254``) is the classic way to
    smuggle a blocked IPv4 target past a literal-IP blocklist, because
    the IPv6 form is not itself in the blocked IPv4 range.
    """
    if isinstance(ip, ipaddress.IPv4Address):
        return (ip,)
    mapped = ip.ipv4_mapped
    if mapped is not None:
        return (mapped,)
    sixtofour = ip.sixtofour
    if sixtofour is not None:
        return (sixtofour,)
    teredo = ip.teredo
    if teredo is not None:
        # (server, client) — the server IPv4 is the routable one.
        return (teredo[0],)
    return _nat64_embedded_ipv4(ip)


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

    An IPv6 address that EMBEDS a blocked IPv4 address (6to4 / NAT64) is
    never allowed either: ``[2002:a9fe:a9fe::]`` is the metadata endpoint
    in another notation, and a NAT64 address reaches any IPv4 target
    through a translator the operator does not control.
    """
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    return any(
        embedded.is_link_local or embedded.is_multicast or embedded.is_reserved or embedded.is_unspecified
        for embedded in _embedded_ipv4_candidates(ip)
    )


def _is_private_ip(ip: _IPAddress) -> bool:
    """RFC1918 / loopback / CGNAT: not routable publicly, but a normal LAN peer.

    Blocked by default; permitted when the caller opts in (trusted-LAN
    federation, local webhook testing).

    The CGNAT range (100.64.0.0/10) is in this class, not in the
    never-allowed one: a Tailscale/overlay peer may legitimately live
    there, so ``allow_private`` still opens it up. What must stay closed
    even with ``allow_private`` is the metadata service — that is the
    never-allowed class above.
    """
    if ip.is_loopback or ip.is_private:
        return True
    return any(embedded.version == 4 and embedded in _CGNAT_NETWORK for embedded in _embedded_ipv4_candidates(ip))


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
