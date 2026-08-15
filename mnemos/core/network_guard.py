"""Refuse to publish an unauthenticated API to the network.

When authentication is disabled every protected route receives a synthetic
``root`` principal, so a non-loopback bind hands full administrative access to
anyone who can reach the port.

The check lives here rather than in the CLI because the published container
images do not go through ``mnemos serve`` — they exec uvicorn directly — so a
CLI-only guard leaves the exact deployment the review flagged unprotected. Both
entry points call into this module:

* ``mnemos serve`` knows its bind address, validates it, and records that
  decision in :data:`BIND_CHECKED_ENV` for the app it is about to start.
* The ASGI app checks at startup. If the marker is absent it was launched
  directly (uvicorn, gunicorn, a custom runner) and cannot see the bind
  address, so it fails closed instead of assuming loopback.
"""

from __future__ import annotations

import ipaddress
import os

#: Escape hatch for the deliberate "open box on a trusted LAN" case. Named to be
#: uncomfortable to type, because it disables the only thing standing between an
#: unauthenticated root-equivalent API and every host that can reach the port.
UNSAFE_NETWORK_BIND_ENV = "MNEMOS_ALLOW_UNAUTHENTICATED_NETWORK_BIND"

#: Set by ``mnemos serve`` to the host it validated. Internal handoff only — it
#: is not a supported way to suppress the app-side check, because any value here
#: still has to name a loopback address to satisfy it.
BIND_CHECKED_ENV = "_MNEMOS_BIND_VALIDATED_HOST"

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "127.0.0.0/8"}

_TRUTHY = {"1", "true", "yes"}


def is_loopback_bind(host: str) -> bool:
    """True only for addresses that cannot receive traffic from another host."""
    candidate = (host or "").strip().strip("[]")
    if not candidate:
        return False
    if candidate in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # Not a bare address (a hostname, or the 0.0.0.0/:: wildcards, which land
        # here as non-loopback). Treat anything unrecognised as reachable —
        # failing closed is the whole point of this check.
        return False


def unsafe_bind_allowed() -> bool:
    return os.environ.get(UNSAFE_NETWORK_BIND_ENV, "").strip().lower() in _TRUTHY


def auth_is_enabled() -> bool:
    from mnemos.core.config import get_settings

    settings = get_settings()
    return bool(getattr(getattr(settings, "auth", None), "enabled", False))


def refusal_reason(
    host: str | None, what: str, auth_enabled: bool | None = None
) -> str | None:
    """Why startup must abort, or None when this configuration is safe.

    ``host`` is None when the caller cannot determine the bind address, which is
    treated as reachable. ``auth_enabled`` lets a caller that has already
    resolved settings pass them in rather than have them re-read here.
    """
    if host is not None and is_loopback_bind(host):
        return None
    if auth_enabled if auth_enabled is not None else auth_is_enabled():
        return None
    if unsafe_bind_allowed():
        return None
    where = f"bind {host}" if host is not None else "start"
    return (
        f"{what} refuses to {where} with authentication disabled: every "
        f"protected route would receive an unauthenticated root principal, so "
        f"any host that can reach this port gains full administrative access.\n"
        f"Fix one of:\n"
        f"  - enable authentication (profile 'server', or set an API key), or\n"
        f"  - bind loopback only: --host 127.0.0.1\n"
        f"If an open port on a trusted network really is intended, set "
        f"{UNSAFE_NETWORK_BIND_ENV}=1."
    )


def record_validated_bind(host: str) -> None:
    """Tell the app process which host ``mnemos serve`` already validated."""
    os.environ[BIND_CHECKED_ENV] = host


def validated_bind_host() -> str | None:
    """The host a parent ``mnemos serve`` validated, if any."""
    return os.environ.get(BIND_CHECKED_ENV) or None
