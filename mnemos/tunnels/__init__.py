"""MNEMOS public-tunnel bridges.

Publishes a LOOPBACK MNEMOS port (normally the MCP HTTP/SSE edge on 5004)
to a public HTTPS URL, so hosted agents that cannot spawn a local process
— ChatGPT Pro Developer Mode above all — can reach it.

Two backends, both driven by the vendor's agent binary rather than a
vendor SDK:

``cloudflare``
    ``cloudflared`` quick tunnel. No account, no credentials, no signup.
    The default. Ephemeral only — random URL, changes every restart.

``ngrok``
    ``ngrok`` agent. Needs a free-tier authtoken.

The REST surface that drives these lives in
``mnemos.api.routes.tunnels``; the operator-facing helper is
``scripts/mnemos_tunnel_setup.py``.
"""
from __future__ import annotations

from typing import Type

from mnemos.tunnels.base import (
    STARTUP_TIMEOUT_SECONDS,
    TunnelAuthError,
    TunnelBinaryMissingError,
    TunnelBridge,
    TunnelError,
    TunnelInfo,
    TunnelStartError,
)
from mnemos.tunnels.cloudflare_bridge import CloudflareBridge
from mnemos.tunnels.ngrok_bridge import NgrokBridge

#: Backend name -> bridge class. The keys are the REST contract's
#: ``backend`` discriminator and are part of the public API.
BRIDGES: dict[str, Type[TunnelBridge]] = {
    CloudflareBridge.name: CloudflareBridge,
    NgrokBridge.name: NgrokBridge,
}

#: Chosen as the default because a cloudflared quick tunnel needs no
#: account and no credentials, so it works on a host that has nothing
#: configured. ngrok cannot open its first tunnel without a signup.
DEFAULT_BACKEND = CloudflareBridge.name

BACKEND_NAMES = tuple(BRIDGES)


def get_bridge_class(backend: str) -> Type[TunnelBridge]:
    """Resolve a backend name to its bridge class.

    Raises :class:`KeyError` for an unknown name; callers at the REST
    edge turn that into a 422 rather than leaking the exception.
    """
    return BRIDGES[backend]


__all__ = [
    "BACKEND_NAMES",
    "BRIDGES",
    "DEFAULT_BACKEND",
    "STARTUP_TIMEOUT_SECONDS",
    "CloudflareBridge",
    "NgrokBridge",
    "TunnelAuthError",
    "TunnelBinaryMissingError",
    "TunnelBridge",
    "TunnelError",
    "TunnelInfo",
    "TunnelStartError",
    "get_bridge_class",
]
