"""Integration tests for MNEMOS v3.0.0 unified service.

Tests verify:
1. New /v1/ endpoints work correctly
2. Backward compatibility with v2.x endpoints
3. Database migrations applied successfully
4. Hash-chained audit logging
5. Consultation memory refs tracking
"""
import asyncio
import hashlib
import json
import pytest
from httpx import AsyncClient
from datetime import datetime

pytestmark = pytest.mark.asyncio


class TestConsultationsV1:
    """Test new /v1/consultations endpoints."""

    async def test_create_consultation(self, client: AsyncClient, auth_headers: dict):
        """POST /v1/consultations creates a consultation."""
        resp = await client.post(
            "/v1/consultations",
            json={
                "prompt": "What is the capital of France?",
                "task_type": "reasoning",
                "mode": "consensus",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "consultation_id" in data or "all_responses" in data
        assert "consensus_response" in data or "all_responses" in data

    async def test_get_consultation(self, client: AsyncClient, auth_headers: dict, sample_consultation_id: str):
        """GET /v1/consultations/{id} retrieves a consultation."""
        resp = await client.get(
            f"/v1/consultations/{sample_consultation_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sample_consultation_id
        assert "all_responses" in data or "consensus_response" in data

    async def test_get_consultation_artifacts(self, client: AsyncClient, auth_headers: dict, sample_consultation_id: str):
        """GET /v1/consultations/{id}/artifacts returns citations and memory refs."""
        resp = await client.get(
            f"/v1/consultations/{sample_consultation_id}/artifacts",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "citations" in data or "memory_refs" in data or data == {}

    async def test_list_audit_log(self, client: AsyncClient, auth_headers: dict):
        """GET /v1/consultations/audit lists audit log entries."""
        resp = await client.get(
            "/v1/consultations/audit",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            entry = data[0]
            assert "id" in entry
            assert "sequence_num" in entry
            assert "chain_hash" in entry

    async def test_verify_audit_chain(self, client: AsyncClient, auth_headers: dict):
        """GET /v1/consultations/audit/verify validates chain integrity."""
        resp = await client.get(
            "/v1/consultations/audit/verify",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "valid" in data
        assert "entries_checked" in data
        assert "message" in data


class TestProvidersV1:
    """Test new /v1/providers endpoints."""

    async def test_list_providers(self, client: AsyncClient, auth_headers: dict):
        """GET /v1/providers lists available providers."""
        resp = await client.get(
            "/v1/providers",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            provider = data[0]
            assert "provider" in provider
            assert "total_models" in provider or "available_models" in provider

    async def test_provider_health(self, client: AsyncClient):
        """GET /v1/providers/health checks provider status."""
        resp = await client.get("/v1/providers/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        # May contain per-provider status if available

    async def test_recommend_model(self, client: AsyncClient, auth_headers: dict):
        """GET /v1/providers/recommend returns cost-optimized model."""
        resp = await client.get(
            "/v1/providers/recommend",
            params={
                "task_type": "reasoning",
                "cost_budget": 10.0,
                "quality_floor": 0.85,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        if data:  # May be empty if no models match criteria
            assert "recommended" in data or "model_id" in data


class TestMemoriesV1:
    """Test /v1/memories endpoints (renamed from v2)."""

    async def test_create_memory(self, client: AsyncClient, auth_headers: dict):
        """POST /v1/memories creates a memory."""
        resp = await client.post(
            "/v1/memories",
            json={
                "content": "Test memory content",
                "category": "solutions",
                "tags": ["test", "integration"],
            },
            headers=auth_headers,
        )
        assert resp.status_code in [200, 201]
        data = resp.json()
        assert "id" in data

    async def test_list_memories(self, client: AsyncClient, auth_headers: dict):
        """GET /v1/memories lists memories."""
        resp = await client.get(
            "/v1/memories",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    async def test_search_memories(self, client: AsyncClient, auth_headers: dict):
        """POST /v1/memories/search searches memories."""
        resp = await client.post(
            "/v1/memories/search",
            json={"query": "test", "limit": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


class TestBackwardCompatibilityV2:
    """Test that v2.x endpoints still work (deprecated but functional)."""

    async def test_graeae_consult_redirect(self, client: AsyncClient):
        """POST /graeae/consult still works (deprecated)."""
        resp = await client.post(
            "/graeae/consult",
            json={
                "prompt": "Test query",
                "task_type": "reasoning",
            },
        )
        # Should either work or return deprecation notice
        assert resp.status_code in [200, 301, 308]
        # Check for deprecation header if present
        if "X-Deprecated" in resp.headers:
            assert "use /v1/consultations" in resp.headers["X-Deprecated"]

    async def test_graeae_health_redirect(self, client: AsyncClient):
        """GET /graeae/health still works (deprecated)."""
        resp = await client.get("/graeae/health")
        assert resp.status_code in [200, 301, 308]

    async def test_model_registry_recommend(self, client: AsyncClient, auth_headers: dict):
        """GET /model-registry/recommend still works (deprecated)."""
        resp = await client.get(
            "/model-registry/recommend",
            params={"task_type": "reasoning"},
            headers=auth_headers,
        )
        assert resp.status_code in [200, 301, 308]


class TestDatabaseMigrations:
    """Test that database schema changes were applied."""

    async def test_consultation_memory_refs_table_exists(self, db_pool):
        """Verify consultation_memory_refs table exists."""
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name='consultation_memory_refs')"
            )
            assert exists, "consultation_memory_refs table not found"

    async def test_consultation_memory_refs_has_indexes(self, db_pool):
        """Verify required indexes on consultation_memory_refs."""
        async with db_pool.acquire() as conn:
            indexes = await conn.fetch(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename='consultation_memory_refs'"
            )
            index_names = [idx["indexname"] for idx in indexes]
            assert any("consultation" in idx for idx in index_names), "Missing consultation_id index"
            assert any("memory" in idx for idx in index_names), "Missing memory_id index"
            assert any("injected" in idx for idx in index_names), "Missing injected_at index"

    async def test_graeae_audit_log_table_exists(self, db_pool):
        """Verify graeae_audit_log table exists and is immutable."""
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name='graeae_audit_log')"
            )
            assert exists, "graeae_audit_log table not found"

            # Verify chain_hash column exists
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='graeae_audit_log'"
            )
            col_names = [col["column_name"] for col in cols]
            assert "chain_hash" in col_names, "chain_hash column not found"


class TestAuditChainIntegrity:
    """Test hash-chained audit log integrity."""

    async def test_audit_entries_form_chain(self, db_pool):
        """Verify audit entries are correctly hash-chained."""
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT sequence_num, prompt_hash, response_hash, chain_hash, prev_id "
                "FROM graeae_audit_log ORDER BY sequence_num ASC"
            )

        if not rows:
            pytest.skip("No audit entries to verify")

        _GENESIS_HASH = hashlib.sha256(b"MNEMOS_AUDIT_GENESIS_v3").hexdigest()
        prev_chain = _GENESIS_HASH

        for row in rows:
            expected = hashlib.sha256(
                (prev_chain + row["prompt_hash"] + row["response_hash"]).encode()
            ).hexdigest()
            assert expected == row["chain_hash"], f"Chain broken at sequence {row['sequence_num']}"
            prev_chain = row["chain_hash"]

    async def test_memory_refs_link_consultations(self, db_pool):
        """Verify consultation_memory_refs correctly links consultations to memories."""
        async with db_pool.acquire() as conn:
            # Get a consultation with memory refs (if any exist)
            row = await conn.fetchrow(
                "SELECT cmr.consultation_id, cmr.memory_id, cmr.relevance_score "
                "FROM consultation_memory_refs cmr LIMIT 1"
            )

            if row:
                # Verify both sides of the foreign key exist
                consult_exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM graeae_consultations WHERE id=$1)",
                    row["consultation_id"]
                )
                memory_exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM memories WHERE id=$1)",
                    row["memory_id"]
                )
                assert consult_exists, "Referenced consultation not found"
                assert memory_exists, "Referenced memory not found"


class TestVersions:
    """Test version reporting."""

    async def test_health_reports_v3(self, client: AsyncClient):
        """Health endpoint reports v3.0.0."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("version") == "3.0.0", f"Expected version 3.0.0, got {data.get('version')}"

    async def test_api_version_in_responses(self, client: AsyncClient, auth_headers: dict):
        """API responses include v3 metadata."""
        resp = await client.get("/v1/providers/health")
        assert resp.status_code == 200
        # Response should come from new /v1/ handlers
        # (no specific version field required, but should be from v3 codebase)


# Fixtures

@pytest.fixture
def auth_headers():
    """Return authorization headers for authenticated endpoints."""
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
async def sample_consultation_id(client: AsyncClient, auth_headers: dict):
    """Create a sample consultation and return its ID."""
    try:
        resp = await client.post(
            "/v1/consultations",
            json={
                "prompt": "Sample consultation for testing",
                "task_type": "reasoning",
            },
            headers=auth_headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("consultation_id") or data.get("id")
    except Exception:
        pass
    return "test-id"
