"""An unauthenticated MNEMOS API must never come up reachable off-host.

The original guard lived only in ``mnemos serve``, but the published images
exec uvicorn directly (``python -m uvicorn mnemos.api.main:app --host
0.0.0.0``), so the deployment the review actually flagged never reached it.
These tests pin both halves: the policy itself, and that the ASGI lifespan
enforces it for launch paths that bypass the CLI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mnemos.core import network_guard

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(network_guard.UNSAFE_NETWORK_BIND_ENV, raising=False)
    monkeypatch.delenv(network_guard.BIND_CHECKED_ENV, raising=False)


def _set_auth(monkeypatch, enabled: bool):
    monkeypatch.setattr(network_guard, "auth_is_enabled", lambda: enabled)


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "::1", "localhost", "127.0.0.5"], ids=lambda h: h
)
def test_loopback_binds_are_allowed_without_auth(monkeypatch, host):
    _set_auth(monkeypatch, False)
    assert network_guard.refusal_reason(host, "mnemos serve") is None


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.207.67", "mnemos.internal"], ids=lambda h: h
)
def test_reachable_binds_are_refused_without_auth(monkeypatch, host):
    _set_auth(monkeypatch, False)
    reason = network_guard.refusal_reason(host, "mnemos serve")
    assert reason is not None
    assert "authentication disabled" in reason


def test_unknown_bind_fails_closed(monkeypatch):
    # A direct uvicorn launch cannot report its host; assuming loopback there is
    # precisely how the published image stayed exposed.
    _set_auth(monkeypatch, False)
    assert network_guard.refusal_reason(None, "the MNEMOS API") is not None


def test_authenticated_deployments_may_bind_anywhere(monkeypatch):
    _set_auth(monkeypatch, True)
    assert network_guard.refusal_reason("0.0.0.0", "mnemos serve") is None
    assert network_guard.refusal_reason(None, "the MNEMOS API") is None


def test_explicit_escape_hatch_permits_an_open_box(monkeypatch):
    _set_auth(monkeypatch, False)
    monkeypatch.setenv(network_guard.UNSAFE_NETWORK_BIND_ENV, "1")
    assert network_guard.refusal_reason("0.0.0.0", "mnemos serve") is None


def test_serve_handoff_is_only_honoured_for_a_loopback_host(monkeypatch):
    # The marker records what `mnemos serve` validated; it is not a bypass, so a
    # non-loopback value in it still refuses.
    _set_auth(monkeypatch, False)
    network_guard.record_validated_bind("0.0.0.0")
    assert network_guard.refusal_reason(network_guard.validated_bind_host(), "x") is not None
    network_guard.record_validated_bind("127.0.0.1")
    assert network_guard.refusal_reason(network_guard.validated_bind_host(), "x") is None


@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_an_unauthenticated_open_api(monkeypatch):
    """The direct-uvicorn path the images used must now fail closed."""
    from mnemos.core import lifecycle

    _set_auth(monkeypatch, False)
    with pytest.raises(RuntimeError, match="authentication disabled"):
        async with lifecycle.lifespan(object()):
            pytest.fail("lifespan started an unauthenticated, reachable API")


def test_published_images_do_not_bypass_the_guard():
    """Every image CMD must run a path that validates its bind address."""
    offenders = []
    for dockerfile in sorted(REPO_ROOT.glob("Dockerfile*")):
        for line in dockerfile.read_text().splitlines():
            if not line.startswith("CMD"):
                continue
            if re.search(r"\buvicorn\b|\bgunicorn\b", line):
                offenders.append(f"{dockerfile.name}: {line}")
    assert not offenders, (
        "these images exec an ASGI server directly, bypassing the bind "
        "validation in `mnemos serve`:\n" + "\n".join(offenders)
    )
