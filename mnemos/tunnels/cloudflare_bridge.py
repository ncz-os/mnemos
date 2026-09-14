"""Cloudflare Tunnel backend for the MNEMOS public-tunnel bridge.

Shells out to ``cloudflared`` in **quick-tunnel** mode::

    cloudflared tunnel --url http://localhost:<port>

Quick tunnels need no Cloudflare account, no domain, no
``cloudflared tunnel login``, and no credentials of any kind — the agent
registers anonymously and prints a ``https://<random>.trycloudflare.com``
URL. That zero-credential property is why this, not ngrok, is the default
backend for the tunnel API: ngrok's free tier requires a signup and an
authtoken paste before the first tunnel can open at all.

Scope limit, stated plainly: this is EPHEMERAL only. The URL is random,
it changes every restart, and quick tunnels carry no uptime guarantee
from Cloudflare. NAMED tunnels — the ones that give a stable
``mnemos.yourdomain.com`` and are what ``docs/connectors/`` recommends
for anything long-lived — need a Cloudflare account, a zone, and DNS
records, and are still a manual ``cloudflared`` setup documented there.
This bridge does not create, manage, or route them.

There is no ``cloudflared`` Python SDK and none is wanted; the agent
binary is the supported integration surface.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from mnemos.tunnels.base import TunnelBridge, TunnelStartError, child_env

# cloudflared announces the quick tunnel inside a boxed banner, e.g.
#   |  https://foo-bar-baz-qux.trycloudflare.com                     |
# so match the URL anywhere on the line rather than anchoring.
_QUICK_TUNNEL_URL = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9-]*\.trycloudflare\.com")


class CloudflareBridge(TunnelBridge):
    name = "cloudflare"
    binary = "cloudflared"
    requires_authtoken = False
    install_hint = (
        "Install cloudflared: `brew install cloudflared` (macOS), "
        "`sudo apt install cloudflared` / the .deb from "
        "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ "
        "(Linux) — then re-run."
    )

    def __init__(self) -> None:
        super().__init__()
        self._url: Optional[str] = None
        self._url_seen: Optional[asyncio.Event] = None

    def _spawn_spec(
        self, *, target_port: int, authtoken: Optional[str]
    ) -> tuple[list[str], Optional[dict[str, str]]]:
        self._url = None
        self._url_seen = asyncio.Event()
        argv = [
            self.binary,
            "tunnel",
            # Quiet the self-update check: on a server it either no-ops or
            # restarts the agent mid-tunnel, and neither helps us.
            "--no-autoupdate",
            "--url",
            f"http://localhost:{target_port}",
        ]
        # authtoken is accepted and ignored: quick tunnels are anonymous.
        # A NAMED tunnel's token is a different credential with a different
        # command shape, and silently treating one as the other would open
        # the wrong tunnel. See the module docstring.
        #
        # child_env() rather than os.environ: MNEMOS's environment carries
        # the database DSN, the MCP bearer token and the OAuth signing key,
        # none of which a tunnel agent needs, and all of which would become
        # readable via /proc/<pid>/environ and visible to a third-party
        # binary that talks to a vendor edge.
        return argv, child_env()

    def _on_output_line(self, line: str) -> None:
        if self._url is not None:
            return
        match = _QUICK_TUNNEL_URL.search(line)
        if match:
            self._url = match.group(0)
            if self._url_seen is not None:
                self._url_seen.set()

    async def _await_public_url(self, proc: asyncio.subprocess.Process) -> str:
        """Wait for the drain task to spot the trycloudflare.com URL.

        Also watches the child: cloudflared exits immediately on things
        like a bad flag or a blocked outbound 7844, and waiting the full
        startup timeout for a process that is already dead just delays a
        clear error.
        """
        if self._url_seen is None:  # pragma: no cover - set by _spawn_spec
            raise TunnelStartError("cloudflared URL event was never created")
        waiter = asyncio.ensure_future(self._url_seen.wait())
        exited = asyncio.ensure_future(proc.wait())
        try:
            done, _pending = await asyncio.wait(
                {waiter, exited}, return_when=asyncio.FIRST_COMPLETED
            )
            if waiter in done and self._url:
                return self._url
            raise TunnelStartError(
                f"cloudflared exited with code {proc.returncode} before "
                f"reporting a tunnel URL. Output: {self.recent_output()}"
            )
        finally:
            for task in (waiter, exited):
                if not task.done():
                    task.cancel()
