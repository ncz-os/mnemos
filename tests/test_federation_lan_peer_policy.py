"""LAN peers must be registrable; metadata endpoints must never be.

MNEMOS federates across a private fleet, but FEDERATION_ALLOW_PRIVATE and
FEDERATION_ALLOW_INSECURE both defaulted false. Peer registration runs the
webhook SSRF guard, so registering http://192.168.207.67:5002 failed with

    {"detail": "url host resolves to a non-routable address"}

i.e. a fleet could not register its own peers. LAN is trusted policy, so both
default true; offsite deployments set them false.

The relaxation must NOT extend to cloud instance-metadata endpoints, which are
never a legitimate peer on any network and are refused unconditionally.
"""

from __future__ import annotations

import pytest

from mnemos.core.config import get_settings
from mnemos.core.net_validation import _BLOCKED_METADATA_HOSTS


def test_lan_defaults_permit_private_peers():
    fed = get_settings().federation
    assert fed.allow_private is True, "a LAN fleet must be able to register RFC1918 peers"


def test_lan_defaults_permit_http_peers():
    fed = get_settings().federation
    assert fed.allow_insecure is True, "fleet peers are addressed over http:// on the LAN"


@pytest.mark.parametrize(
    "host",
    ["metadata.google.internal", "metadata.goog", "metadata.tencentyun.com"],
)
def test_cloud_metadata_hosts_are_always_blocked(host):
    """The LAN relaxation must not open a path to instance metadata."""
    assert host in _BLOCKED_METADATA_HOSTS, (
        f"{host} must be refused regardless of allow_private"
    )


def test_offsite_can_still_lock_both_down(monkeypatch):
    """Offsite federation restores the strict posture via env."""
    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE", "false")
    monkeypatch.setenv("FEDERATION_ALLOW_INSECURE", "false")
    from mnemos.core.config import _reset_settings_for_tests

    _reset_settings_for_tests()
    fed = get_settings().federation
    assert fed.allow_private is False
    assert fed.allow_insecure is False
    _reset_settings_for_tests()
