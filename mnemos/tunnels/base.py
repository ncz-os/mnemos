"""Shared process lifecycle for MNEMOS public-tunnel bridges.

A "tunnel bridge" owns one child process (``ngrok`` or ``cloudflared``)
that publishes a LOOPBACK MNEMOS port to the public internet and reports
the public URL back. Everything specific to a vendor lives in the
subclass; this module owns the parts that are identical either way:

* locating the vendor binary and failing with an install hint rather
  than a ``FileNotFoundError`` traceback,
* spawning the child and draining its output forever (an undrained pipe
  fills at ~64 KiB and then blocks the tunnel agent mid-run, which looks
  like a hung tunnel rather than a plumbing bug),
* answering "is it still alive, and what URL did it get",
* terminating it politely and then not-politely.

Deliberately dependency-light: stdlib + httpx, no vendor SDK. See
``ngrok_bridge`` / ``cloudflare_bridge`` for why per backend.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import socket
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, Deque, Optional

from mnemos.core.config import raw_env_subset

logger = logging.getLogger(__name__)

# How long to wait for a freshly-spawned agent to report a public URL.
# ngrok and cloudflared both do a TLS handshake plus an edge registration
# round trip; 2-5s is typical, 30s is a generous ceiling before we call it
# a failure and clean up.
STARTUP_TIMEOUT_SECONDS = 30.0

# Lines of child stderr/stdout kept for diagnostics. Bounded on purpose:
# a long-lived tunnel agent is chatty and we are not a log shipper.
OUTPUT_RING_SIZE = 50

# Grace period between SIGTERM and SIGKILL on stop().
TERMINATE_TIMEOUT_SECONDS = 5.0


class TunnelError(RuntimeError):
    """Base class for every tunnel failure surfaced to the operator."""


class TunnelBinaryMissingError(TunnelError):
    """The vendor agent binary is not installed on this host.

    Carries an install hint so the route layer can tell the operator what
    to do instead of returning a bare 'not found'.
    """

    def __init__(self, binary: str, install_hint: str) -> None:
        self.binary = binary
        self.install_hint = install_hint
        super().__init__(f"`{binary}` is not installed or not on PATH. {install_hint}")


class TunnelStartError(TunnelError):
    """The agent spawned but never produced a usable public URL."""


class TunnelAuthError(TunnelError):
    """The backend needs credentials the caller did not supply."""


@dataclass(frozen=True)
class TunnelInfo:
    """A running tunnel, as reported back to the operator."""

    backend: str
    url: str
    target_port: int
    pid: int
    started_at: str


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Environment variables a tunnel agent legitimately needs. Everything else
#: in MNEMOS's environment is withheld — see :func:`child_env`.
_INHERITED_ENV_VARS = (
    "PATH",
    "HOME",             # ngrok reads ~/.config/ngrok/ngrok.yml from here
    "XDG_CONFIG_HOME",  # ...and honours this over $HOME/.config when set
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TZ",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",  # custom CA bundles, for hosts behind TLS inspection
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def child_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Build a minimal environment for a vendor tunnel agent.

    MNEMOS's own environment holds the database DSN, the MCP bearer token,
    the OAuth signing key and every provider API key. A tunnel agent needs
    none of them, and handing them over exposes the lot through
    ``/proc/<pid>/environ`` and to a third-party binary that talks to a
    vendor's edge. Allow-list instead of inherit-everything.

    The filter also stops a stray ``NGROK_*`` / ``TUNNEL_*`` variable in
    the daemon's environment from silently reconfiguring the agent behind
    the API's back — only what a bridge passes in ``extra`` gets through.
    """
    env = raw_env_subset(_INHERITED_ENV_VARS)
    if extra:
        env.update(extra)
    return env


def find_free_port() -> int:
    """Reserve an ephemeral loopback port and return it.

    Used to pin a vendor agent's local admin/metrics listener to a port we
    chose, rather than letting it take its compiled-in default and collide
    with a second agent (or with anything else already on 4040). There is a
    TOCTOU window between closing this socket and the child binding it;
    that is acceptable for a locally-spawned helper and is the same trick
    the vendor CLIs' own test suites use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TunnelBridge(ABC):
    """One vendor agent, one child process, at most one tunnel."""

    #: Stable identifier used as the REST ``backend`` discriminator.
    name: ClassVar[str] = ""
    #: Executable looked up on PATH.
    binary: ClassVar[str] = ""
    #: Operator-facing remediation text when :attr:`binary` is absent.
    install_hint: ClassVar[str] = ""
    #: Whether start() requires an authtoken.
    requires_authtoken: ClassVar[bool] = False

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._info: Optional[TunnelInfo] = None
        self._output: Deque[str] = deque(maxlen=OUTPUT_RING_SIZE)
        self._drain_task: Optional[asyncio.Task] = None

    # ── binary discovery ─────────────────────────────────────────────

    @classmethod
    def binary_path(cls) -> Optional[str]:
        return shutil.which(cls.binary)

    @classmethod
    def require_binary(cls) -> str:
        path = cls.binary_path()
        if path is None:
            raise TunnelBinaryMissingError(cls.binary, cls.install_hint)
        return path

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self, *, target_port: int, authtoken: Optional[str] = None) -> TunnelInfo:
        """Spawn the agent and return the tunnel once it has a public URL.

        Raises :class:`TunnelBinaryMissingError`, :class:`TunnelAuthError`,
        or :class:`TunnelStartError`. On any failure the child is reaped
        before the exception propagates, so a failed start never leaves an
        orphan agent holding a tunnel we have lost track of.
        """
        if self.status() is not None:
            raise TunnelStartError(f"{self.name} tunnel is already running")

        # status() clears _proc/_info when the agent exited on its own, but
        # its drain task is still attached to the dead child's pipe. Left
        # running it keeps calling _on_output_line, so a URL still buffered
        # from the PREVIOUS agent could satisfy the NEW start and hand back
        # a dead URL. Reap the remains before spawning again.
        await self._reap()

        self.require_binary()
        self._validate_credentials(authtoken)

        self._output.clear()
        argv, env = self._spawn_spec(target_port=target_port, authtoken=authtoken)
        logger.info("[TUNNEL] starting %s -> 127.0.0.1:%d", self.name, target_port)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self._proc = proc
        self._drain_task = asyncio.ensure_future(self._drain_output(proc))

        try:
            url = await asyncio.wait_for(
                self._await_public_url(proc),
                timeout=STARTUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            await self._reap()
            raise TunnelStartError(
                f"{self.name} did not report a public URL within "
                f"{STARTUP_TIMEOUT_SECONDS:.0f}s. Recent output: {self.recent_output()}"
            ) from exc
        except TunnelError:
            await self._reap()
            raise
        except asyncio.CancelledError:
            # CancelledError is a BaseException, NOT an Exception, so the
            # clause below does not catch it. Without this branch a client
            # disconnect (or lifespan teardown) during the startup wait
            # leaves the agent running with a live public URL and no Python
            # handle on it — exactly the orphan the shutdown hook exists to
            # prevent, except the hook cannot see it either, because
            # `_active_bridge` is only assigned after start() returns.
            await self._reap()
            raise
        except Exception as exc:  # pragma: no cover - defensive
            await self._reap()
            raise TunnelStartError(f"{self.name} failed to start: {exc}") from exc

        self._info = TunnelInfo(
            backend=self.name,
            url=url,
            target_port=target_port,
            pid=proc.pid,
            started_at=_utcnow_iso(),
        )
        logger.info("[TUNNEL] %s is up at %s (pid=%d)", self.name, url, proc.pid)
        return self._info

    def status(self) -> Optional[TunnelInfo]:
        """Return the live tunnel, or None if there isn't one.

        Checks the child is genuinely still running rather than trusting
        the stored record: an agent that crashed or was killed out from
        under us must not keep being reported as a working tunnel.
        """
        if self._proc is None or self._info is None:
            return None
        if self._proc.returncode is not None:
            logger.warning(
                "[TUNNEL] %s exited on its own (rc=%s); clearing state",
                self.name,
                self._proc.returncode,
            )
            self._proc = None
            self._info = None
            return None
        return self._info

    async def stop(self) -> bool:
        """Terminate the agent. Returns True if something was running."""
        if self._proc is None:
            self._info = None
            return False
        was_running = self._proc.returncode is None
        await self._reap()
        return was_running

    def recent_output(self) -> str:
        """Last few lines the agent emitted, for error messages."""
        return " | ".join(self._output) or "(no output captured)"

    # ── subclass contract ────────────────────────────────────────────

    def _validate_credentials(self, authtoken: Optional[str]) -> None:
        """Fail fast when the backend cannot possibly authenticate.

        Default: honour :attr:`requires_authtoken`. Backends that can also
        pick credentials up from an on-disk vendor config (ngrok) override
        this so a host already running ``ngrok config add-authtoken`` does
        not have to re-supply the token. Failing here rather than letting
        the agent spawn turns a 30s startup timeout into an immediate,
        actionable error.
        """
        if self.requires_authtoken and not authtoken:
            raise TunnelAuthError(
                f"the {self.name} backend requires an authtoken; "
                f"none was supplied and none is configured on this host"
            )

    @abstractmethod
    def _spawn_spec(
        self, *, target_port: int, authtoken: Optional[str]
    ) -> tuple[list[str], Optional[dict[str, str]]]:
        """Return ``(argv, env)`` for the vendor agent.

        Secrets belong in ``env``, never in ``argv`` — argv is world-readable
        via ``ps`` on every host this might run on.
        """

    @abstractmethod
    async def _await_public_url(self, proc: asyncio.subprocess.Process) -> str:
        """Block until the agent has a public URL, then return it."""

    def _on_output_line(self, line: str) -> None:
        """Called for every line the agent emits, by the drain task.

        The drain task owns the child's stream exclusively — a second
        reader would race it and lose lines — so backends that discover
        their URL from agent output (cloudflared) hook in here rather than
        reading the pipe themselves. Backends that poll a local API
        (ngrok) ignore it. Must not raise: the drain task is the only
        thing keeping the child's pipe from filling.
        """

    # ── internals ────────────────────────────────────────────────────

    async def _drain_output(self, proc: asyncio.subprocess.Process) -> None:
        """Consume the child's output forever so its pipe never fills."""
        stream = proc.stdout
        if stream is None:  # pragma: no cover - PIPE is always requested
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._output.append(line)
                    try:
                        self._on_output_line(line)
                    except Exception as exc:  # pragma: no cover - defensive
                        # A hook fault must never stop the drain: if this
                        # loop dies the child's pipe fills and the tunnel
                        # wedges, which is far worse than a missed line.
                        logger.warning("[TUNNEL] %s output hook failed: %s", self.name, exc)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[TUNNEL] %s output drain ended: %s", self.name, exc)

    async def _reap(self) -> None:
        """Terminate the child, then cancel the drain task. Idempotent."""
        proc = self._proc
        self._proc = None
        self._info = None
        drain = self._drain_task
        self._drain_task = None

        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_SECONDS)
            except Exception:
                # Broad on purpose: stop() is documented idempotent and the
                # route awaits it with no guard, so anything escaping here
                # becomes a 500 on a teardown the caller cannot retry.
                # Escalate to SIGKILL and move on either way.
                logger.warning("[TUNNEL] %s ignored SIGTERM; killing", self.name)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_SECONDS)
        elif proc is not None:
            with contextlib.suppress(Exception):
                await proc.wait()

        if drain is not None and not drain.done():
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
