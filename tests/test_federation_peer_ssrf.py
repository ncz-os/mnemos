from __future__ import annotations

import pytest
from fastapi import HTTPException

from mnemos.api.routes import federation
from mnemos.core import config
from mnemos.core import net_validation as validation


pytestmark = pytest.mark.asyncio


def _reset_settings(monkeypatch, *, allow_private: bool = False):
    monkeypatch.setenv("FEDERATION_ALLOW_INSECURE", "true")
    # Set the value explicitly rather than unsetting it. Deleting the variable
    # means "use the default", and the default is now the trusted-LAN posture
    # (allow_private=True), so unsetting no longer expresses the strict case.
    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE", "true" if allow_private else "false")
    monkeypatch.setattr(config, "_settings", None)


async def test_peer_url_rejects_metadata_ip(monkeypatch):
    _reset_settings(monkeypatch)

    async def resolve(_host):
        return ["169.254.169.254"]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)

    with pytest.raises(HTTPException) as exc:
        await federation._validate_peer_base_url("https://peer.example")

    assert exc.value.status_code == 422


async def test_peer_url_rejects_localhost_unless_private_allowed(monkeypatch):
    """Private addressing is gated by the flag, in both directions.

    The default is now the trusted-LAN posture (allow_private=True), so the
    rejecting half declares allow_private=False explicitly -- that is the
    OFFSITE posture this half is actually about.
    """
    _reset_settings(monkeypatch, allow_private=False)
    with pytest.raises(HTTPException):
        await federation._validate_peer_base_url("http://127.0.0.1:5002")

    _reset_settings(monkeypatch, allow_private=True)
    await federation._validate_peer_base_url("http://127.0.0.1:5002")


async def test_allow_private_never_unblocks_metadata_by_dns(monkeypatch):
    """The LAN relaxation must not open the rebinding path.

    A hostname resolving to 169.254.169.254 is only caught by the resolved-IP
    check; the literal-string blocklist misses it. That check must apply even
    with allow_private=True, or trusting the LAN would hand out SSRF to cloud
    instance metadata.
    """
    _reset_settings(monkeypatch, allow_private=True)

    async def resolve(_host):
        return ["169.254.169.254"]

    monkeypatch.setattr(validation, "_resolve_addrs", resolve)
    with pytest.raises(HTTPException) as exc:
        await federation._validate_peer_base_url("https://peer.example")
    assert exc.value.status_code == 422
