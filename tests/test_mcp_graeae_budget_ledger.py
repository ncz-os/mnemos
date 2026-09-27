"""MCP GRAEAE must see the KNEMON ledger when a budget cap is configured.

Measured 2026-09-27 on PYTHIA (mnemos-api 7.0.1): every graeae_consult over
MCP, in every mode, returned ProviderBudgetExceeded "budget ledger
unavailable; failing closed under configured cap" while the same request over
/v1/consultations succeeded. The MCP process never registered a lifecycle
persistence backend, which the budget gate reads.
"""

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette

from tests.oauth_backend_helpers import oauth_database as oauth_database
from tests.test_mcp_oauth_integration import mcp_http_app as mcp_http_app


class _Backend:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _settings(*, weekly=0.0, provider_caps=None):
    return SimpleNamespace(
        oauth=SimpleNamespace(database_url="", issuer=""),
        knemon=SimpleNamespace(
            weekly_budget_cap_usd=weekly,
            parsed_provider_budget_caps_usd=lambda: dict(provider_caps or {}),
        ),
    )


@pytest.mark.asyncio
async def test_a_configured_cap_gets_the_ledger_for_the_lifespan(mcp_http_app, monkeypatch):
    from mnemos.core import lifecycle

    mcp_http = mcp_http_app.http

    backend = _Backend()

    async def factory(_settings):
        return "test", backend

    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", factory)
    monkeypatch.setattr(mcp_http, "get_settings", lambda: _settings(weekly=200.0))
    async with mcp_http._mcp_http_lifespan(Starlette()):
        assert lifecycle._persistence_backend is backend
    assert lifecycle._persistence_backend is None
    assert backend.closed, "the backend this lifespan opened must be closed"


@pytest.mark.asyncio
async def test_a_provider_cap_alone_also_needs_the_ledger(mcp_http_app, monkeypatch):
    from mnemos.core import lifecycle

    mcp_http = mcp_http_app.http

    backend = _Backend()

    async def factory(_settings):
        return "test", backend

    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", factory)
    monkeypatch.setattr(mcp_http, "get_settings", lambda: _settings(provider_caps={"claude": 50.0}))
    async with mcp_http._mcp_http_lifespan(Starlette()):
        assert lifecycle._persistence_backend is backend


@pytest.mark.asyncio
async def test_no_cap_opens_nothing(mcp_http_app, monkeypatch):
    from mnemos.core import lifecycle

    mcp_http = mcp_http_app.http

    async def factory(_settings):
        pytest.fail("no cap means the budget gate needs no ledger")

    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", factory)
    monkeypatch.setattr(mcp_http, "get_settings", lambda: _settings())
    async with mcp_http._mcp_http_lifespan(Starlette()):
        assert lifecycle._persistence_backend is None


@pytest.mark.asyncio
async def test_an_existing_lifecycle_backend_is_left_alone(mcp_http_app, monkeypatch):
    from mnemos.core import lifecycle

    mcp_http = mcp_http_app.http

    existing = _Backend()

    async def factory(_settings):
        pytest.fail("an existing lifecycle backend must be reused")

    monkeypatch.setattr(lifecycle, "_persistence_backend", existing)
    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", factory)
    monkeypatch.setattr(mcp_http, "get_settings", lambda: _settings(weekly=200.0))
    async with mcp_http._mcp_http_lifespan(Starlette()):
        assert lifecycle._persistence_backend is existing
    assert lifecycle._persistence_backend is existing
    assert not existing.closed


@pytest.mark.asyncio
async def test_a_ledger_that_cannot_open_does_not_abort_startup(mcp_http_app, monkeypatch):
    from mnemos.core import lifecycle

    mcp_http = mcp_http_app.http

    async def factory(_settings):
        raise RuntimeError("db down")

    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", factory)
    monkeypatch.setattr(mcp_http, "get_settings", lambda: _settings(weekly=200.0))
    async with mcp_http._mcp_http_lifespan(Starlette()):
        assert lifecycle._persistence_backend is None
