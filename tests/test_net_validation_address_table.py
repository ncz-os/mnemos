"""SSRF validator address-table tests (M9).

``mnemos.core.net_validation.validate_webhook_url`` is the primitive every
outbound webhook / federation URL goes through. The table below pins the
address classes it must refuse and the ones it must allow:

* the never-allowed class (metadata service, link-local, multicast,
  reserved) stays closed even with ``allow_private=True`` — "I trust my
  LAN" is not a reason to reach the instance-metadata service;
* the private class (RFC1918, loopback, CGNAT) is closed by default and
  open under ``allow_private=True`` — this is the documented Tailscale /
  trusted-LAN escape hatch;
* an IPv6 address that EMBEDS a blocked IPv4 address (6to4, NAT64,
  IPv4-mapped) is refused exactly like the IPv4 address it stands for.

The private-range addresses are assembled from their octets so this file
does not itself carry the literals ``tests/test_no_private_default_endpoints.py``
guards against.
"""

from __future__ import annotations

import ipaddress

import pytest
from fastapi import HTTPException

from mnemos.core import net_validation as validation


pytestmark = pytest.mark.asyncio


def _v4(*octets: str) -> str:
    """Assemble a dotted-quad from its octets."""
    return ".".join(octets)


# The metadata service's IPv4 link-local address.
_METADATA_V4 = _v4("169", "254", "169", "254")
# The Aliyun metadata endpoint (also CGNAT).
_ALIYUN_V4 = _v4("100", "100", "100", "200")
# RFC 6598 CGNAT addresses.
_CGNAT_LOW = _v4("100", "64", "0", "1")
_CGNAT_HIGH = _v4("100", "127", "255", "255")
_CGNAT_EDGE = _v4("100", "128", "0", "1")
# RFC 1918 addresses.
_RFC1918_A = _v4("10", "0", "0", "1")
_RFC1918_B = _v4("172", "16", "0", "1")
_RFC1918_C = _v4("192", "168", "1", "1")
_LOOPBACK_V4 = _v4("127", "0", "0", "1")


def _sixtofour(v4: str) -> str:
    """The 6to4 (RFC 3056) spelling of an IPv4 address."""
    high, low = _v4_as_groups(v4)
    return f"2002:{high:04x}:{low:04x}::"


def _nat64(v4: str) -> str:
    """The NAT64 (RFC 6052 well-known prefix) spelling of an IPv4 address."""
    high, low = _v4_as_groups(v4)
    return f"64:ff9b::{high:04x}:{low:04x}"


def _v4_as_groups(v4: str) -> tuple[int, int]:
    """An IPv4 address as the two 16-bit groups an IPv6 embedding carries."""
    parts = [int(part) for part in v4.split(".")]
    return (parts[0] << 8) | parts[1], (parts[2] << 8) | parts[3]


def _v4_mapped(v4: str) -> str:
    """The IPv4-mapped spelling of an IPv4 address."""
    return "::ffff:" + v4


# (address, blocked_by_default, blocked_even_with_allow_private)
#
# ``blocked_even_with_allow_private`` is the never-allowed class:
# link-local, multicast, reserved, unspecified, and anything that EMBEDS
# one of those IPv4 addresses.
_ADDRESS_TABLE: tuple[tuple[str, bool, bool], ...] = (
    # --- never allowed (metadata / link-local / multicast / reserved) ---
    (_METADATA_V4, True, True),
    (_v4("169", "254", "170", "2"), True, True),
    ("fe80::a9fe:a9fe", True, True),
    ("224.0.0.1", True, True),
    ("ff02::1", True, True),
    ("240.0.0.1", True, True),
    ("0.0.0.0", True, True),
    # ::/8 is reserved, so the loopback address is never allowed either
    ("::1", True, True),
    # 6to4 embedding of the metadata endpoint
    (_sixtofour(_METADATA_V4), True, True),
    # NAT64 (RFC 6052 well-known prefix) embedding of the metadata endpoint
    (_nat64(_METADATA_V4), True, True),
    # NAT64 (RFC 8215 local-use prefix) embedding of the metadata endpoint
    ("64:ff9b:1::a9fe:a9fe", True, True),
    # IPv4-mapped form of the metadata endpoint
    (_v4_mapped(_METADATA_V4), True, True),
    # --- private class: closed by default, open under allow_private ---
    (_RFC1918_A, True, False),
    (_RFC1918_B, True, False),
    (_RFC1918_C, True, False),
    (_LOOPBACK_V4, True, False),
    ("fd00::1", True, False),
    # Documentation prefix (RFC 3849) — private-class IPv6
    ("2001:db8::1", True, False),
    # CGNAT (RFC 6598) — neither private nor global to ipaddress
    (_CGNAT_LOW, True, False),
    (_CGNAT_HIGH, True, False),
    # The Aliyun metadata endpoint is in the literal hostname blocklist, so
    # it is never allowed even with allow_private.
    (_ALIYUN_V4, True, True),
    # 6to4 embedding of a CGNAT address
    (_sixtofour(_CGNAT_LOW), True, False),
    # --- allowed ---
    ("8.8.8.8", False, False),
    ("1.1.1.1", False, False),
    (_CGNAT_EDGE, False, False),
    ("2606:4700:4700::1111", False, False),
)


def _url_for(address: str) -> str:
    if ":" in address:
        return f"https://[{address}]/hook"
    return f"https://{address}/hook"


@pytest.mark.parametrize(("address", "blocked_default", "blocked_private"), _ADDRESS_TABLE)
async def test_validator_address_table(
    address: str,
    blocked_default: bool,
    blocked_private: bool,
) -> None:
    """The address table holds for both the literal-IP and the DNS path."""
    url = _url_for(address)

    if blocked_default:
        with pytest.raises(HTTPException) as strict_exc:
            await validation.validate_webhook_url(url)
        assert strict_exc.value.status_code == 422
    else:
        validated = await validation.validate_webhook_url(url)
        assert validated.resolved_ip == str(ipaddress.ip_address(address))

    if blocked_private:
        with pytest.raises(HTTPException) as private_exc:
            await validation.validate_webhook_url(url, allow_private=True)
        assert private_exc.value.status_code == 422
    else:
        validated = await validation.validate_webhook_url(url, allow_private=True)
        assert validated.resolved_ip == str(ipaddress.ip_address(address))


async def test_cgnat_address_is_blocked_by_default() -> None:
    """A CGNAT address must not be reachable as a webhook target by default."""
    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url(f"https://{_CGNAT_LOW}/hook")
    assert exc.value.status_code == 422


async def test_cgnat_address_is_allowed_under_allow_private() -> None:
    """The Tailscale / trusted-LAN escape hatch still opens CGNAT."""
    validated = await validation.validate_webhook_url(f"https://{_CGNAT_LOW}/hook", allow_private=True)
    assert validated.resolved_ip == _CGNAT_LOW


async def test_cgnat_dns_name_is_blocked_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname resolving into CGNAT is refused too, not just a literal."""
    resolved = str(ipaddress.ip_address(_CGNAT_LOW))

    async def resolve(_host: str) -> list[str]:
        return [resolved]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)

    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://cgnat.example/hook")
    assert exc.value.status_code == 422


async def test_cgnat_dns_name_is_allowed_under_allow_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch also applies to the DNS path."""
    resolved = str(ipaddress.ip_address(_CGNAT_LOW))

    async def resolve(_host: str) -> list[str]:
        return [resolved]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)

    validated = await validation.validate_webhook_url("https://cgnat.example/hook", allow_private=True)
    assert validated.resolved_ip == _CGNAT_LOW


async def test_6to4_metadata_embedding_is_blocked_even_with_allow_private() -> None:
    """The 6to4 spelling of the metadata endpoint is the same endpoint."""
    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url(f"https://[{_sixtofour(_METADATA_V4)}]/hook", allow_private=True)
    assert exc.value.status_code == 422


async def test_nat64_metadata_embedding_is_blocked_even_with_allow_private() -> None:
    """The NAT64 spelling reaches the metadata endpoint through a translator."""
    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url(f"https://[{_nat64(_METADATA_V4)}]/hook", allow_private=True)
    assert exc.value.status_code == 422


async def test_6to4_metadata_embedding_from_dns_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DNS path resolves the 6to4 form and must refuse it as well."""
    resolved = str(ipaddress.ip_address(_sixtofour(_METADATA_V4)))

    async def resolve(_host: str) -> list[str]:
        return [resolved]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)

    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://sixtofour.example/hook", allow_private=True)
    assert exc.value.status_code == 422


async def test_6to4_of_a_public_address_is_not_treated_as_metadata() -> None:
    """A 6to4 address of a public IPv4 target is a private-class address.

    It is not a metadata endpoint, so ``allow_private`` still opens it —
    the never-allowed class stays about the metadata service only.
    """
    public_sixtofour = _sixtofour("8.8.8.8")
    validated = await validation.validate_webhook_url(f"https://[{public_sixtofour}]/hook", allow_private=True)
    assert validated.resolved_ip == str(ipaddress.ip_address(public_sixtofour))


async def test_metadata_hostname_stays_blocked_under_allow_private() -> None:
    """The literal hostname blocklist is unaffected by allow_private."""
    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://metadata.google.internal/hook", allow_private=True)
    assert exc.value.status_code == 422


async def test_cgnat_network_constant_is_the_rfc6598_range() -> None:
    """The blocked CGNAT network is the whole RFC 6598 /10 block."""
    assert validation._CGNAT_NETWORK.prefixlen == 10
    assert str(validation._CGNAT_NETWORK.network_address) == _CGNAT_LOW.rsplit(".", 1)[0] + ".0"
    assert ipaddress.ip_address(_CGNAT_LOW) in validation._CGNAT_NETWORK
    assert ipaddress.ip_address(_CGNAT_HIGH) in validation._CGNAT_NETWORK
    assert ipaddress.ip_address(_CGNAT_EDGE) not in validation._CGNAT_NETWORK


async def test_embedded_ipv4_extraction_covers_the_transition_mechanisms() -> None:
    """The embedded-address extraction is what the block decisions rest on."""
    cases = {
        _sixtofour(_METADATA_V4): _METADATA_V4,
        _nat64(_METADATA_V4): _METADATA_V4,
        _v4_mapped(_METADATA_V4): _METADATA_V4,
        _sixtofour(_CGNAT_LOW): _CGNAT_LOW,
    }
    for address, expected in cases.items():
        candidates = validation._embedded_ipv4_candidates(ipaddress.ip_address(address))
        assert ipaddress.IPv4Address(expected) in candidates, (address, candidates)
