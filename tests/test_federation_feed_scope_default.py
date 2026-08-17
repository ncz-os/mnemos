"""LAN federation replicates the full corpus; offsite is opt-in restricted.

`create_memory` writes permission_mode 600, and the federation feed's
world-read gate requires `permission_mode % 10 >= 4`. 600 % 10 == 0, so with
the include-private flag defaulted OFF the feed offered nothing: peers reported
`{"pulled": 0, "new": 0, "updated": 0}` -- success, silently -- and the fleet
had not moved a memory since 2026-05-23.

MNEMOS is deployed as a trusted LAN fleet, so full-corpus replication is the
correct default. Offsite deployments set the flag to 0 and get the restrictive
world-readable-only scope.
"""

from __future__ import annotations

import pytest

from mnemos.core.config import federation_feed_include_private

ENV = "MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE"


def test_unset_defaults_to_lan_full_corpus(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert federation_feed_include_private() is True, (
        "an unconfigured LAN install must replicate the full corpus, "
        "otherwise every permission_mode-600 memory is silently unfederatable"
    )


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE", "Off"])
def test_offsite_opt_out_is_honoured(monkeypatch, val):
    """Offsite federation restricts the feed to world-readable memories."""
    monkeypatch.setenv(ENV, val)
    assert federation_feed_include_private() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_explicit_enable_still_works(monkeypatch, val):
    monkeypatch.setenv(ENV, val)
    assert federation_feed_include_private() is True


def test_unrecognised_value_stays_on_the_lan_default(monkeypatch):
    """A typo must not silently switch a LAN fleet into offsite mode."""
    monkeypatch.setenv(ENV, "maybe")
    assert federation_feed_include_private() is True


def test_the_600_case_that_wedged_the_fleet(monkeypatch):
    """permission_mode 600 fails the world-read gate; the default must cover it."""
    assert 600 % 10 < 4, "600 is owner-only, which is exactly the problem"
    monkeypatch.delenv(ENV, raising=False)
    assert federation_feed_include_private() is True
