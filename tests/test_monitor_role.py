from __future__ import annotations

import ast
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from mnemos.api import dependencies
from mnemos.api.main import app
from mnemos.api.routes import federation, health
from mnemos.core.auth_context import UserContext
from mnemos.core.roles import KNOWN_ROLES, MCP_ALLOWED_ROLES, OPERATOR_ROLES
from mnemos.domain.models import UserCreateRequest


def _uses_auth(dependant) -> bool:
    if dependant.call is dependencies.get_current_user:
        return True
    return any(_uses_auth(child) for child in dependant.dependencies)


def _flatten_routes(routes):
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from _flatten_routes(original_router.routes)
        else:
            yield route


def test_monitor_role_is_accepted_for_user_provisioning():
    assert UserCreateRequest(id="dash", display_name="Dashboard", role="monitor").role == "monitor"


def test_role_vocabularies_share_one_definition():
    from mnemos.mcp import tools as mcp_tools

    assert dependencies.KNOWN_ROLES is KNOWN_ROLES
    assert MCP_ALLOWED_ROLES == KNOWN_ROLES - {"monitor"}
    assert OPERATOR_ROLES == {"root", "operator"}
    assert mcp_tools.MCP_ALLOWED_ROLES is MCP_ALLOWED_ROLES
    source = Path("mnemos/mcp/http.py").read_text()
    module = ast.parse(source)
    imports_roles = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "mnemos.core.roles"
        and any(alias.name == "OPERATOR_ROLES" for alias in node.names)
        for node in ast.walk(module)
    )
    assert imports_roles
    assert "return context.role in OPERATOR_ROLES" in source


def test_legacy_operator_role_is_accepted_by_http_api(monkeypatch):
    operator = UserContext(
        user_id="legacy", group_ids=[], role="operator", namespace="default", authenticated=True
    )
    monkeypatch.setattr(dependencies, "_auth_enabled", False)
    monkeypatch.setattr(dependencies, "PERSONAL_SINGLETON", operator)
    legacy_app = FastAPI()

    @legacy_app.get("/legacy-role")
    async def legacy_role(user: UserContext = Depends(dependencies.get_current_user)):
        return {"role": user.role}

    response = TestClient(legacy_app).get("/legacy-role")
    assert response.status_code == 200
    assert response.json() == {"role": "operator"}


@pytest.mark.asyncio
async def test_monitor_role_is_denied_mcp_tool_access():
    from mnemos.mcp.tools import execute_tool

    monitor = UserContext(
        user_id="dash", group_ids=[], role="monitor", namespace="default", authenticated=True
    )
    result = await execute_tool("get_stats", {}, user=monitor)
    assert result == {"success": False, "error": "Role not permitted"}


def test_monitor_reads_both_approved_status_routes(monkeypatch):
    monitor = UserContext(
        user_id="dash", group_ids=[], role="monitor", namespace="default", authenticated=True
    )
    monkeypatch.setattr(dependencies, "_auth_enabled", False)
    monkeypatch.setattr(dependencies, "PERSONAL_SINGLETON", monitor)

    class Cache:
        async def get(self, key):
            return '{"total_memories":0,"total_compressions":0,"average_compression_ratio":0.57,"average_quality_rating":75,"memories_by_category":{},"unreviewed_compressions":0,"timestamp":"now"}'

    @asynccontextmanager
    async def transaction():
        yield object()

    backend = SimpleNamespace(
        transactional=transaction,
        federation=SimpleNamespace(
            list_peers=AsyncMock(return_value=[]),
            count_federated_rows_without_embedding_per_peer=AsyncMock(return_value={}),
        ),
    )
    monkeypatch.setattr(health._lc, "_cache", Cache())
    monkeypatch.setattr(federation, "require_federation_backend", lambda: backend)
    status_app = FastAPI()
    status_app.include_router(health.router)
    status_app.include_router(federation.router)
    client = TestClient(status_app)

    assert client.get("/stats").status_code == 200
    assert client.get("/v1/federation/status").status_code == 200


@pytest.mark.parametrize("role", ["monitor", "unexpected"])
def test_nonprivileged_role_is_denied_on_authenticated_routes(monkeypatch, role):
    monitor = UserContext(
        user_id="dash", group_ids=[], role=role, namespace="default", authenticated=True
    )
    monkeypatch.setattr(dependencies, "_auth_enabled", False)
    monkeypatch.setattr(dependencies, "PERSONAL_SINGLETON", monitor)

    routes = []
    for route in _flatten_routes(app.routes):
        methods = getattr(route, "methods", set())
        dependant = getattr(route, "dependant", None)
        if not methods or dependant is None or not _uses_auth(dependant):
            continue
        for method in methods - {"HEAD", "OPTIONS"}:
            if role != "monitor" or (method, route.path) not in dependencies._MONITOR_ALLOWED_ROUTES:
                routes.append((method, route))

    assert len(routes) >= 25
    client = TestClient(app)
    for method, route in routes:
        path = re.sub(r"\{[^}:]+(?::[^}]+)?\}", "monitor-test", route.path)
        response = client.request(method, path, json={} if method in {"POST", "PUT", "PATCH"} else None)
        assert response.status_code == 403, f"{method} {route.path}: {response.status_code}"


def test_unknown_role_is_denied_by_shared_auth_boundary():
    from fastapi import HTTPException
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/stats", "headers": []})
    unknown = UserContext(
        user_id="odd", group_ids=[], role="unexpected", namespace="default", authenticated=True
    )
    try:
        dependencies._enforce_role_route_boundary(request, unknown)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("unknown roles must be denied")
