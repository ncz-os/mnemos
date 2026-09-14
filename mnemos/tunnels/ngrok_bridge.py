"""ngrok backend for the MNEMOS public-tunnel bridge.

Shells out to the ngrok **agent CLI** rather than embedding ngrok Inc's
``ngrok`` Python SDK. Reason: the SDK is not a declared MNEMOS dependency
(see ``pyproject.toml`` — nothing in the base install or any extra pulls
it), and adding a vendor-specific runtime dep to core so that one optional
convenience route works is the wrong trade. Operators who want ngrok
install the agent the same way the connector docs already tell them to
(``brew install ngrok`` / ``snap install ngrok``), and MNEMOS stays
dependency-neutral between the two backends.

The public URL is read back from the agent's local API
(``http://127.0.0.1:<web_port>/api/tunnels``), which is ngrok's documented
introspection surface and is more robust than scraping the terminal UI.
We pin that listener to a port we chose instead of the compiled-in 4040
so a second agent — or anything else already on 4040 — does not make the
tunnel fail to start.

The authtoken travels in the child's ENVIRONMENT (``NGROK_AUTHTOKEN``),
never in argv: argv is readable by every user on the host via ``ps``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import httpx

from mnemos.core.config import raw_env
from mnemos.tunnels.base import (
    TunnelAuthError,
    TunnelBridge,
    TunnelStartError,
    child_env,
    find_free_port,
)

# Where the ngrok agent stores a token written by `ngrok config add-authtoken`.
# Checked so a host that has already done that step does not have to pass a
# token through the REST API again.
def _ngrok_config_paths() -> tuple[Path, ...]:
    """Candidate ngrok config locations, XDG_CONFIG_HOME first.

    Computed per call rather than at import: the daemon's HOME is not
    guaranteed to be set before this module is imported, and a
    module-level ``Path.home()`` would freeze whatever was true then.
    """
    paths = []
    xdg = raw_env("XDG_CONFIG_HOME").strip()
    if xdg:
        paths.append(Path(xdg) / "ngrok" / "ngrok.yml")
    home = Path.home()
    paths += [
        home / ".config" / "ngrok" / "ngrok.yml",
        home / ".ngrok2" / "ngrok.yml",
        home / "Library" / "Application Support" / "ngrok" / "ngrok.yml",
    ]
    return tuple(paths)

_POLL_INTERVAL_SECONDS = 0.25


class NgrokBridge(TunnelBridge):
    name = "ngrok"
    binary = "ngrok"
    requires_authtoken = True
    install_hint = (
        "Install the ngrok agent: `brew install ngrok` (macOS), "
        "`snap install ngrok` (Linux), or download from "
        "https://ngrok.com/download — then re-run."
    )

    def __init__(self) -> None:
        super().__init__()
        self._web_port: Optional[int] = None

    # ── credentials ──────────────────────────────────────────────────

    @staticmethod
    def stored_authtoken_path() -> Optional[Path]:
        """Return an existing ngrok config file, if the host has one."""
        for path in _ngrok_config_paths():
            try:
                if path.is_file():
                    return path
            except OSError:  # pragma: no cover - unreadable home dir
                continue
        return None

    def _validate_credentials(self, authtoken: Optional[str]) -> None:
        if authtoken:
            return
        if raw_env("NGROK_AUTHTOKEN").strip():
            return
        if self.stored_authtoken_path() is not None:
            return
        raise TunnelAuthError(
            "ngrok requires an authtoken. Sign up at "
            "https://dashboard.ngrok.com/signup, copy the token from "
            "https://dashboard.ngrok.com/get-started/your-authtoken, and "
            "either pass it as `authtoken` or run "
            "`ngrok config add-authtoken <token>` on the MNEMOS host."
        )

    # ── spawn ────────────────────────────────────────────────────────

    def _spawn_spec(
        self, *, target_port: int, authtoken: Optional[str]
    ) -> tuple[list[str], Optional[dict[str, str]]]:
        self._web_port = find_free_port()
        argv = [
            self.binary,
            "http",
            str(target_port),
            # Pinned so _await_public_url knows exactly where to look.
            "--web-addr",
            f"127.0.0.1:{self._web_port}",
            # Machine-readable output into the drain ring, so a failure
            # message quotes ngrok's own reason instead of TUI escape codes.
            "--log",
            "stdout",
            "--log-format",
            "logfmt",
        ]
        # Environment, not argv — argv is visible in `ps` to every local
        # user, and this token controls the operator's whole ngrok account.
        # child_env() also withholds MNEMOS's own environment (DSN, MCP
        # token, OAuth signing key) from the vendor binary.
        extra = {"NGROK_AUTHTOKEN": authtoken} if authtoken else None
        return argv, child_env(extra)

    # ── URL discovery ────────────────────────────────────────────────

    async def _await_public_url(self, proc: asyncio.subprocess.Process) -> str:
        """Poll the agent's local API until a public HTTPS URL appears."""
        if self._web_port is None:  # pragma: no cover - set by _spawn_spec
            raise TunnelStartError("ngrok local API port was never assigned")
        api_url = f"http://127.0.0.1:{self._web_port}/api/tunnels"

        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                if proc.returncode is not None:
                    raise TunnelStartError(
                        f"ngrok exited with code {proc.returncode} before "
                        f"opening a tunnel. Output: {self.recent_output()}"
                    )
                url = await self._poll_once(client, api_url)
                if url:
                    return url
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _poll_once(self, client: httpx.AsyncClient, api_url: str) -> Optional[str]:
        """One local-API read. Returns a URL, or None to keep waiting.

        Connection errors are expected while the agent is still binding its
        local listener, so they are not fatal — only a dead child (checked
        by the caller) or the overall startup timeout ends the wait.
        """
        try:
            response = await client.get(api_url)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return _pick_public_url(payload)


def _pick_public_url(payload: Any) -> Optional[str]:
    """Extract the HTTPS public URL from an ngrok /api/tunnels payload.

    ngrok opens an http:// and an https:// tunnel for the same target;
    we want the https one, because every connector surface MNEMOS
    documents (ChatGPT, Claude Desktop, Cursor, Codex) requires TLS and
    the tunnel is the only thing terminating it.
    """
    if not isinstance(payload, dict):
        return None
    tunnels = payload.get("tunnels")
    if not isinstance(tunnels, list):
        return None
    fallback: Optional[str] = None
    for tunnel in tunnels:
        if not isinstance(tunnel, dict):
            continue
        url = tunnel.get("public_url")
        if not isinstance(url, str) or not url:
            continue
        if url.startswith("https://"):
            return url
        fallback = fallback or url
    return fallback
