"""Bridge-level tests for mnemos.tunnels — real subprocesses, fake agents.

These exercise the actual lifecycle (spawn, output drain, URL discovery,
liveness, terminate) by putting a FAKE ``ngrok`` / ``cloudflared`` on PATH
rather than by mocking ``asyncio.create_subprocess_exec``. Mocking the
spawn would leave exactly the parts that have historically broken —
output-pipe draining, the local-API poll loop, reaping — untested.

Nothing here touches the network or needs a vendor account: the fake
ngrok serves its own ``/api/tunnels`` on loopback, and the fake
cloudflared just prints a banner.
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from mnemos.tunnels import (
    BACKEND_NAMES,
    DEFAULT_BACKEND,
    CloudflareBridge,
    NgrokBridge,
    TunnelAuthError,
    TunnelBinaryMissingError,
    TunnelStartError,
    get_bridge_class,
)
from mnemos.tunnels.ngrok_bridge import _pick_public_url

FAKE_TRYCLOUDFLARE_URL = "https://fake-tunnel-for-tests.trycloudflare.com"
FAKE_NGROK_URL = "https://fake-test-tunnel.ngrok-free.app"


def _install_fake(tmp_path: Path, name: str, body: str) -> Path:
    """Write an executable python script named `name` into tmp_path."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return bindir


FAKE_CLOUDFLARED = """
    import sys, time
    # cloudflared prints the quick-tunnel URL inside a boxed banner.
    print("2026-09-14T00:00:00Z INF Requesting new quick Tunnel")
    print("+----------------------------------------------------+")
    print("|  %s  |" % "{url}")
    print("+----------------------------------------------------+")
    sys.stdout.flush()
    # Stay alive like the real agent; the bridge terminates us.
    time.sleep(300)
"""

FAKE_CLOUDFLARED_DIES = """
    import sys
    sys.stdout.write("ERR failed to dial cloudflare edge\\n")
    sys.stdout.flush()
    sys.exit(1)
"""

# A fake ngrok agent: parses --web-addr, serves the documented
# /api/tunnels payload there, and blocks like the real agent does.
FAKE_NGROK = """
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    argv = sys.argv[1:]
    web_addr = argv[argv.index("--web-addr") + 1]
    host, port = web_addr.rsplit(":", 1)

    payload = json.dumps({{"tunnels": [
        {{"public_url": "http://insecure.example", "proto": "http"}},
        {{"public_url": "{url}", "proto": "https"}},
    ]}}).encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/tunnels":
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *a):
            pass

    HTTPServer((host, int(port)), H).serve_forever()
"""


@pytest.fixture
def fake_path(tmp_path, monkeypatch):
    """Return a helper that installs a fake agent and puts it on PATH."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))

    def install(name: str, body: str) -> None:
        _install_fake(tmp_path, name, body)

    return install


# ── registry ─────────────────────────────────────────────────────────────


def test_registry_exposes_both_backends():
    assert set(BACKEND_NAMES) == {"cloudflare", "ngrok"}
    assert get_bridge_class("cloudflare") is CloudflareBridge
    assert get_bridge_class("ngrok") is NgrokBridge


def test_default_backend_is_cloudflare():
    """Cloudflare quick tunnels need no account; ngrok cannot open its
    first tunnel without a signup. The zero-credential backend is the
    right default for a helper whose whole point is low friction."""
    assert DEFAULT_BACKEND == "cloudflare"


def test_unknown_backend_raises_keyerror():
    with pytest.raises(KeyError):
        get_bridge_class("tailscale")


# ── missing binary ───────────────────────────────────────────────────────


@pytest.mark.parametrize("bridge_cls", [CloudflareBridge, NgrokBridge])
def test_missing_binary_carries_install_hint(bridge_cls, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(TunnelBinaryMissingError) as excinfo:
        bridge_cls.require_binary()
    message = str(excinfo.value)
    assert bridge_cls.binary in message
    # The operator must be told what to install, not just that it's absent.
    assert "install" in message.lower()


@pytest.mark.asyncio
async def test_start_without_binary_does_not_spawn(tmp_path, monkeypatch):
    empty = tmp_path / "empty2"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    bridge = CloudflareBridge()
    with pytest.raises(TunnelBinaryMissingError):
        await bridge.start(target_port=5004)
    assert bridge.status() is None


# ── cloudflare ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cloudflare_start_status_stop(fake_path):
    fake_path("cloudflared", FAKE_CLOUDFLARED.format(url=FAKE_TRYCLOUDFLARE_URL))
    bridge = CloudflareBridge()
    info = await bridge.start(target_port=5004)
    try:
        assert info.url == FAKE_TRYCLOUDFLARE_URL
        assert info.backend == "cloudflare"
        assert info.target_port == 5004
        assert info.pid > 0
        assert bridge.status() is not None
        assert bridge.status().url == FAKE_TRYCLOUDFLARE_URL
    finally:
        stopped = await bridge.stop()
    assert stopped is True
    assert bridge.status() is None
    # Idempotent: a second stop is a no-op, not an error.
    assert await bridge.stop() is False


@pytest.mark.asyncio
async def test_cloudflare_quick_tunnel_needs_no_authtoken(fake_path):
    """The whole reason cloudflare is the default backend."""
    fake_path("cloudflared", FAKE_CLOUDFLARED.format(url=FAKE_TRYCLOUDFLARE_URL))
    bridge = CloudflareBridge()
    info = await bridge.start(target_port=5004, authtoken=None)
    try:
        assert info.url.endswith(".trycloudflare.com")
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_cloudflare_agent_death_before_url_is_a_clear_error(fake_path):
    fake_path("cloudflared", FAKE_CLOUDFLARED_DIES)
    bridge = CloudflareBridge()
    with pytest.raises(TunnelStartError) as excinfo:
        await bridge.start(target_port=5004)
    # The agent's own reason must reach the operator, not just "it failed".
    assert "failed to dial cloudflare edge" in str(excinfo.value)
    assert bridge.status() is None


@pytest.mark.asyncio
async def test_second_start_while_running_is_rejected(fake_path):
    fake_path("cloudflared", FAKE_CLOUDFLARED.format(url=FAKE_TRYCLOUDFLARE_URL))
    bridge = CloudflareBridge()
    await bridge.start(target_port=5004)
    try:
        with pytest.raises(TunnelStartError, match="already running"):
            await bridge.start(target_port=5005)
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_status_reports_not_running_after_agent_dies(fake_path):
    """A crashed agent must not keep being reported as a live tunnel."""
    fake_path("cloudflared", FAKE_CLOUDFLARED.format(url=FAKE_TRYCLOUDFLARE_URL))
    bridge = CloudflareBridge()
    info = await bridge.start(target_port=5004)
    assert bridge.status() is not None

    os.kill(info.pid, 9)
    await bridge._proc.wait()  # deterministic: don't race the reaper

    assert bridge.status() is None


def test_cloudflare_target_port_is_loopback_only():
    """The published target is always localhost — the bridge never exposes
    another host's port, whatever target_port is set to."""
    bridge = CloudflareBridge()
    argv, _env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert "--url" in argv
    assert argv[argv.index("--url") + 1] == "http://localhost:5004"


# ── ngrok ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ngrok_start_reads_url_from_local_api(fake_path):
    fake_path("ngrok", FAKE_NGROK.format(url=FAKE_NGROK_URL))
    bridge = NgrokBridge()
    info = await bridge.start(target_port=5004, authtoken="2abcDEF_faketokenfortests")
    try:
        # https must win over the sibling http tunnel ngrok also opens.
        assert info.url == FAKE_NGROK_URL
        assert info.backend == "ngrok"
    finally:
        await bridge.stop()
    assert bridge.status() is None


@pytest.mark.asyncio
async def test_ngrok_without_credentials_fails_fast(fake_path, monkeypatch):
    """No authtoken, no env var, no ngrok.yml -> immediate actionable
    error, not a 30-second startup timeout."""
    fake_path("ngrok", FAKE_NGROK.format(url=FAKE_NGROK_URL))
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    monkeypatch.setattr(NgrokBridge, "stored_authtoken_path", staticmethod(lambda: None))
    bridge = NgrokBridge()
    with pytest.raises(TunnelAuthError) as excinfo:
        await bridge.start(target_port=5004)
    assert "dashboard.ngrok.com" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ngrok_accepts_host_stored_authtoken(fake_path, monkeypatch, tmp_path):
    """A host that already ran `ngrok config add-authtoken` must not be
    forced to re-supply the token through the REST API."""
    fake_path("ngrok", FAKE_NGROK.format(url=FAKE_NGROK_URL))
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    stored = tmp_path / "ngrok.yml"
    stored.write_text("version: 2\n")
    monkeypatch.setattr(NgrokBridge, "stored_authtoken_path", staticmethod(lambda: stored))
    bridge = NgrokBridge()
    info = await bridge.start(target_port=5004)
    try:
        assert info.url == FAKE_NGROK_URL
    finally:
        await bridge.stop()


def test_ngrok_authtoken_never_appears_in_argv():
    """argv is world-readable via `ps`; this token owns the operator's
    whole ngrok account. It must travel in the environment only."""
    bridge = NgrokBridge()
    secret = "2zzzTOPSECRET_authtoken_value"
    argv, env = bridge._spawn_spec(target_port=5004, authtoken=secret)
    assert secret not in " ".join(argv)
    assert env["NGROK_AUTHTOKEN"] == secret


def test_ngrok_web_addr_is_pinned_to_loopback():
    bridge = NgrokBridge()
    argv, _env = bridge._spawn_spec(target_port=5004, authtoken="x")
    web_addr = argv[argv.index("--web-addr") + 1]
    assert web_addr.startswith("127.0.0.1:")
    assert int(web_addr.split(":")[1]) > 0


# ── /api/tunnels payload parsing ─────────────────────────────────────────


def test_pick_public_url_prefers_https():
    payload = {
        "tunnels": [
            {"public_url": "http://a.ngrok-free.app"},
            {"public_url": "https://a.ngrok-free.app"},
        ]
    }
    assert _pick_public_url(payload) == "https://a.ngrok-free.app"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tunnels": []},
        {"tunnels": "not-a-list"},
        {"tunnels": [{"public_url": ""}]},
        {"tunnels": [{"no_url": 1}]},
        "not-a-dict",
        None,
    ],
)
def test_pick_public_url_returns_none_for_unusable_payloads(payload):
    """The poll loop must keep waiting on a half-initialised agent rather
    than crash or return garbage."""
    assert _pick_public_url(payload) is None


# ── regressions from adversarial review (2026-09-14) ─────────────────────


def test_child_env_withholds_mnemos_secrets(monkeypatch):
    """MNEMOS's environment carries the DB DSN, the MCP bearer token and
    the OAuth signing key. A vendor tunnel agent needs none of them, and
    inheriting them exposes the lot via /proc/<pid>/environ."""
    from mnemos.tunnels.base import child_env

    monkeypatch.setenv("MNEMOS_DATABASE_DSN", "postgres://u:secret@db/mnemos")
    monkeypatch.setenv("MNEMOS_MCP_TOKEN", "super-secret-mcp-token")
    monkeypatch.setenv("MNEMOS_OAUTH_SIGNING_KEY", "super-secret-signing-key")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/mnemos")

    env = child_env()
    assert "MNEMOS_DATABASE_DSN" not in env
    assert "MNEMOS_MCP_TOKEN" not in env
    assert "MNEMOS_OAUTH_SIGNING_KEY" not in env
    # ...but the agent still needs these.
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/mnemos"  # ngrok reads its config from here


def test_child_env_does_not_leak_ambient_vendor_vars(monkeypatch):
    """A stray NGROK_AUTHTOKEN in the daemon's environment must not
    silently reconfigure the agent behind the API's back."""
    from mnemos.tunnels.base import child_env

    monkeypatch.setenv("NGROK_AUTHTOKEN", "ambient-token")
    monkeypatch.setenv("TUNNEL_TOKEN", "ambient-cf-token")
    assert "NGROK_AUTHTOKEN" not in child_env()
    assert "TUNNEL_TOKEN" not in child_env()
    assert child_env({"NGROK_AUTHTOKEN": "explicit"})["NGROK_AUTHTOKEN"] == "explicit"


@pytest.mark.parametrize("bridge_cls", [CloudflareBridge, NgrokBridge])
def test_spawn_env_excludes_mnemos_secrets(bridge_cls, monkeypatch):
    monkeypatch.setenv("MNEMOS_OAUTH_SIGNING_KEY", "signing-key-must-not-leak")
    _argv, env = bridge_cls()._spawn_spec(target_port=5004, authtoken="tok")
    assert "MNEMOS_OAUTH_SIGNING_KEY" not in env


@pytest.mark.asyncio
async def test_cancellation_during_startup_reaps_the_child(fake_path):
    """CancelledError is a BaseException, so `except Exception` misses it.
    A client disconnect mid-startup must not orphan an agent holding a
    live public URL with no Python handle on it."""
    import asyncio

    # An agent that never prints a URL, so start() is still waiting when
    # we cancel it.
    fake_path("cloudflared", "\nimport time\ntime.sleep(300)\n")
    bridge = CloudflareBridge()
    task = asyncio.ensure_future(bridge.start(target_port=5004))

    # Let it spawn before cancelling.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if bridge._proc is not None:
            break
    assert bridge._proc is not None, "fake agent never spawned"
    pid = bridge._proc.pid

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bridge.status() is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped, not orphaned


@pytest.mark.asyncio
async def test_restart_after_agent_death_does_not_return_the_dead_url(fake_path):
    """status() clears _proc/_info when the agent exits on its own but
    leaves the drain task on the dead pipe. A restart on the same bridge
    must not be satisfied by the previous agent's buffered URL."""
    stale = "https://stale-dead-tunnel.trycloudflare.com"
    fake_path("cloudflared", FAKE_CLOUDFLARED.format(url=stale))
    bridge = CloudflareBridge()
    info = await bridge.start(target_port=5004)
    assert info.url == stale

    os.kill(info.pid, 9)
    await bridge._proc.wait()
    assert bridge.status() is None

    fresh = "https://fresh-live-tunnel.trycloudflare.com"
    fake_path("cloudflared", FAKE_CLOUDFLARED.format(url=fresh))
    info2 = await bridge.start(target_port=5004)
    try:
        assert info2.url == fresh, "restart returned the dead agent's URL"
    finally:
        await bridge.stop()


def test_ngrok_config_lookup_honours_xdg_config_home(monkeypatch, tmp_path):
    """ngrok prefers $XDG_CONFIG_HOME/ngrok/ngrok.yml over $HOME/.config.
    child_env() must forward the variable and the stored-token probe must
    look there, or a host that set it appears to have no authtoken."""
    from mnemos.tunnels.base import child_env
    from mnemos.tunnels.ngrok_bridge import _ngrok_config_paths

    xdg = tmp_path / "xdg"
    (xdg / "ngrok").mkdir(parents=True)
    cfg = xdg / "ngrok" / "ngrok.yml"
    cfg.write_text("version: 2\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert _ngrok_config_paths()[0] == cfg
    assert NgrokBridge.stored_authtoken_path() == cfg
    assert child_env()["XDG_CONFIG_HOME"] == str(xdg)
