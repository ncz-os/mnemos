"""Pytest configuration and shared fixtures for MNEMOS tests."""
import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def client():
    """Create an AsyncClient for testing API endpoints.

    Connects to PROTEUS MNEMOS v3.0.0 at 192.168.207.25:5002
    Set API_URL environment variable to change target.
    """
    import os
    api_url = os.getenv("API_URL", "http://192.168.207.25:5002")
    async with AsyncClient(base_url=api_url, timeout=10.0) as ac:
        yield ac


@pytest_asyncio.fixture
async def db_pool():
    """Mock database pool for testing database operations.

    Returns a mock asyncpg pool with common operations.
    """
    pool = AsyncMock()

    # Mock acquire context manager
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None

    # Mock common query methods
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)

    # Mock transaction context manager
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)

    return pool


@pytest.fixture
def auth_headers():
    """Return authorization headers with test token."""
    return {
        "Authorization": "Bearer test-token-for-testing"
    }


@pytest_asyncio.fixture
async def sample_consultation_id(client: AsyncClient, auth_headers: dict):
    """Create a sample consultation and return its ID for testing."""
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
            return data.get("consultation_id") or data.get("id") or "test-id"
    except Exception:
        pass
    return "test-id"


@pytest.fixture
def mock_graeae_engine():
    """Mock GRAEAE engine for testing."""
    engine = MagicMock()
    engine.consult = AsyncMock(return_value={
        "all_responses": {
            "openai": {
                "response_text": "Test response",
                "final_score": 0.95,
                "latency_ms": 1200,
            }
        },
        "consensus_response": "Test response",
    })
    engine.provider_status = MagicMock(return_value={
        "providers": {
            "openai": {"status": "healthy"},
            "anthropic": {"status": "healthy"},
        }
    })
    return engine


@pytest.fixture
def mock_model_registry():
    """Mock model registry for testing."""
    registry = MagicMock()
    registry.recommend = AsyncMock(return_value={
        "provider": "openai",
        "model_id": "gpt-4",
        "cost_per_mtok": 0.03,
    })
    registry.list_providers = AsyncMock(return_value=[
        {"provider": "openai", "total_models": 5},
        {"provider": "anthropic", "total_models": 3},
    ])
    return registry
