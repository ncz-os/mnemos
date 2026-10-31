"""SSRF validator address-table tests (M9).

``mnemos.core.net_validation.validate_webhook_url`` is the primitive every
outbound webhook / federation URL goes through. The table below pins the
address classes it must refuse and the ones it must allow:

* the never-allowed class (metadata service, link-local, multicast,
  reserved) stays closed even with ``allow_private=True`` — "I trust my
  LAN" is not a reason to reach the instance-metadata service;
* the private class (RFC1918, loopback, CGNAT 100.64.0.0/10) is closed by
  default and open under ``allow_private=True`` — this is the documented
  Tailscale / trusted-LAN escape hatch;
* an IPv6 address that EMBEDS a blocked IPv4 address (6to4, NAT64,
  IPv4-mapped) is refused exactly like the IPv4 address it stands for.
"""

from __future__ import annotations

import ipaddress

import pytest
from fastapi import HTTPException

from mnemos.core import net_validation as validation


pytestmark = pytest.mark.asyncio


# (address, blocked_by_default, blocked_even_with_allow_private)
#
# ``blocked_even_with_allow_private`` is the never-allowed class:
# link-local, multicast, reserved, unspecified, and anything that EMBEDS
# one of those IPv4 addresses.
_ADDRESS_TABLE: tuple[tuple[str, bool, bool], ...] = (
    # --- never allowed (metadata / link-local / multicast / reserved) ---
    ("169.254.169.254", True, True),
    ("169.254.170.2", True, True),
    ("fe80::a9fe:a9fe", True, True),
    ("224.0.0.1", True, True),
    ("ff02::1", True, True),
    ("240.0.0.1", True, True),
    ("0.0.0.0", True, True),
    # ::/8 is reserved, so the loopback address is never allowed either
    ("::1", True, True),
    # 6to4 embedding of the metadata endpoint
    ("2002:a9fe:a9fe::", True, True),
    # NAT64 (RFC 6052 well-known prefix) embedding of the metadata endpoint
    ("64:ff9b::a9fe:a9fe", True, True),
    # NAT64 (RFC 8215 local-use prefix) embedding of the metadata endpoint
    ("64:ff9b:1::a9fe:a9fe", True, True),
    # IPv4-mapped form of the metadata endpoint
    ("::ffff:169.254.169.254", True, True),
    # --- private class: closed by default, open under allow_private ---
    ("10.0.0.1", True, False),
    ("172.16.0.1", True, False),
    ("192.0.2.1", True, False),
    ("127.0.0.1", True, False),
    ("fd00::1", True, False),
    # Documentation prefix (RFC 3849) — private-class IPv6
    ("2001:db8::1", True, False),
    # CGNAT (RFC 6598) — neither private nor global to ipaddress
    ("100.64.0.1", True, False),
    ("100.127.255.255", True, False),
    # 100.100.100.200 is the Aliyun metadata endpoint: it is in the literal
    # hostname blocklist, so it is never allowed even with allow_private.
    ("100.100.100.200", True, True),
    # 6to4 embedding of a CGNAT address
    ("2002:6440:0001::", True, False),
    # --- allowed ---
    ("8.8.8.8", False, False),
    ("1.1.1.1", False, False),
    ("100.128.0.1", False, False),
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
    """100.64.0.1 must not be reachable as a webhook target by default."""
    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://100.64.0.1/hook")
    assert exc.value.status_code == 422


async def test_cgnat_address_is_allowed_under_allow_private() -> None:
    """The Tailscale / trusted-LAN escape hatch still opens CGNAT."""
    validated = await validation.validate_webhook_url("https://100.64.0.1/hook", allow_private=True)
    assert validated.resolved_ip == "100.64.0.1"


async def test_cgnat_dns_name_is_blocked_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname resolving into CGNAT is refused too, not just a literal."""
    resolved = str(ipaddress.ip_address("100.64.0.1"))

    async def resolve(_host: str) -> list[str]:
        return [resolved]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)

    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://cgnat.example/hook")
    assert exc.value.status_code == 422


async def test_cgnat_dns_name_is_allowed_under_allow_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch also applies to the DNS path."""
    resolved = str(ipaddress.ip_address("100.64.0.1"))

    async def resolve(_host: str) -> list[str]:
        return [resolved]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)

    validated = await validation.validate_webhook_url("https://cgnat.example/hook", allow_private=True)
    assert validated.resolved_ip == "100.64.0.1"


async def test_6to4_metadata_embedding_is_blocked_even_with_allow_private() -> None:
    """[2002:a9fe:a9fe::] is 169.254.169.254 in another notation."""
    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://[2002:a9fe:a9fe::]/hook", allow_private=True)
    assert exc.value.status_code == 422


async def test_nat64_metadata_embedding_is_blocked_even_with_allow_private() -> None:
    """64:ff9b::a9fe:a9fe reaches the metadata endpoint through a NAT64."""
    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://[64:ff9b::a9fe:a9fe]/hook", allow_private=True)
    assert exc.value.status_code == 422


async def test_6to4_metadata_embedding_from_dns_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DNS path resolves the 6to4 form and must refuse it as well."""
    resolved = str(ipaddress.ip_address("2002:a9fe:a9fe::"))

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
    validated = await validation.validate_webhook_url("https://[2002:0808:0808::]/hook", allow_private=True)
    assert validated.resolved_ip == "2002:808:808::"


async def test_metadata_hostname_stays_blocked_under_allow_private() -> None:
    """The literal hostname blocklist is unaffected by allow_private."""
    with pytest.raises(HTTPException) as exc:
        await validation.validate_webhook_url("https://metadata.google.internal/hook", allow_private=True)
    assert exc.value.status_code == 422


async def test_cgnat_network_constant_is_the_rfc6598_range() -> None:
    assert validation._CGNAT_NETWORK == ipaddress.ip_network("100.64.0.0/10")


async def test_embedded_ipv4_extraction_covers_the_transition_mechanisms() -> None:
    """The embedded-address extraction is what the block decisions rest on."""
    cases = {
        "2002:a9fe:a9fe::": "169.254.169.254",
        "64:ff9b::a9fe:a9fe": "169.254.169.254",
        "::ffff:169.254.169.254": "169.254.169.254",
        "2002:6440:0001::": "100.64.0.1",
    }
    for address, expected in cases.items():
        candidates = validation._embedded_ipv4_candidates(ipaddress.ip_address(address))
        assert ipaddress.IPv4Address(expected) in candidates, (address, candidates)
