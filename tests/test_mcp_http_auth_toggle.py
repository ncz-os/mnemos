"""MNEMOS_MCP_AUTH_ENABLED: environment toggle for MCP HTTP/SSE bearer auth.

Rule under test (one rule for REST and MCP):

* ``MNEMOS_MCP_AUTH_ENABLED`` set explicitly -> that value wins.
* unset -> MCP auth follows the resolved REST auth setting
  (``MNEMOS_AUTH_ENABLED`` if set, else the profile default: ``server`` on,
  ``edge``/``personal``/``dev`` off).

Disabled: the server starts with no token configured, every request is
served as the legacy single-token principal, and any Authorization header is
ignored. Enabled: today's behaviour, including the FATAL with no token.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys

import pytest
from httpx import ASGITransport, AsyncClient

_AUTH_ENV = (
    "MNEMOS_MCP_AUTH_ENABLED",
    "MNEMOS_AUTH_ENABLED",
    "MNEMOS_PROFILE",
    "MNEMOS_PROFILE_OVERRIDE",
    "MNEMOS_MCP_TOKEN",
    "MNEMOS_MCP_TOKENS",
    "MNEMOS_OAUTH_ISSUER",
    "MNEMOS_OAUTH_ADMIN_PASSPHRASE",
    "MNEMOS_API_KEY",
)

DISABLED_EXPLICIT = "MCP HTTP/SSE auth DISABLED by MNEMOS_MCP_AUTH_ENABLED=false (trusted network only)"
DISABLED_PREFIX = "MCP HTTP/SSE auth DISABLED by "


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch):
    from mnemos.core import config

    for name in _AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    yield
    sys.modules.pop("mnemos.mcp.http", None)
    for name in _AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    config._reset_settings_for_tests()


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str):
    from mnemos.core import config

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config._reset_settings_for_tests()
    return config.get_settings()


def _fresh_http(monkeypatch: pytest.MonkeyPatch, **env: str):
    _settings(monkeypatch, **env)
    sys.modules.pop("mnemos.mcp.http", None)
    return importlib.import_module("mnemos.mcp.http")


def _client(http) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=http.starlette_app), base_url="http://testserver")


_MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _jsonrpc_payloads(text: str) -> list[dict]:
    """Return JSON-RPC bodies from either a JSON or an SSE response body."""
    text = text.strip()
    if text.startswith("{"):
        return [json.loads(text)]
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data = line[len("data:") :].strip()
            if data:
                out.append(json.loads(data))
    return out


async def _initialize_and_list_tools(client: AsyncClient, extra_headers: dict[str, str]):
    headers = {**_MCP_HEADERS, **extra_headers}
    init = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "auth-toggle-test", "version": "1.0"},
            },
        },
    )
    if init.status_code != 200:
        return init, None
    session_id = init.headers["mcp-session-id"]
    session_headers = {**headers, "mcp-session-id": session_id}
    await client.post(
        "/mcp",
        headers=session_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    tools = await client.post(
        "/mcp",
        headers=session_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    await client.delete("/mcp", headers=session_headers)
    return init, tools


# ─── Default rule (settings level) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "expected"),
    [("server", True), ("edge", False), ("personal", False), ("dev", False)],
)
def test_mcp_auth_default_follows_profile(monkeypatch, profile, expected):
    settings = _settings(monkeypatch, MNEMOS_PROFILE=profile)
    assert settings.auth.enabled is expected
    assert settings.mcp.auth_enabled is expected


def test_mcp_auth_default_is_off_with_no_profile_set(monkeypatch):
    settings = _settings(monkeypatch)
    assert settings.profile == "edge"
    assert settings.mcp.auth_enabled is False


@pytest.mark.parametrize(("rest", "expected"), [("true", True), ("false", False)])
def test_mcp_auth_default_follows_explicit_rest_auth(monkeypatch, rest, expected):
    edge = _settings(monkeypatch, MNEMOS_PROFILE="edge", MNEMOS_AUTH_ENABLED=rest)
    assert edge.mcp.auth_enabled is expected
    server = _settings(monkeypatch, MNEMOS_PROFILE="server", MNEMOS_AUTH_ENABLED=rest)
    assert server.mcp.auth_enabled is expected


@pytest.mark.parametrize("profile", ["server", "edge", "personal", "dev"])
@pytest.mark.parametrize(("flag", "expected"), [("true", True), ("false", False), ("1", True), ("0", False)])
def test_explicit_mcp_toggle_overrides_profile_both_ways(monkeypatch, profile, flag, expected):
    settings = _settings(monkeypatch, MNEMOS_PROFILE=profile, MNEMOS_MCP_AUTH_ENABLED=flag)
    assert settings.mcp.auth_enabled is expected
    assert "auth_enabled" in settings.explicit_fields("mcp")


def test_explicit_mcp_toggle_does_not_change_rest_auth(monkeypatch):
    settings = _settings(monkeypatch, MNEMOS_PROFILE="server", MNEMOS_MCP_AUTH_ENABLED="false")
    assert settings.auth.enabled is True
    assert settings.mcp.auth_enabled is False


def test_invalid_mcp_toggle_value_fails_closed(monkeypatch):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="maybe")


# ─── Disabled: startup ────────────────────────────────────────────────────


def _auth_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.getMessage().startswith(DISABLED_PREFIX)
    ]


def test_disabled_explicit_starts_without_token_and_warns_once(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    http = _fresh_http(monkeypatch, MNEMOS_PROFILE="server", MNEMOS_MCP_AUTH_ENABLED="false")
    assert http.MCP_AUTH_ENABLED is False
    assert http.TOKEN_PRINCIPALS == {}
    assert _auth_warnings(caplog) == [DISABLED_EXPLICIT]


@pytest.mark.parametrize("profile", ["edge", "personal", "dev"])
def test_disabled_by_profile_default_starts_without_token_and_warns_once(monkeypatch, caplog, profile):
    caplog.set_level(logging.INFO)
    http = _fresh_http(monkeypatch, MNEMOS_PROFILE=profile)
    assert http.MCP_AUTH_ENABLED is False
    assert http.TOKEN_PRINCIPALS == {}
    warnings = _auth_warnings(caplog)
    assert len(warnings) == 1
    assert "trusted network only" in warnings[0]
    assert "MNEMOS_MCP_AUTH_ENABLED=true" in warnings[0]


def test_disabled_ignores_configured_tokens_and_never_logs_them(monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    secret = "s3cret-token-value-should-never-be-logged"
    # Malformed on purpose: with auth disabled the map is not parsed, so it
    # cannot FATAL, and nothing from it reaches the logs.
    http = _fresh_http(
        monkeypatch,
        MNEMOS_MCP_AUTH_ENABLED="false",
        MNEMOS_MCP_TOKEN=secret,
        MNEMOS_MCP_TOKENS=f"broken-entry-{secret}",
    )
    assert http.TOKEN_PRINCIPALS == {}
    captured = capsys.readouterr()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in logged
    assert secret not in captured.err
    assert secret not in captured.out
    assert _auth_warnings(caplog) == [DISABLED_EXPLICIT]


# ─── Disabled: requests ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer bogus"},
        {"authorization": "Bearer mnemos-old-client-token-0123456789abcdef"},
        {"authorization": "Basic Zm9vOmJhcg=="},
    ],
    ids=["no-header", "bogus-bearer", "valid-looking-bearer", "non-bearer-scheme"],
)
async def test_disabled_mcp_initialize_and_tools_list_need_no_token(monkeypatch, headers):
    http = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="false")
    async with _client(http) as client:
        init, tools = await _initialize_and_list_tools(client, headers)
    assert init.status_code == 200, init.text
    assert _jsonrpc_payloads(init.text)[0]["result"]["serverInfo"]["name"] == "mnemos"
    assert tools is not None and tools.status_code == 200, tools.text
    names = {tool["name"] for tool in _jsonrpc_payloads(tools.text)[0]["result"]["tools"]}
    assert names == set(http.HTTP_TOOL_REGISTRY)


async def _dispatch(http, path: str, method: str, headers: list[tuple[bytes, bytes]]):
    """Run BearerAuthMiddleware for one request; return (status, state seen by the route)."""
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse

    seen: dict = {}

    async def _route(request):
        seen["principal"] = getattr(request.state, "mnemos_mcp_principal", None)
        seen["principal_id"] = getattr(request.state, "mnemos_mcp_principal_id", None)
        return PlainTextResponse("reached")

    scope = {"type": "http", "method": method, "path": path, "headers": headers, "query_string": b""}
    response = await http.BearerAuthMiddleware(app=None).dispatch(Request(scope), _route)
    return response.status_code, seen


_HEADER_VARIANTS = [
    [],
    [(b"authorization", b"Bearer bogus")],
    [(b"authorization", b"Bearer mnemos-old-client-token-0123456789abcdef")],
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "method"), [("/sse", "GET"), ("/messages/", "POST"), ("/mcp", "POST")])
async def test_disabled_middleware_passes_sse_and_messages(monkeypatch, path, method):
    # /sse is a long-lived stream the in-process ASGI client cannot finish,
    # so the auth decision is checked at the middleware boundary.
    http = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="false")
    expected = http._trusted_network_principal()
    for headers in _HEADER_VARIANTS:
        status, seen = await _dispatch(http, path, method, headers)
        assert status == 200
        assert seen["principal"] == expected
        assert seen["principal_id"] == http._principal_id(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "method"), [("/sse", "GET"), ("/messages/", "POST"), ("/mcp", "POST")])
async def test_enabled_middleware_still_blocks_sse_and_messages(monkeypatch, path, method):
    http = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="true", MNEMOS_MCP_TOKEN="right-token")
    for headers in _HEADER_VARIANTS:
        status, seen = await _dispatch(http, path, method, headers)
        assert status == 401
        assert seen == {}
    status, seen = await _dispatch(http, path, method, [(b"authorization", b"Bearer right-token")])
    assert status == 200
    assert seen["principal"] is not None


@pytest.mark.asyncio
async def test_disabled_messages_endpoint_reaches_handler(monkeypatch):
    http = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="false")
    async with _client(http) as client:
        message = await client.post(
            "/messages/?session_id=0123456789abcdef0123456789abcdef",
            headers={"authorization": "Bearer bogus"},
            json={},
        )
    # Reached the handler (unknown session), not the 401 auth gate.
    assert message.status_code == 404


@pytest.mark.asyncio
async def test_disabled_public_routes_unchanged(monkeypatch):
    http = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="false")
    async with _client(http) as client:
        health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.text == "ok"


# ─── Identity parity with legacy single-token mode ────────────────────────


@pytest.mark.parametrize("api_key", ["", "backend-api-key"])
def test_disabled_identity_matches_legacy_single_token(monkeypatch, api_key):
    env = {"MNEMOS_API_KEY": api_key} if api_key else {}
    legacy = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="true", MNEMOS_MCP_TOKEN="legacy-token", **env)
    (legacy_principal,) = legacy.TOKEN_PRINCIPALS.values()
    legacy_id = legacy._principal_id(legacy_principal)

    disabled = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="false", **env)
    principal = disabled._trusted_network_principal()
    assert principal == disabled.MCPClientPrincipal(user_id=legacy_principal.user_id, api_key=legacy_principal.api_key)
    assert principal.user_id is None
    assert principal.api_key == (api_key or None)
    assert disabled._principal_id(principal) == legacy_id


@pytest.mark.asyncio
async def test_disabled_resolved_context_matches_legacy(monkeypatch):
    from types import SimpleNamespace

    legacy = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="true", MNEMOS_MCP_TOKEN="legacy-token")
    (legacy_principal,) = legacy.TOKEN_PRINCIPALS.values()
    legacy_ctx = await legacy._resolve_mcp_user_context(
        SimpleNamespace(state=SimpleNamespace(mnemos_mcp_principal=legacy_principal, mnemos_mcp_principal_id=None))
    )

    disabled = _fresh_http(monkeypatch, MNEMOS_MCP_AUTH_ENABLED="false")
    seen = {}

    async def _capture(request):
        seen["principal"] = request.state.mnemos_mcp_principal
        seen["principal_id"] = request.state.mnemos_mcp_principal_id
        seen["context"] = await disabled._resolve_mcp_user_context(request)
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("ok")

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp/events/stream",
        "headers": [(b"authorization", b"Bearer whatever")],
        "query_string": b"",
    }
    await disabled.BearerAuthMiddleware(app=None).dispatch(Request(scope), _capture)
    # The two module imports define distinct dataclass types; compare fields.
    assert (seen["principal"].user_id, seen["principal"].api_key) == (
        legacy_principal.user_id,
        legacy_principal.api_key,
    )
    assert seen["principal_id"] == legacy._principal_id(legacy_principal)
    assert seen["context"] == disabled.MCPUserContext(
        user_id=legacy_ctx.user_id, role=legacy_ctx.role, namespace=legacy_ctx.namespace
    )
    assert (seen["context"].user_id, seen["context"].role, seen["context"].namespace) == (None, "user", "default")


# ─── Enabled: behaviour unchanged ─────────────────────────────────────────


@pytest.mark.parametrize(
    "env",
    [
        {"MNEMOS_MCP_AUTH_ENABLED": "true"},
        {"MNEMOS_PROFILE": "server"},
        {"MNEMOS_PROFILE": "edge", "MNEMOS_AUTH_ENABLED": "true"},
    ],
    ids=["explicit-true", "server-profile-default", "rest-auth-on"],
)
def test_enabled_without_token_is_still_fatal(monkeypatch, capsys, env):
    with pytest.raises(SystemExit) as excinfo:
        _fresh_http(monkeypatch, **env)
    assert excinfo.value.code == 2
    assert "FATAL: configure MNEMOS_MCP_TOKEN/MNEMOS_MCP_TOKENS" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [{"MNEMOS_MCP_AUTH_ENABLED": "true"}, {"MNEMOS_PROFILE": "server"}],
    ids=["explicit-true", "server-profile-default"],
)
async def test_enabled_requires_the_right_token(monkeypatch, caplog, env):
    caplog.set_level(logging.INFO)
    http = _fresh_http(monkeypatch, MNEMOS_MCP_TOKEN="right-token", **env)
    assert http.MCP_AUTH_ENABLED is True
    assert _auth_warnings(caplog) == []
    async with _client(http) as client:
        missing, _ = await _initialize_and_list_tools(client, {})
        bogus, _ = await _initialize_and_list_tools(client, {"authorization": "Bearer bogus"})
        sse_missing = await client.get("/sse")
        good, tools = await _initialize_and_list_tools(client, {"authorization": "Bearer right-token"})
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == 'Bearer realm="mnemos-mcp"'
    assert bogus.status_code == 401
    assert sse_missing.status_code == 401
    assert good.status_code == 200, good.text
    assert tools is not None and tools.status_code == 200
