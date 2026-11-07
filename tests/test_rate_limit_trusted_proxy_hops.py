"""MN-14 / CF-23: rate-limit bucketing must not trust a client-chosen XFF.

``RATE_LIMIT_TRUST_PROXY=true`` exists to say "a reverse proxy I control is in
front of me". The contract it needs is *which* proxy that is, because only the
RIGHT-most ``X-Forwarded-For`` entries were written by infrastructure. Every
entry a client sends sits to the LEFT of them: a proxy appends the address it
actually saw to whatever header the client supplied, so with one trusted hop

    X-Forwarded-For: 1.2.3.4, 203.0.113.9

``1.2.3.4`` is attacker-controlled text and ``203.0.113.9`` is the peer the
proxy really saw. The old ``rate_limit.py`` returned ``xff.split(",")[0]`` --
the left-most entry -- so any client could mint a fresh bucket per request by
sending a random left-most value and never hit a limit. The trust switch
turned rate limiting OFF for exactly the deployments that enabled it.

The fix is hop counting: ``RATE_LIMIT_TRUSTED_PROXY_HOPS`` (default 1) says how
many proxies append to XFF. Bucket on the ``hops``-th entry counted from the
right, and refuse to bucket on a client-influenced value at all when the header
is shorter than that (a lying or misconfigured chain collapses to the direct
peer instead of to attacker text).
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from mnemos.core import rate_limit
from mnemos.core.config import _RateLimitSettings

#: RFC 5737 TEST-NET-3 addresses: documentation-only, never routable.
CLIENT = "198.51.100.7"  # left-most; attacker-controlled text under trust
PROXY_PEER = "203.0.113.9"  # what the proxy actually saw; right-most
SPOOFED = "192.0.2.66"  # what the attacker claims


def _request(headers: dict[str, str] | None = None, peer: str = PROXY_PEER) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "client": (peer, 12345)})


@pytest.fixture
def trust_proxy(monkeypatch):
    """Enable proxy trust for the duration of one test (default: 1 hop)."""

    def _enable(hops: int | None = None) -> None:
        monkeypatch.setattr(rate_limit, "RATE_LIMIT_TRUST_PROXY", True)
        if hops is not None:
            monkeypatch.setattr(rate_limit, "RATE_LIMIT_TRUSTED_PROXY_HOPS", hops)

    return _enable

def test_client_supplied_left_most_entry_does_not_change_the_bucket(trust_proxy):
    """The spoof this item exists for: rotating the left-most XFF entry is a no-op."""
    trust_proxy()

    first = rate_limit._client_ip(_request({"x-forwarded-for": f"{SPOOFED}, {PROXY_PEER}"}))
    second = rate_limit._client_ip(_request({"x-forwarded-for": f"{CLIENT}, {PROXY_PEER}"}))

    assert first == PROXY_PEER
    assert second == PROXY_PEER


def test_single_hop_returns_the_right_most_entry(trust_proxy):
    trust_proxy()
    assert rate_limit._client_ip(_request({"x-forwarded-for": f"{CLIENT}, {PROXY_PEER}"})) == PROXY_PEER


def test_two_trusted_hops_return_the_second_entry_from_the_right(trust_proxy):
    """CDN -> ingress -> app: the CDN wrote the client address two from the end."""
    trust_proxy(hops=2)
    header = f"{SPOOFED}, {CLIENT}, {PROXY_PEER}"
    assert rate_limit._client_ip(_request({"x-forwarded-for": header})) == CLIENT


def test_header_shorter_than_the_trusted_chain_collapses_to_the_direct_peer(trust_proxy):
    """A client that sends no XFF (or fakes one) gets its real socket address."""
    trust_proxy(hops=2)

    assert rate_limit._client_ip(_request({"x-forwarded-for": SPOOFED})) == PROXY_PEER
    assert rate_limit._client_ip(_request()) == PROXY_PEER


def test_empty_and_blank_entries_do_not_shift_the_hop_count(trust_proxy):
    """Malformed XFF must not move the bucket to a client-chosen value."""
    trust_proxy()
    header = f"{SPOOFED}, , {PROXY_PEER} ,"
    assert rate_limit._client_ip(_request({"x-forwarded-for": header})) == PROXY_PEER


def test_trust_disabled_ignores_the_header_entirely(monkeypatch):
    """Unchanged contract: with no proxy trust the socket peer is the bucket key."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_TRUST_PROXY", False)
    assert rate_limit._client_ip(_request({"x-forwarded-for": SPOOFED})) == PROXY_PEER


def test_hop_setting_is_configurable_from_the_environment(monkeypatch):
    """The hop count is operator-settable; a typo must not silently trust 0 hops."""
    monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXY_HOPS", "3")
    assert _RateLimitSettings().trusted_proxy_hops == 3

    monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXY_HOPS", "0")
    assert _RateLimitSettings().trusted_proxy_hops == 1

    monkeypatch.delenv("RATE_LIMIT_TRUSTED_PROXY_HOPS")
    assert _RateLimitSettings().trusted_proxy_hops == 1


# ── the bucket key is ALWAYS a str, whatever the header layer hands back ──────
#
# Starlette 1.6 decodes header values in ``Headers.getlist``, so ``_forwarded_
# client_ip`` sees ``str`` and this is belt-and-braces. It is worth pinning
# anyway: the bucket key is compared and stored downstream, a ``bytes`` key
# would never match the ``str`` peer returned by ``get_remote_address``, and
# the raw-ASGI/other-framework shape of ``headers.getlist`` has changed across
# versions. These tests fail if the boundary stops coercing.


@pytest.mark.parametrize(
    "raw",
    [f"{CLIENT}, {PROXY_PEER}".encode(), CLIENT.encode()],
    ids=["comma-joined", "single"],
)
def test_bucket_key_is_str_even_if_the_header_layer_returns_bytes(trust_proxy, monkeypatch, raw):
    # One trusted hop: the right-most entry is the address our proxy saw.
    trust_proxy(hops=1)

    class _BytesHeaders:
        """A header store that returns bytes, like a non-decoding ASGI layer."""

        def getlist(self, name):
            return [raw] if name == "x-forwarded-for" else []

    request = _request({"x-forwarded-for": f"{CLIENT}, {PROXY_PEER}"})
    object.__setattr__(request, "_headers", _BytesHeaders())

    key = rate_limit._client_ip(request)
    assert isinstance(key, str), f"bucket key must be str, got {type(key).__name__}"
    assert key == PROXY_PEER


def test_blank_entries_can_never_become_the_bucket_key(trust_proxy):
    """No mixture of blanks/whitespace may yield "" or a whitespace key."""
    trust_proxy()

    for header in ("   ", ",", " , , ", f"{SPOOFED},   ,", " , " + PROXY_PEER):
        key = rate_limit._client_ip(_request({"x-forwarded-for": header}))
        assert key.strip(), f"blank bucket key from header {header!r}"
        assert isinstance(key, str)
