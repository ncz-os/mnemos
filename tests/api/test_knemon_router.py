from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.knemon_router import router as knemon_router
from tests.domain.test_knemon_router import _SqliteKnemonBackend


def _root() -> UserContext:
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


@pytest.mark.asyncio
async def test_knemon_route_endpoint_requires_root_and_returns_decision(monkeypatch):
    monkeypatch.setattr(lifecycle, "_persistence_backend", _SqliteKnemonBackend(92))

    app = FastAPI()
    app.include_router(knemon_router)
    app.dependency_overrides[get_current_user] = _root

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/knemon/route",
            params={
                "task_kind": "code-fix",
                "priority": 5,
                "caller_session_id": "s-api",
                "est_tokens_in": 10000,
                "est_tokens_out": 2000,
                "exclude_providers": "xai",
                "require_capability": "code",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "nvidia"
    assert body["model_id"] == "ngc-free"
    assert body["auth_method"] == "free"
    assert body["fallback_chain"]
