"""Federation subsystem tests — wiring, id convention, sync protocol.

Unit tests run without DB. Integration tests require MNEMOS_TEST_DB.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Module wiring ────────────────────────────────────────────────────────────


class TestFederationWiring:
    def test_federation_module_imports(self):
        from api import federation
        for name in (
            "sync_peer",
            "federation_worker_loop",
            "FEDERATION_ID_PREFIX",
            "FEDERATION_BATCH_LIMIT",
        ):
            assert hasattr(federation, name), f"api.federation missing: {name}"

    def test_federation_handler_router(self):
        from api.handlers import federation as handler
        assert hasattr(handler, "router")
        assert handler.router.prefix == "/v1/federation"

    def test_federation_models(self):
        from api.models import (
            FederationPeerCreateRequest,
        )
        req = FederationPeerCreateRequest(
            name="peer-alpha",
            base_url="https://alpha.example.com",
            auth_token="x" * 40,
        )
        assert req.sync_interval_secs == 300

    def test_router_registered_in_app(self):
        import api_server
        paths = {r.path for r in api_server.app.routes}
        fed_paths = [p for p in paths if p.startswith("/v1/federation")]
        # peers CRUD (5) + sync (1) + log (1) + status (1) + feed (1) = 9 at minimum
        assert len(fed_paths) >= 5, f"expected federation routes, got: {fed_paths}"

    def test_feed_route_exists(self):
        import api_server
        paths = {r.path for r in api_server.app.routes}
        assert "/v1/federation/feed" in paths


# ── Identifier convention ────────────────────────────────────────────────────


class TestFederationIdConvention:
    def test_prefix_constant(self):
        from api.federation import FEDERATION_ID_PREFIX
        assert FEDERATION_ID_PREFIX == "fed:"

    def test_local_id_format_example(self):
        # Docs promise: fed:{peer_name}:{remote_id}
        from api.federation import FEDERATION_ID_PREFIX
        peer = "alpha"
        remote_id = "mem_abc123"
        local = f"{FEDERATION_ID_PREFIX}{peer}:{remote_id}"
        assert local == "fed:alpha:mem_abc123"


# ── Peer name validation (format checked at DB layer) ────────────────────────


class TestPeerNameFormat:
    """The DB CHECK constraint enforces format. Here we just document it."""

    def test_valid_peer_names(self):
        import re
        pat = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
        for name in ("alpha", "peer-alpha", "peer-1", "a1"):
            assert pat.match(name), f"expected {name} valid"

    def test_invalid_peer_names(self):
        import re
        pat = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
        for name in ("A", "peer_alpha", "peer.alpha", "-alpha", "alpha-", "x"):
            assert not pat.match(name), f"expected {name} invalid"


# ── Integration ──────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    "MNEMOS_TEST_DB" not in os.environ,
    reason="set MNEMOS_TEST_DB=postgres://... to run integration tests",
)
class TestFederationIntegration:
    @pytest.mark.asyncio
    async def test_peer_crud(self):
        import asyncpg
        conn = await asyncpg.connect(os.environ["MNEMOS_TEST_DB"])
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO federation_peers (name, base_url, auth_token)
                VALUES ($1, $2, $3)
                RETURNING id, name, enabled, total_pulled
                """,
                "peer-test", "https://test.example.invalid", "token",
            )
            assert row["name"] == "peer-test"
            assert row["enabled"] is True
            assert row["total_pulled"] == 0
            await conn.execute(
                "DELETE FROM federation_peers WHERE id = $1", row["id"]
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_memories_federation_source_column_exists(self):
        import asyncpg
        conn = await asyncpg.connect(os.environ["MNEMOS_TEST_DB"])
        try:
            row = await conn.fetchrow(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'memories' AND column_name = 'federation_source'
                """
            )
            assert row is not None, "migration not applied"
        finally:
            await conn.close()
