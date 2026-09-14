"""Route tests for /admin/tunnels/* — auth, the host opt-in gate, backend
dispatch, token issuance, and the single-tunnel invariant.

No vendor binary and no network: the bridge is replaced with a fake whose
start/status/stop are pure. The bridges themselves are covered against
real subprocesses in ``test_tunnels_bridges.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes import tunnels as tr
from mnemos.tunnels.base import (
    TunnelAuthError,
    TunnelBinaryMissingError,
    TunnelInfo,
    TunnelStartError,
)

TUNNEL_PATHS = ("/admin/tunnels/start", "/admin/tunnels/status", "/admin/tunnels/stop")


# ── fakes ────────────────────────────────────────────────────────────────


class FakeBridge:
    """Stand-in for a TunnelBridge with no subprocess behind it."""

    name = "cloudflare"
    raise_on_start: BaseException | None = None

    def __init__(self) -> None:
        self._info: TunnelInfo | None = None

    async def start(self, *, target_port: int, authtoken=None) -> TunnelInfo:
        if type(self).raise_on_start is not None:
            raise type(self).raise_on_start
        self._info = TunnelInfo(
            backend=self.name,
            url="https://fake.trycloudflare.com",
            target_port=target_port,
            pid=4242,
            started_at="2026-09-14T00:00:00+00:00",
        )
        return self._info

    def status(self) -> TunnelInfo | None:
        return self._info

    async def stop(self) -> bool:
        was = self._info is not None
        self._info = None
        return was


def _settings(*, tunnels_enabled=True, mcp_token="static-mcp-token"):
    return SimpleNamespace(mcp=SimpleNamespace(token=mcp_token, tokens="", tunnels_enabled=tunnels_enabled))


@pytest.fixture(autouse=True)
def _clean_module_state():
    tr._reset_active_for_tests()
    FakeBridge.raise_on_start = None
    yield
    tr._reset_active_for_tests()
    FakeBridge.raise_on_start = None


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(tr, "get_settings", lambda: _settings())


@pytest.fixture
def fake_bridge(monkeypatch):
    monkeypatch.setattr(tr, "get_bridge_class", lambda backend: FakeBridge)


@pytest.fixture
def app():
    from mnemos.api.main import app as real_app

    return real_app


def _client(app, *, role="root", user_id="root"):
    """TestClient whose caller is a UserContext with the given role.

    ``get_current_user`` is overridden (not ``require_root``) so the real
    ``require_root`` check is the thing under test.
    """
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id, group_ids=[], role=role, namespace="default", authenticated=True
    )
    return TestClient(app)


def _call(client, method: str, path: str):
    """Issue the request. Only POST carries a body; this TestClient's
    GET/DELETE signatures don't accept `json=`."""
    if method == "post":
        return client.post(path, json={})
    return getattr(client, method)(path)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from mnemos.api.main import app as real_app

    real_app.dependency_overrides.clear()


# ── auth ─────────────────────────────────────────────────────────────────


def test_every_tunnel_route_is_gated_by_require_root(app):
    """Structural: the root gate must be wired on all three routes.

    Asserted directly against the dependency graph so that removing
    ``Depends(require_root)`` from any handler fails here, even if some
    other test happens to authenticate as root anyway.
    """
    from mnemos.api.dependencies import require_root

    seen = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path in TUNNEL_PATHS:
            calls = {dep.call for dep in route.dependant.dependencies}
            seen[path] = require_root in calls
    assert set(seen) == set(TUNNEL_PATHS), f"missing routes: {set(TUNNEL_PATHS) - set(seen)}"
    assert all(seen.values()), f"routes missing require_root: {seen}"


@pytest.mark.parametrize("method,path", [
    ("post", "/admin/tunnels/start"),
    ("get", "/admin/tunnels/status"),
    ("delete", "/admin/tunnels/stop"),
])
def test_non_root_caller_is_rejected(app, enabled, method, path):
    client = _client(app, role="user", user_id="alice")
    response = _call(client, method, path)
    assert response.status_code == 403
    assert "Root access required" in response.json()["detail"]


@pytest.mark.parametrize("method,path", [
    ("post", "/admin/tunnels/start"),
    ("get", "/admin/tunnels/status"),
    ("delete", "/admin/tunnels/stop"),
])
def test_unauthenticated_caller_is_rejected(app, enabled, method, path, monkeypatch):
    """With auth on and no credentials, every tunnel route must 401."""
    import mnemos.api.dependencies as deps

    monkeypatch.setattr(deps, "_auth_enabled", True)

    def _no_backend(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    monkeypatch.setattr(deps, "_auth_backend", _no_backend)
    response = _call(TestClient(app), method, path)
    assert response.status_code == 401


# ── host opt-in gate ─────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("post", "/admin/tunnels/start"),
    ("get", "/admin/tunnels/status"),
    ("delete", "/admin/tunnels/stop"),
])
def test_routes_are_403_until_the_host_opts_in(app, monkeypatch, method, path):
    """Root auth alone is not enough: a leaked root key must not be able
    to publish this instance to the internet."""
    monkeypatch.setattr(tr, "get_settings", lambda: _settings(tunnels_enabled=False))
    response = _call(_client(app), method, path)
    assert response.status_code == 403
    # The remedy has to be in the message, or the operator is stuck.
    assert "MNEMOS_TUNNELS_ENABLED" in response.json()["detail"]


# ── backend dispatch ─────────────────────────────────────────────────────


def test_start_dispatches_to_the_requested_backend(app, enabled, monkeypatch):
    chosen = []

    def _get(backend):
        chosen.append(backend)
        return FakeBridge

    monkeypatch.setattr(tr, "get_bridge_class", _get)
    response = _client(app).post("/admin/tunnels/start", json={"backend": "ngrok"})
    assert response.status_code == 200, response.text
    assert chosen == ["ngrok"]


def test_start_defaults_to_cloudflare(app, enabled, monkeypatch):
    chosen = []
    monkeypatch.setattr(tr, "get_bridge_class", lambda b: (chosen.append(b), FakeBridge)[1])
    response = _client(app).post("/admin/tunnels/start", json={})
    assert response.status_code == 200, response.text
    assert chosen == ["cloudflare"]


def test_unknown_backend_is_422(app, enabled, fake_bridge):
    response = _client(app).post("/admin/tunnels/start", json={"backend": "tailscale"})
    assert response.status_code == 422


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_out_of_range_target_port_is_422(app, enabled, fake_bridge, port):
    response = _client(app).post("/admin/tunnels/start", json={"target_port": port})
    assert response.status_code == 422


# ── bridge failure mapping ───────────────────────────────────────────────


def test_missing_binary_is_503_with_install_hint(app, enabled, fake_bridge):
    FakeBridge.raise_on_start = TunnelBinaryMissingError(
        "cloudflared", "Install cloudflared: `brew install cloudflared`."
    )
    response = _client(app).post("/admin/tunnels/start", json={})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "cloudflared" in detail
    assert "brew install cloudflared" in detail


def test_missing_credentials_is_422(app, enabled, fake_bridge):
    FakeBridge.raise_on_start = TunnelAuthError("ngrok requires an authtoken. Sign up at ...")
    response = _client(app).post("/admin/tunnels/start", json={"backend": "ngrok"})
    assert response.status_code == 422
    assert "authtoken" in response.json()["detail"]


def test_agent_startup_failure_is_502(app, enabled, fake_bridge):
    FakeBridge.raise_on_start = TunnelStartError("cloudflared exited with code 1")
    response = _client(app).post("/admin/tunnels/start", json={})
    assert response.status_code == 502


def test_failed_start_leaves_no_active_tunnel(app, enabled, fake_bridge):
    FakeBridge.raise_on_start = TunnelStartError("boom")
    _client(app).post("/admin/tunnels/start", json={})
    assert _client(app).get("/admin/tunnels/status").json()["running"] is False


# ── token issuance ───────────────────────────────────────────────────────


def test_token_prefers_oauth_over_static(app, enabled, fake_bridge, monkeypatch):
    """The OAuth path is the scoped, expiring credential — it must win
    whenever the MCP authorization server is configured.

    Patches ``tr._oauth_service``, the seam in the route module itself,
    not ``mnemos.mcp.oauth.get_oauth_service``: other tests in this suite
    pop and re-import ``mnemos.mcp.*`` from ``sys.modules``, so a patch
    aimed at that module survives only in a lucky ordering.
    """
    issued = {}

    class _Service:
        def issue_access_token(self, *, client_id, provider, lifetime):
            issued.update(client_id=client_id, provider=provider, lifetime=lifetime)
            return "oauth-jwt-token"

    monkeypatch.setattr(tr, "_oauth_service", lambda: _Service())
    body = _client(app).post("/admin/tunnels/start", json={}).json()
    assert body["token"] == "oauth-jwt-token"
    assert body["token_source"] == "oauth"
    assert body["expires_in"] == tr.TUNNEL_TOKEN_SECONDS
    assert issued["lifetime"] == tr.TUNNEL_TOKEN_SECONDS


def test_token_falls_back_to_static_mcp_token(app, enabled, fake_bridge, monkeypatch):
    def _unconfigured():
        raise RuntimeError("MNEMOS_OAUTH_ISSUER is not configured")

    monkeypatch.setattr(tr, "_oauth_service", _unconfigured)
    body = _client(app).post("/admin/tunnels/start", json={}).json()
    assert body["token"] == "static-mcp-token"
    assert body["token_source"] == "static"
    assert body["expires_in"] is None


def test_no_credential_available_is_503_and_opens_no_tunnel(app, fake_bridge, monkeypatch):
    """A token the MCP edge would reject is worse than no tunnel: the
    operator would paste it into ChatGPT and get an opaque 401."""
    monkeypatch.setattr(tr, "get_settings", lambda: _settings(mcp_token=""))
    monkeypatch.setattr(
        tr, "_oauth_service",
        lambda: (_ for _ in ()).throw(RuntimeError("not configured")),
    )
    response = _client(app).post("/admin/tunnels/start", json={})
    assert response.status_code == 503
    assert "MNEMOS_MCP_TOKEN" in response.json()["detail"]
    assert tr._active_bridge is None


def test_per_user_token_map_is_not_used_as_a_credential(app, fake_bridge, monkeypatch):
    """Handing out one entry of MNEMOS_MCP_TOKENS would grant that user's
    identity to whoever holds the connector URL."""
    settings = SimpleNamespace(
        mcp=SimpleNamespace(token="", tokens="alice:key1,bob:key2", tunnels_enabled=True)
    )
    monkeypatch.setattr(tr, "get_settings", lambda: settings)
    monkeypatch.setattr(
        tr, "_oauth_service",
        lambda: (_ for _ in ()).throw(RuntimeError("not configured")),
    )
    response = _client(app).post("/admin/tunnels/start", json={})
    assert response.status_code == 503
    assert "key1" not in response.text and "key2" not in response.text


# ── lifecycle ────────────────────────────────────────────────────────────


def test_status_start_stop_round_trip(app, enabled, fake_bridge):
    client = _client(app)
    assert client.get("/admin/tunnels/status").json()["running"] is False

    started = client.post("/admin/tunnels/start", json={"target_port": 5004}).json()
    assert started["url"] == "https://fake.trycloudflare.com"
    assert started["backend"] == "cloudflare"
    assert started["target_port"] == 5004
    assert started["pid"] == 4242

    status = client.get("/admin/tunnels/status").json()
    assert status["running"] is True
    assert status["url"] == "https://fake.trycloudflare.com"
    assert status["pid"] == 4242

    stopped = client.delete("/admin/tunnels/stop").json()
    assert stopped["stopped"] is True
    assert stopped["backend"] == "cloudflare"
    assert client.get("/admin/tunnels/status").json()["running"] is False


def test_second_start_while_running_is_409(app, enabled, fake_bridge):
    client = _client(app)
    client.post("/admin/tunnels/start", json={})
    response = client.post("/admin/tunnels/start", json={})
    assert response.status_code == 409
    assert "/admin/tunnels/stop" in response.json()["detail"]


def test_stop_with_nothing_running_is_not_an_error(app, enabled, fake_bridge):
    response = _client(app).delete("/admin/tunnels/stop")
    assert response.status_code == 200
    assert response.json() == {"stopped": False, "backend": None}


@pytest.mark.asyncio
async def test_shutdown_hook_closes_an_open_tunnel(monkeypatch):
    """Otherwise the agent outlives MNEMOS and keeps a public URL live
    against a port that no longer answers."""
    monkeypatch.setattr(tr, "get_settings", lambda: _settings())
    bridge = FakeBridge()
    await bridge.start(target_port=5004)
    tr._active_bridge = bridge

    await tr.shutdown_active_tunnel()

    assert tr._active_bridge is None
    assert bridge.status() is None


@pytest.mark.asyncio
async def test_shutdown_hook_is_safe_with_no_tunnel():
    tr._reset_active_for_tests()
    await tr.shutdown_active_tunnel()  # must not raise
    assert tr._active_bridge is None
