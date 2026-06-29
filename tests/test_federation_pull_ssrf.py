"""Federation pull SSRF defense (adversarial review F1, 2026-06-28).

The peer ``base_url`` is SSRF-validated once at registration. The pull
path (``_check_peer_schema`` / ``_pull_batch`` / ``pull_memory_by_id``)
previously used plain ``httpx.AsyncClient`` with no re-validation at
fetch time, so a DNS-rebinding TOCTOU between registration and a later
sync could route the authenticated pull (carrying the peer bearer token)
to an internal or cloud-metadata endpoint. These tests verify the pull
path now re-validates via ``make_safe_client`` (validate-then-pin) and
rejects a URL that resolves to a blocked target at fetch time.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from mnemos.domain import federation
from mnemos.core import net_validation as validation


pytestmark = pytest.mark.asyncio


def _reject_metadata_ip(url, *, allow_private=None):
    """Stand-in for validate_webhook_url that rejects a metadata target."""
    raise HTTPException(status_code=422, detail="url host resolves to a non-routable address")


async def test_check_peer_schema_rejects_ssrf_at_fetch_time(monkeypatch):
    """_check_peer_schema re-validates the peer URL at fetch and returns durable."""
    monkeypatch.setattr(validation, "validate_webhook_url", _reject_metadata_ip)

    result = await federation._check_peer_schema(
        base_url="https://peer.example",
        auth_token="sek",
        name="peer-a",
    )

    assert result["ok"] is False
    assert result["transient"] is False
    assert "url-rejected" in result["reason"]


async def test_pull_batch_rejects_ssrf_at_fetch_time(monkeypatch):
    """_pull_batch raises RuntimeError when the peer URL is an SSRF target."""
    monkeypatch.setattr(validation, "validate_webhook_url", _reject_metadata_ip)

    with pytest.raises(RuntimeError, match="federation URL rejected"):
        await federation._pull_batch(
            base_url="https://peer.example",
            auth_token="sek",
            since=None,
            namespace_filter=None,
            category_filter=None,
        )


async def test_pull_memory_by_id_rejects_ssrf_at_fetch_time(monkeypatch):
    """pull_memory_by_id raises RuntimeError when the peer URL is an SSRF target."""
    monkeypatch.setattr(validation, "validate_webhook_url", _reject_metadata_ip)

    with pytest.raises(RuntimeError, match="federation URL rejected"):
        await federation.pull_memory_by_id(
            base_url="https://peer.example",
            auth_token="sek",
            memory_id="mem-1",
            namespace_filter=None,
            category_filter=None,
        )


async def test_make_safe_client_pins_dns_on_success(monkeypatch):
    """make_safe_client returns a client whose transport is pinned to the resolved IP."""
    from mnemos.core.safe_http import make_safe_client

    monkeypatch.setattr(
        validation,
        "validate_webhook_url",
        lambda url, allow_private=None: _async_validate(url),
    )

    client, validated = await make_safe_client(
        "https://peer.example", timeout=5.0, allow_private=False,
    )
    async with client:
        transport = client._transport
        assert transport._pool._network_backend._resolved_ip == "203.0.113.7"
        assert transport._pool._network_backend._hostname == "peer.example"
    assert validated.resolved_ip == "203.0.113.7"


async def _async_validate(url, allow_private=None):
    """Return a ValidatedWebhookURL with a pinned public IP (no real DNS)."""
    from mnemos.core.net_validation import ValidatedWebhookURL

    return ValidatedWebhookURL(
        url=url,
        hostname="peer.example",
        port=443,
        resolved_ip="203.0.113.7",
    )
