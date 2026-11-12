"""Characterisation tests for ``mnemos/api/routes/tunnels.py``.

These pin the module's CURRENT observable behaviour — not an ideal one.
If a future change alters any assertion here, that change is behavioural
and should be a deliberate decision, not a side effect.

Scope is the route module only: request models, the host opt-in gate, the
token-issuance preference order, bridge-failure to HTTP mapping, the
single-tunnel invariant, and the lifespan shutdown hook.

Everything external is faked: no vendor binary is spawned, no network is
touched, no clock is read, and no database is contacted. The bridge is a
pure in-memory fake, so each test is deterministic and sleep-free.

The routes are exercised by calling the handlers directly rather than
through ``TestClient``: the installed FastAPI version (see
``requirements.txt``'s ``<0.137`` cap) sits above the pin, and its
``_IncludedRouter`` wrapper hides routes from ``app.routes`` introspection.
Calling the coroutines directly keeps this file independent of that
environment skew while still covering every branch of this module.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import tunnels as tr
from mnemos.tunnels import (
    TunnelAuthError,
    TunnelBinaryMissingError,
    TunnelError,
    TunnelInfo,
    TunnelStartError,
)
from mnemos.tunnels.base import TunnelBridge

# ── fakes and fixtures ───────────────────────────────────────────────────


class FakeBridge:
    """In-memory stand-in for a TunnelBridge.

    ``start`` records its arguments and either raises the class-level
    ``raise_on_start`` or installs a :class:`TunnelInfo`. ``status``
    returns whatever is installed, so a test can simulate an agent that
    died on its own by clearing it.
    """

    name = "cloudflare"
    raise_on_start: BaseException | None = None
    start_calls: list[dict] = []

    def __init__(self) -> None:
        self._info: TunnelInfo | None = None
        self.stop_calls = 0

    async def start(self, *, target_port: int, authtoken=None) -> TunnelInfo:
        type(self).start_calls.append({"target_port": target_port, "authtoken": authtoken})
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
        self.stop_calls += 1
        was = self._info is not None
        self._info = None
        return was


class FakeNgrokBridge(FakeBridge):
    name = "ngrok"


def _settings(*, tunnels_enabled=True, mcp_token="static-mcp-token"):
    return SimpleNamespace(mcp=SimpleNamespace(token=mcp_token, tokens="", tunnels_enabled=tunnels_enabled))


def _patch_settings(monkeypatch, **kwargs):
    """Make ``get_settings()`` answer with ``_settings(**kwargs)``.

    The route resolves ``get_settings`` from its own module globals, so the
    patch is applied to ``tr`` directly: an import of the function would
    rebind only this file's name, and patching ``mnemos.core.config`` would
    miss a route module that was re-imported after this file was loaded.
    monkeypatch restores the original afterwards.
    """
    monkeypatch.setattr(tr, "get_settings", lambda: _settings(**kwargs))
    return tr


def _root_user() -> UserContext:
    return UserContext(
        user_id="root-user",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


@pytest.fixture(autouse=True)
def _isolate_module_state(request, monkeypatch):
    """Reset every piece of module-level and fake-class state per test.

    Tests that request the ``real_oauth_seam`` fixture keep the module's
    real ``_oauth_service`` (that is what those tests pin), so the stub is
    not installed for them.
    """
    tr._reset_active_for_tests()
    FakeBridge.raise_on_start = None
    FakeBridge.start_calls = []
    FakeNgrokBridge.raise_on_start = None
    FakeNgrokBridge.start_calls = []
    # Default: enabled, with a static token, and OAuth unavailable. Tests
    # override the specific seam they care about.
    _patch_settings(monkeypatch)
    if "real_oauth_seam" not in request.fixturenames:
        monkeypatch.setattr(tr, "_oauth_service", _oauth_unavailable)
    yield
    tr._reset_active_for_tests()
    FakeBridge.raise_on_start = None
    FakeBridge.start_calls = []
    FakeNgrokBridge.raise_on_start = None
    FakeNgrokBridge.start_calls = []


@pytest.fixture(autouse=True)
def _route_module_is_the_live_one():
    """Run against the route module the rest of the repo currently uses.

    Other suites in this repo pop and re-import ``mnemos.api.routes.*`` and
    ``mnemos.core.config``. This file binds the route module at import time,
    so after such a reload ``tr`` would be a superseded copy: its patches
    would land on an object no request reaches. Re-binding ``tr`` to the
    module the process actually serves keeps every patch here effective.
    """
    global tr
    tr = importlib.import_module("mnemos.api.routes.tunnels")
    yield
    tr._reset_active_for_tests()


@pytest.fixture
def real_oauth_seam():
    """Opt out of the autouse ``_oauth_service`` stub for one test.

    Purely a signal consumed by ``_isolate_module_state``; it yields nothing.
    """
    yield


def _oauth_unavailable():
    raise RuntimeError("MNEMOS_OAUTH_ISSUER is not configured")


@pytest.fixture
def fake_bridge(monkeypatch):
    monkeypatch.setattr(tr, "get_bridge_class", lambda backend: FakeBridge)
    return FakeBridge


def _request(**kwargs) -> tr.TunnelStartRequest:
    return tr.TunnelStartRequest(**kwargs)


def _run(coro):
    """Run a handler coroutine on a fresh loop (no pytest-asyncio needed)."""
    return asyncio.run(coro)


# ── module constants and router wiring ───────────────────────────────────


def test_token_lifetime_is_twelve_hours():
    """A connector token outlives a short interactive session but is bounded."""
    assert tr.TUNNEL_TOKEN_SECONDS == 12 * 60 * 60
    assert tr.TUNNEL_TOKEN_SECONDS == 43200


def test_default_target_port_is_the_mcp_http_edge():
    assert tr.DEFAULT_TARGET_PORT == 5004


def test_router_is_mounted_under_the_admin_tunnels_prefix():
    assert tr.router.prefix == "/admin/tunnels"


def test_router_exposes_exactly_three_routes_with_expected_methods():
    routes = {r.path: set(r.methods) for r in tr.router.routes}
    assert routes == {
        "/admin/tunnels/start": {"POST"},
        "/admin/tunnels/status": {"GET"},
        "/admin/tunnels/stop": {"DELETE"},
    }


def test_backend_names_are_the_two_supported_vendors():
    assert tr.BACKEND_NAMES == ("cloudflare", "ngrok")
    assert tr.DEFAULT_BACKEND == "cloudflare"


def test_every_route_is_gated_by_require_root():
    """Structural pin: dropping Depends(require_root) from any handler fails."""
    from mnemos.api.dependencies import require_root

    seen = {}
    for route in tr.router.routes:
        calls = {dep.call for dep in route.dependant.dependencies}
        seen[route.path] = require_root in calls
    assert set(seen) == {"/admin/tunnels/start", "/admin/tunnels/status", "/admin/tunnels/stop"}
    assert all(seen.values()), f"routes missing require_root: {seen}"


# ── TunnelStartRequest ───────────────────────────────────────────────────


def test_start_request_defaults_to_cloudflare_with_no_authtoken():
    req = tr.TunnelStartRequest()
    assert req.backend == "cloudflare"
    assert req.authtoken is None
    assert req.target_port == 5004


@pytest.mark.parametrize("backend", ["cloudflare", "ngrok"])
def test_start_request_accepts_each_known_backend(backend):
    assert tr.TunnelStartRequest(backend=backend).backend == backend


@pytest.mark.parametrize("backend", ["tailscale", "", "Cloudflare", None, 1])
def test_start_request_rejects_unknown_backends(backend):
    """The Literal type is the first line of defence — 422 before any handler body."""
    with pytest.raises(ValidationError):
        tr.TunnelStartRequest(backend=backend)


@pytest.mark.parametrize("port", [1, 2, 5004, 65534, 65535])
def test_start_request_accepts_in_range_ports_including_both_boundaries(port):
    assert tr.TunnelStartRequest(target_port=port).target_port == port


@pytest.mark.parametrize("port", [0, -1, -5004, 65536, 999999])
def test_start_request_rejects_out_of_range_ports(port):
    """ge=1, le=65535 — 0 and 65536 are the boundary-adjacent rejections."""
    with pytest.raises(ValidationError):
        tr.TunnelStartRequest(target_port=port)


def test_start_request_keeps_a_supplied_authtoken_verbatim():
    req = tr.TunnelStartRequest(authtoken="2abc_secret")
    assert req.authtoken == "2abc_secret"


def test_start_request_accepts_an_empty_authtoken_string():
    """Empty is accepted by the model; credential policy is the bridge's call."""
    assert tr.TunnelStartRequest(authtoken="").authtoken == ""


def test_start_request_authtoken_is_optional_none():
    assert tr.TunnelStartRequest(authtoken=None).authtoken is None


# ── response models ──────────────────────────────────────────────────────


def test_status_response_defaults_to_not_running_with_every_field_null():
    resp = tr.TunnelStatusResponse(running=False)
    assert resp.model_dump() == {
        "running": False,
        "backend": None,
        "url": None,
        "target_port": None,
        "pid": None,
        "started_at": None,
    }


def test_stop_response_defaults_backend_to_none():
    resp = tr.TunnelStopResponse(stopped=False)
    assert resp.stopped is False
    assert resp.backend is None


def test_start_response_expires_in_defaults_to_none():
    """None signals a static token, which does not expire."""
    resp = tr.TunnelStartResponse(
        url="https://x",
        token="t",
        token_source="static",
        backend="cloudflare",
        target_port=5004,
        pid=1,
        started_at="2026-01-01T00:00:00+00:00",
    )
    assert resp.expires_in is None


@pytest.mark.parametrize("source", ["oauth", "static"])
def test_start_response_accepts_both_token_sources(source):
    resp = tr.TunnelStartResponse(
        url="https://x",
        token="t",
        token_source=source,
        backend="cloudflare",
        target_port=5004,
        pid=1,
        started_at="2026-01-01T00:00:00+00:00",
    )
    assert resp.token_source == source


def test_start_response_rejects_an_unknown_token_source():
    with pytest.raises(ValidationError):
        tr.TunnelStartResponse(
            url="https://x",
            token="t",
            token_source="cookie",
            backend="cloudflare",
            target_port=5004,
            pid=1,
            started_at="2026-01-01T00:00:00+00:00",
        )


# ── _require_enabled: the host opt-in gate ───────────────────────────────


def test_require_enabled_passes_when_the_host_opted_in():
    assert tr._require_enabled() is None


def test_require_enabled_raises_403_when_the_flag_is_false(monkeypatch):
    _patch_settings(monkeypatch, tunnels_enabled=False)
    with pytest.raises(HTTPException) as ei:
        tr._require_enabled()
    assert ei.value.status_code == 403


def test_require_enabled_403_names_the_env_var_and_config_key(monkeypatch):
    """The operator must be able to act on the message alone."""
    _patch_settings(monkeypatch, tunnels_enabled=False)
    with pytest.raises(HTTPException) as ei:
        tr._require_enabled()
    detail = ei.value.detail
    assert "MNEMOS_TUNNELS_ENABLED=true" in detail
    assert "tunnels_enabled = true" in detail
    assert "disabled on this host" in detail


def test_require_enabled_403_mentions_restart(monkeypatch):
    _patch_settings(monkeypatch, tunnels_enabled=False)
    with pytest.raises(HTTPException) as ei:
        tr._require_enabled()
    assert "restart MNEMOS" in ei.value.detail


def test_require_enabled_treats_a_truthy_non_bool_as_enabled(monkeypatch):
    """The gate is a plain truthiness test, not `is True`."""
    _patch_settings(monkeypatch, tunnels_enabled=1)
    assert tr._require_enabled() is None


# ── _http_error_for: bridge failure -> status code ───────────────────────


def test_binary_missing_maps_to_503():
    exc = TunnelBinaryMissingError("cloudflared", "Install cloudflared: `brew install cloudflared`.")
    http = tr._http_error_for(exc)
    assert http.status_code == 503
    assert str(http.detail) == str(exc)


def test_binary_missing_detail_carries_binary_and_install_hint():
    exc = TunnelBinaryMissingError("ngrok", "Install ngrok from https://ngrok.com/download.")
    http = tr._http_error_for(exc)
    assert "`ngrok` is not installed or not on PATH." in http.detail
    assert "https://ngrok.com/download" in http.detail


def test_auth_error_maps_to_422():
    http = tr._http_error_for(TunnelAuthError("ngrok requires an authtoken"))
    assert http.status_code == 422
    assert http.detail == "ngrok requires an authtoken"


def test_start_error_maps_to_502():
    http = tr._http_error_for(TunnelStartError("cloudflared exited with code 1"))
    assert http.status_code == 502
    assert http.detail == "cloudflared exited with code 1"


def test_bare_tunnel_error_maps_to_502():
    """The catch-all: a TunnelError that is none of the specific kinds."""
    http = tr._http_error_for(TunnelError("something broke"))
    assert http.status_code == 502
    assert http.detail == "something broke"


def test_auth_error_takes_precedence_over_binary_missing_check_order():
    """TunnelAuthError is not a TunnelBinaryMissingError, so it is 422 not 503."""
    assert tr._http_error_for(TunnelAuthError("x")).status_code == 422
    assert tr._http_error_for(TunnelBinaryMissingError("b", "h")).status_code == 503


# ── _oauth_service: the lazy-import seam ─────────────────────────────────


def _live_oauth_module():
    """Return the ``mnemos.mcp.oauth`` module the seam will actually import.

    Resolved through ``importlib`` at call time, never bound at import
    time: other suites in this repo pop and re-import ``mnemos.mcp.*``,
    which leaves any module-level ``import`` in this file holding a stale
    object whose patch would be invisible to the seam. This resolves the
    same object the seam's own inline import will use.
    """
    import importlib

    return importlib.import_module("mnemos.mcp.oauth")


def test_oauth_service_delegates_to_the_mcp_edge_factory(monkeypatch, real_oauth_seam):
    """The seam returns whatever mnemos.mcp.oauth.get_oauth_service returns.

    The real import is exercised (not patched): the seam exists so the
    route module need not import the MCP edge at startup, and it must stay
    a pure pass-through to that factory.
    """
    sentinel = object()
    monkeypatch.setattr(_live_oauth_module(), "get_oauth_service", lambda: sentinel)
    assert tr._oauth_service() is sentinel


def test_oauth_service_propagates_a_factory_failure(monkeypatch, real_oauth_seam):
    """An unconfigured OAuth server raises; callers decide whether to fall back."""

    def _boom():
        raise RuntimeError("MNEMOS_OAUTH_ISSUER is not configured")

    monkeypatch.setattr(_live_oauth_module(), "get_oauth_service", _boom)
    with pytest.raises(RuntimeError, match="MNEMOS_OAUTH_ISSUER is not configured"):
        tr._oauth_service()


# ── _issue_tunnel_token ──────────────────────────────────────────────────


def test_issue_prefers_an_oauth_token_over_the_static_token(monkeypatch):
    issued = {}

    class _Service:
        def issue_access_token(self, *, client_id, provider, lifetime):
            issued.update(client_id=client_id, provider=provider, lifetime=lifetime)
            return "oauth-jwt"

    monkeypatch.setattr(tr, "_oauth_service", lambda: _Service())
    token, source, expires_in = tr._issue_tunnel_token()
    assert (token, source, expires_in) == ("oauth-jwt", "oauth", tr.TUNNEL_TOKEN_SECONDS)


def test_issue_requests_the_tunnel_lifetime_and_client_identity(monkeypatch):
    issued = {}

    class _Service:
        def issue_access_token(self, *, client_id, provider, lifetime):
            issued.update(client_id=client_id, provider=provider, lifetime=lifetime)
            return "oauth-jwt"

    monkeypatch.setattr(tr, "_oauth_service", lambda: _Service())
    tr._issue_tunnel_token()
    assert issued == {
        "client_id": "mnemos-tunnel",
        "provider": "mnemos-tunnel",
        "lifetime": tr.TUNNEL_TOKEN_SECONDS,
    }


def test_issue_falls_back_to_the_static_token_when_oauth_is_unavailable(monkeypatch):
    """RuntimeError (the real 'not configured' shape) is swallowed."""
    monkeypatch.setattr(tr, "_oauth_service", _oauth_unavailable)
    _patch_settings(monkeypatch, mcp_token="static-abc")
    assert tr._issue_tunnel_token() == ("static-abc", "static", None)


@pytest.mark.parametrize("exc", [RuntimeError("no issuer"), ImportError("no module"), ValueError("bad")])
def test_issue_falls_back_for_any_oauth_exception(monkeypatch, exc):
    """The except clause is broad on purpose: any failure falls back."""

    def _boom():
        raise exc

    monkeypatch.setattr(tr, "_oauth_service", _boom)
    _patch_settings(monkeypatch, mcp_token="static-abc")
    assert tr._issue_tunnel_token() == ("static-abc", "static", None)


def test_issue_logs_the_oauth_failure_before_falling_back(monkeypatch, caplog):
    """The fallback is silent to the caller, so the only trace is this log
    line — it is what an operator greps for when a tunnel gets a static
    token instead of the expected OAuth one."""
    monkeypatch.setattr(tr, "_oauth_service", _oauth_unavailable)
    _patch_settings(monkeypatch, mcp_token="static-abc")
    with caplog.at_level("INFO", logger=tr.logger.name):
        tr._issue_tunnel_token()
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "[TUNNEL] OAuth token issuance unavailable" in m and "MNEMOS_OAUTH_ISSUER is not configured" in m
        for m in messages
    ), messages


def test_issue_strips_surrounding_whitespace_from_the_static_token(monkeypatch):
    _patch_settings(monkeypatch, mcp_token="  spaced-token \n")
    assert tr._issue_tunnel_token() == ("spaced-token", "static", None)


def test_issue_raises_503_when_oauth_is_absent_and_no_static_token(monkeypatch):
    _patch_settings(monkeypatch, mcp_token="")
    with pytest.raises(HTTPException) as ei:
        tr._issue_tunnel_token()
    assert ei.value.status_code == 503


def test_issue_503_message_names_both_accepted_credentials(monkeypatch):
    _patch_settings(monkeypatch, mcp_token="")
    with pytest.raises(HTTPException) as ei:
        tr._issue_tunnel_token()
    detail = ei.value.detail
    assert "MNEMOS_MCP_TOKEN" in detail
    assert "MNEMOS_OAUTH_ISSUER" in detail
    assert "MNEMOS_MCP_TOKENS alone is not used here" in detail


@pytest.mark.parametrize("empty", ["", "   ", "\n\t "])
def test_issue_treats_whitespace_only_static_token_as_absent(monkeypatch, empty):
    _patch_settings(monkeypatch, mcp_token=empty)
    with pytest.raises(HTTPException) as ei:
        tr._issue_tunnel_token()
    assert ei.value.status_code == 503


def test_issue_never_uses_the_per_user_token_map(monkeypatch):
    """Handing one entry of MNEMOS_MCP_TOKENS out would grant that identity."""
    settings = SimpleNamespace(mcp=SimpleNamespace(token="", tokens="alice:key1,bob:key2", tunnels_enabled=True))
    monkeypatch.setattr(tr, "get_settings", lambda: settings)
    with pytest.raises(HTTPException) as ei:
        tr._issue_tunnel_token()
    assert ei.value.status_code == 503
    assert "key1" not in ei.value.detail
    assert "key2" not in ei.value.detail


def test_issue_prefers_oauth_even_when_a_static_token_exists(monkeypatch):
    class _Service:
        def issue_access_token(self, *, client_id, provider, lifetime):
            return "oauth-wins"

    monkeypatch.setattr(tr, "_oauth_service", lambda: _Service())
    _patch_settings(monkeypatch, mcp_token="static-loses")
    assert tr._issue_tunnel_token()[0] == "oauth-wins"


# ── start_tunnel: normal path ────────────────────────────────────────────


def test_start_returns_every_field_from_the_bridge_info(fake_bridge):
    resp = _run(tr.start_tunnel(_request(target_port=5004), _root_user()))
    assert resp.model_dump() == {
        "url": "https://fake.trycloudflare.com",
        "token": "static-mcp-token",
        "token_source": "static",
        "backend": "cloudflare",
        "target_port": 5004,
        "pid": 4242,
        "started_at": "2026-09-14T00:00:00+00:00",
        "expires_in": None,
    }


def test_start_passes_target_port_and_authtoken_to_the_bridge(fake_bridge):
    _run(tr.start_tunnel(_request(target_port=8080, authtoken="tok-123"), _root_user()))
    assert fake_bridge.start_calls == [{"target_port": 8080, "authtoken": "tok-123"}]


def test_start_passes_a_none_authtoken_through_when_absent(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    assert fake_bridge.start_calls == [{"target_port": 5004, "authtoken": None}]


def test_start_installs_the_bridge_as_the_active_tunnel(fake_bridge):
    assert tr._active_bridge is None
    _run(tr.start_tunnel(_request(), _root_user()))
    assert isinstance(tr._active_bridge, fake_bridge)
    assert tr._active_bridge.status() is not None


def test_start_uses_an_oauth_token_and_reports_its_expiry(monkeypatch, fake_bridge):
    class _Service:
        def issue_access_token(self, *, client_id, provider, lifetime):
            return "oauth-jwt"

    monkeypatch.setattr(tr, "_oauth_service", lambda: _Service())
    resp = _run(tr.start_tunnel(_request(), _root_user()))
    assert resp.token == "oauth-jwt"
    assert resp.token_source == "oauth"
    assert resp.expires_in == 43200


def test_start_dispatches_to_the_requested_backend(monkeypatch):
    chosen = []

    def _get(backend):
        chosen.append(backend)
        return FakeNgrokBridge

    monkeypatch.setattr(tr, "get_bridge_class", _get)
    _run(tr.start_tunnel(_request(backend="ngrok"), _root_user()))
    assert chosen == ["ngrok"]


def test_start_defaults_to_the_cloudflare_bridge_class(monkeypatch):
    chosen = []
    monkeypatch.setattr(tr, "get_bridge_class", lambda b: (chosen.append(b), FakeBridge)[1])
    _run(tr.start_tunnel(_request(), _root_user()))
    assert chosen == ["cloudflare"]


@pytest.mark.parametrize("port", [1, 65535])
def test_start_accepts_both_boundary_ports(fake_bridge, port):
    resp = _run(tr.start_tunnel(_request(target_port=port), _root_user()))
    assert resp.target_port == port


# ── start_tunnel: gate and validation ordering ───────────────────────────


def test_start_403s_when_the_host_has_not_opted_in(monkeypatch, fake_bridge):
    _patch_settings(monkeypatch, tunnels_enabled=False)
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == 403


def test_start_gate_runs_before_any_bridge_is_resolved(monkeypatch):
    """A disabled host must not even look up a backend."""
    _patch_settings(monkeypatch, tunnels_enabled=False)
    resolved = []
    monkeypatch.setattr(tr, "get_bridge_class", lambda b: resolved.append(b) or FakeBridge)
    with pytest.raises(HTTPException):
        _run(tr.start_tunnel(_request(), _root_user()))
    assert resolved == []


def test_start_gate_runs_before_any_token_is_issued(monkeypatch):
    """No live credential may be minted for a request that will be refused."""
    _patch_settings(monkeypatch, tunnels_enabled=False)
    issued = []
    monkeypatch.setattr(tr, "_oauth_service", lambda: issued.append(1) or object())
    with pytest.raises(HTTPException):
        _run(tr.start_tunnel(_request(), _root_user()))
    assert issued == []
    assert tr._active_bridge is None


def test_start_unknown_backend_is_422(monkeypatch):
    """get_bridge_class raising KeyError is turned into a 422, not a 500."""

    def _get(backend):
        raise KeyError(backend)

    monkeypatch.setattr(tr, "get_bridge_class", _get)
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == 422


def test_start_unknown_backend_message_lists_the_valid_names(monkeypatch):
    def _get(backend):
        raise KeyError(backend)

    monkeypatch.setattr(tr, "get_bridge_class", _get)
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    detail = ei.value.detail
    assert "Unknown tunnel backend" in detail
    assert "cloudflare" in detail and "ngrok" in detail


def test_start_unknown_backend_message_quotes_the_requested_value(monkeypatch):
    def _get(backend):
        raise KeyError(backend)

    monkeypatch.setattr(tr, "get_bridge_class", _get)
    req = tr.TunnelStartRequest.model_construct(backend="tailscale", authtoken=None, target_port=5004)
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(req, _root_user()))
    assert "'tailscale'" in ei.value.detail


def test_start_validates_backend_before_issuing_a_token(monkeypatch):
    """A 422 must not leave a minted credential behind."""

    def _get(backend):
        raise KeyError(backend)

    monkeypatch.setattr(tr, "get_bridge_class", _get)
    token_calls = []
    monkeypatch.setattr(tr, "_issue_tunnel_token", lambda: token_calls.append(1) or ("t", "oauth", 1))
    with pytest.raises(HTTPException):
        _run(tr.start_tunnel(_request(), _root_user()))
    assert token_calls == []


def test_start_resolves_the_bridge_class_before_issuing_a_token(monkeypatch, fake_bridge):
    """Ordering pin: backend validation happens first, so a 503 from token
    issuance is reached only after a valid backend was resolved."""
    order = []
    monkeypatch.setattr(tr, "get_bridge_class", lambda b: order.append("resolve") or FakeBridge)

    def _no_token():
        order.append("token")
        raise HTTPException(status_code=503, detail="no credential")

    monkeypatch.setattr(tr, "_issue_tunnel_token", _no_token)
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == 503
    assert order == ["resolve", "token"]


def test_start_resolves_the_token_before_spawning_the_bridge(monkeypatch, fake_bridge):
    """A tunnel opened with no credential would expose the port for nothing."""

    def _no_token():
        raise HTTPException(status_code=503, detail="no credential")

    monkeypatch.setattr(tr, "_issue_tunnel_token", _no_token)
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == 503
    assert fake_bridge.start_calls == []


def test_start_503_on_no_credential_leaves_no_active_tunnel(monkeypatch, fake_bridge):
    _patch_settings(monkeypatch, mcp_token="")
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == 503
    assert tr._active_bridge is None


# ── start_tunnel: bridge failure mapping ─────────────────────────────────


@pytest.mark.parametrize(
    "exc,status",
    [
        (TunnelBinaryMissingError("cloudflared", "install it"), 503),
        (TunnelAuthError("needs an authtoken"), 422),
        (TunnelStartError("exited with code 1"), 502),
        (TunnelError("generic failure"), 502),
    ],
)
def test_start_maps_each_bridge_failure_to_its_status(monkeypatch, fake_bridge, exc, status):
    fake_bridge.raise_on_start = exc
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == status
    assert str(ei.value.detail) == str(exc)


def test_start_surfaces_the_install_hint_for_a_missing_binary(fake_bridge):
    fake_bridge.raise_on_start = TunnelBinaryMissingError(
        "cloudflared", "Install cloudflared: `brew install cloudflared`."
    )
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert "brew install cloudflared" in ei.value.detail


def test_failed_start_leaves_no_active_tunnel(fake_bridge):
    fake_bridge.raise_on_start = TunnelStartError("boom")
    with pytest.raises(HTTPException):
        _run(tr.start_tunnel(_request(), _root_user()))
    assert tr._active_bridge is None


def test_a_refused_second_start_mints_no_credential(monkeypatch):
    """The 409 guard runs before the token is resolved.

    A credential minted for a start that is then refused is a live secret
    nobody asked for; it would also consume a one-shot ngrok/OAuth token
    for a tunnel that never opens.
    """
    running = FakeBridge()
    _run(running.start(target_port=5004))
    tr._active_bridge = running

    minted = []
    monkeypatch.setattr(tr, "_issue_tunnel_token", lambda: minted.append(1) or ("t", "oauth", 1))

    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == 409
    assert minted == []


def test_a_second_start_while_running_is_refused_before_the_bridge_is_entered(monkeypatch):
    """The 409 guard runs first: a live tunnel means the new bridge is
    never constructed, so a failing second bridge never even runs."""
    running = FakeBridge()
    _run(running.start(target_port=5004))
    tr._active_bridge = running

    constructed = []

    class _FailingBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            constructed.append(self)

        async def start(self, *, target_port, authtoken=None):
            raise TunnelStartError("boom")

    monkeypatch.setattr(tr, "get_bridge_class", lambda b: _FailingBridge)
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == 409
    assert constructed == []
    assert tr._active_bridge is running


# ── start_tunnel: the single-tunnel invariant ────────────────────────────


def test_second_start_while_running_is_409(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    assert ei.value.status_code == 409


def test_409_message_names_backend_url_port_and_stop_command(fake_bridge):
    _run(tr.start_tunnel(_request(target_port=5004), _root_user()))
    with pytest.raises(HTTPException) as ei:
        _run(tr.start_tunnel(_request(), _root_user()))
    detail = ei.value.detail
    assert "cloudflare tunnel is already running" in detail
    assert "https://fake.trycloudflare.com" in detail
    assert "port 5004" in detail
    assert "DELETE /admin/tunnels/stop" in detail


def test_409_does_not_spawn_a_second_bridge(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    before = len(fake_bridge.start_calls)
    with pytest.raises(HTTPException):
        _run(tr.start_tunnel(_request(), _root_user()))
    assert len(fake_bridge.start_calls) == before


def test_a_dead_existing_bridge_does_not_block_a_new_start(monkeypatch):
    """status() returning None means the old agent died; a new start is allowed."""
    dead = FakeBridge()
    tr._active_bridge = dead  # never started -> status() is None
    monkeypatch.setattr(tr, "get_bridge_class", lambda b: FakeBridge)
    resp = _run(tr.start_tunnel(_request(), _root_user()))
    assert resp.url == "https://fake.trycloudflare.com"


# ── tunnel_status ────────────────────────────────────────────────────────


def test_status_is_not_running_when_no_tunnel_was_ever_opened():
    resp = _run(tr.tunnel_status(_root_user()))
    assert resp.running is False
    assert resp.backend is None
    assert resp.url is None
    assert resp.pid is None


def test_status_reports_a_live_tunnel(fake_bridge):
    _run(tr.start_tunnel(_request(target_port=5004), _root_user()))
    resp = _run(tr.tunnel_status(_root_user()))
    assert resp.running is True
    assert resp.backend == "cloudflare"
    assert resp.url == "https://fake.trycloudflare.com"
    assert resp.target_port == 5004
    assert resp.pid == 4242
    assert resp.started_at == "2026-09-14T00:00:00+00:00"


def test_status_reports_not_running_when_the_agent_died_on_its_own(fake_bridge):
    """status() re-checks the child, so a dead agent reads as not-running."""
    _run(tr.start_tunnel(_request(), _root_user()))
    tr._active_bridge._info = None  # simulate the agent exiting undetected
    resp = _run(tr.tunnel_status(_root_user()))
    assert resp.running is False


def test_status_does_not_clear_a_dead_bridge_handle(monkeypatch):
    """Characterisation: the handle stays assigned after a dead status read."""
    bridge = FakeBridge()
    tr._active_bridge = bridge
    resp = _run(tr.tunnel_status(_root_user()))
    assert resp.running is False
    assert tr._active_bridge is bridge


def test_status_403s_when_the_host_has_not_opted_in(monkeypatch):
    _patch_settings(monkeypatch, tunnels_enabled=False)
    with pytest.raises(HTTPException) as ei:
        _run(tr.tunnel_status(_root_user()))
    assert ei.value.status_code == 403


def test_status_gate_does_not_touch_the_bridge(monkeypatch):
    _patch_settings(monkeypatch, tunnels_enabled=False)
    bridge = FakeBridge()
    touched = []
    bridge.status = lambda: touched.append(1)
    tr._active_bridge = bridge
    with pytest.raises(HTTPException):
        _run(tr.tunnel_status(_root_user()))
    assert touched == []


def test_status_is_idempotent(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    first = _run(tr.tunnel_status(_root_user()))
    second = _run(tr.tunnel_status(_root_user()))
    assert first.model_dump() == second.model_dump()


# ── stop_tunnel ──────────────────────────────────────────────────────────


def test_stop_with_nothing_running_is_not_an_error():
    """Idempotent: stopping twice must not 404."""
    resp = _run(tr.stop_tunnel(_root_user()))
    assert resp.stopped is False
    assert resp.backend is None


def test_stop_tears_down_a_running_tunnel(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    resp = _run(tr.stop_tunnel(_root_user()))
    assert resp.stopped is True
    assert resp.backend == "cloudflare"
    assert tr._active_bridge is None


def test_stop_calls_the_bridge_stop_once(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    bridge = tr._active_bridge
    _run(tr.stop_tunnel(_root_user()))
    assert bridge.stop_calls == 1


def test_stop_reports_backend_none_when_the_bridge_reports_false():
    """A bridge that says 'was not running' yields backend=None."""

    class _AlreadyDeadBridge(FakeBridge):
        async def stop(self):
            self.stop_calls += 1
            return False

    tr._active_bridge = _AlreadyDeadBridge()
    resp = _run(tr.stop_tunnel(_root_user()))
    assert resp.stopped is False
    assert resp.backend is None


def test_stop_clears_the_handle_even_when_the_bridge_returns_false():
    class _AlreadyDeadBridge(FakeBridge):
        async def stop(self):
            return False

    tr._active_bridge = _AlreadyDeadBridge()
    _run(tr.stop_tunnel(_root_user()))
    assert tr._active_bridge is None


def test_stop_is_idempotent_across_two_calls(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    assert _run(tr.stop_tunnel(_root_user())).stopped is True
    assert _run(tr.stop_tunnel(_root_user())).stopped is False


def test_stop_after_stop_leaves_status_not_running(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    _run(tr.stop_tunnel(_root_user()))
    assert _run(tr.tunnel_status(_root_user())).running is False


def test_stop_403s_when_the_host_has_not_opted_in(monkeypatch, fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    _patch_settings(monkeypatch, tunnels_enabled=False)
    with pytest.raises(HTTPException) as ei:
        _run(tr.stop_tunnel(_root_user()))
    assert ei.value.status_code == 403


def test_stop_gate_runs_before_the_tunnel_is_torn_down(monkeypatch, fake_bridge):
    """A disabled host cannot tear down through the API either."""
    _run(tr.start_tunnel(_request(), _root_user()))
    bridge = tr._active_bridge
    _patch_settings(monkeypatch, tunnels_enabled=False)
    with pytest.raises(HTTPException):
        _run(tr.stop_tunnel(_root_user()))
    assert bridge.stop_calls == 0
    assert tr._active_bridge is bridge


# ── shutdown_active_tunnel ───────────────────────────────────────────────


def test_shutdown_closes_an_open_tunnel(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    bridge = tr._active_bridge
    _run(tr.shutdown_active_tunnel())
    assert tr._active_bridge is None
    assert bridge.stop_calls == 1
    assert bridge.status() is None


def test_shutdown_is_safe_with_no_tunnel():
    tr._reset_active_for_tests()
    assert _run(tr.shutdown_active_tunnel()) is None
    assert tr._active_bridge is None


def test_shutdown_is_idempotent(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    _run(tr.shutdown_active_tunnel())
    _run(tr.shutdown_active_tunnel())  # must not raise
    assert tr._active_bridge is None


def test_shutdown_ignores_a_raising_bridge_stop(caplog):
    """Best-effort teardown: a bridge fault must not break app shutdown."""

    class _RaisingBridge(FakeBridge):
        async def stop(self):
            raise RuntimeError("teardown exploded")

    bridge = _RaisingBridge()
    tr._active_bridge = bridge
    with caplog.at_level("WARNING"):
        _run(tr.shutdown_active_tunnel())  # must not raise
    assert tr._active_bridge is None
    assert any("failed to close" in rec.message or "failed to close" in rec.getMessage() for rec in caplog.records)


def test_shutdown_with_a_bridge_that_reports_not_stopped(caplog):
    """stop() returning False means 'there was nothing to stop'. The handle
    is still cleared; no 'closed on shutdown' line is emitted."""

    class _IdleBridge(FakeBridge):
        async def stop(self):
            self.stop_calls += 1
            return False

    bridge = _IdleBridge()
    tr._active_bridge = bridge
    with caplog.at_level("INFO", logger=tr.logger.name):
        _run(tr.shutdown_active_tunnel())
    assert tr._active_bridge is None
    assert bridge.stop_calls == 1
    assert not any("tunnel closed on shutdown" in rec.getMessage() for rec in caplog.records)


def test_shutdown_logs_when_the_bridge_was_closed(caplog):
    class _LiveBridge(FakeBridge):
        async def stop(self):
            self.stop_calls += 1
            return True

    tr._active_bridge = _LiveBridge()
    with caplog.at_level("INFO", logger=tr.logger.name):
        _run(tr.shutdown_active_tunnel())
    assert any("cloudflare tunnel closed on shutdown" in rec.getMessage() for rec in caplog.records)


def test_shutdown_clears_the_handle_before_awaiting_stop(monkeypatch):
    """The handle is dropped under the lock, before the await, so a slow
    stop cannot leave a half-torn-down tunnel visible to /status."""
    seen = {}

    class _ProbeBridge(FakeBridge):
        async def stop(self):
            seen["active_during_stop"] = tr._active_bridge
            return True

    tr._active_bridge = _ProbeBridge()
    _run(tr.shutdown_active_tunnel())
    assert seen["active_during_stop"] is None


def test_shutdown_does_not_require_the_opt_in_gate(monkeypatch, fake_bridge):
    """Lifespan teardown must work even if the host is now configured off."""
    _run(tr.start_tunnel(_request(), _root_user()))
    bridge = tr._active_bridge
    _patch_settings(monkeypatch, tunnels_enabled=False)
    _run(tr.shutdown_active_tunnel())
    assert tr._active_bridge is None
    assert bridge.stop_calls == 1


# ── _reset_active_for_tests ──────────────────────────────────────────────


def test_reset_clears_the_active_bridge(fake_bridge):
    _run(tr.start_tunnel(_request(), _root_user()))
    assert tr._active_bridge is not None
    tr._reset_active_for_tests()
    assert tr._active_bridge is None


def test_reset_is_idempotent():
    tr._reset_active_for_tests()
    tr._reset_active_for_tests()
    assert tr._active_bridge is None


# ── TunnelBridge agreement with the fake ─────────────────────────────────


def test_fake_bridge_implements_the_real_bridge_interface():
    """Guard against the fake drifting from the ABC it stands in for."""
    assert issubclass(FakeBridge, object)
    for attr in ("start", "status", "stop"):
        assert hasattr(FakeBridge(), attr)
    assert TunnelBridge.__abstractmethods__ == frozenset({"_spawn_spec", "_await_public_url"})
