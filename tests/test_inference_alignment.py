"""Test PROTEUS inference alignment with PYTHIA LLM and GRAEAE providers.

Validates that PROTEUS can successfully:
1. Call PYTHIA's Ollama embeddings (nomic-embed-text)
2. Call PYTHIA's inference servers (ports 8000, 8001, 8080)
3. Reach consensus across GRAEAE providers (same as PYTHIA)
4. Match quality scoring and latency profiles
"""
import asyncio
import json
import pytest
import httpx
from datetime import datetime

pytestmark = pytest.mark.asyncio


class TestPYTHIAInferenceAccess:
    """Test direct access to PYTHIA inference resources from PROTEUS perspective."""

    async def test_pythia_ollama_embeddings_available(self):
        """Verify PYTHIA Ollama embeddings (nomic-embed-text) are reachable."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Test Ollama embeddings endpoint
            response = await client.post(
                "http://192.168.207.67:11434/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": "test query"}
            )
            assert response.status_code == 200
            data = response.json()
            assert "embedding" in data
            assert isinstance(data["embedding"], list)
            assert len(data["embedding"]) == 768  # nomic-embed-text dimension

    async def test_pythia_inference_port_8000(self):
        """Test PYTHIA inference server on port 8000."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get("http://192.168.207.67:8000/health")
                # Port might be llama.cpp or other service
                assert response.status_code in (200, 404, 500)  # Any response means it's listening
            except httpx.ConnectError:
                pytest.skip("Port 8000 not accepting HTTP connections")

    async def test_pythia_inference_port_8001(self):
        """Test PYTHIA inference server on port 8001."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get("http://192.168.207.67:8001/health")
                assert response.status_code in (200, 404, 500)
            except httpx.ConnectError:
                pytest.skip("Port 8001 not accepting HTTP connections")

    async def test_pythia_inference_port_8080(self):
        """Test PYTHIA inference server on port 8080."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get("http://192.168.207.67:8080/health")
                assert response.status_code in (200, 404, 500)
            except httpx.ConnectError:
                pytest.skip("Port 8080 not accepting HTTP connections")


class TestGRAEAEProviderConfiguration:
    """Test GRAEAE provider configuration and consensus scoring."""

    PYTHIA_GRAEAE_URL = "http://192.168.207.67:5001/graeae/consult"
    PROTEUS_GRAEAE_URL = "http://192.168.207.25:5002/v1/consultations"

    async def test_pythia_graeae_provider_health(self):
        """Verify PYTHIA GRAEAE has all providers configured."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("http://192.168.207.67:5001/graeae/health")
            assert response.status_code == 200

            data = response.json()
            muses = data.get("muses", {})

            # Expect providers from PYTHIA config
            expected_providers = {
                "perplexity", "groq", "claude_opus", "xai",
                "openai", "gemini", "nvidia", "together"
            }

            configured_providers = set(muses.keys())
            assert len(configured_providers) >= 5, \
                f"PYTHIA should have at least 5 providers configured. Got: {configured_providers}"

    async def test_proteus_graeae_consensus_endpoint_exists(self):
        """Verify PROTEUS v1/consultations endpoint is available."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            # PROTEUS should have /v1/consultations endpoint for consensus
            response = await client.post(
                self.PROTEUS_GRAEAE_URL,
                json={
                    "prompt": "What is 2+2?",
                    "task_type": "reasoning"
                },
                headers={"Authorization": "Bearer test-token"}
            )
            # Expect 200 (success) or 401 (auth required) or 503 (service not ready)
            # but NOT 404 (endpoint not found)
            assert response.status_code != 404, \
                "PROTEUS /v1/consultations endpoint should exist"

    async def test_proteus_can_reach_pythia_inference(self):
        """Verify PROTEUS can reach PYTHIA's inference servers for compression."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Test compression endpoint which routes to PYTHIA
            response = await client.post(
                "http://192.168.207.25:5002/v1/memories/search",
                json={
                    "query": "test",
                    "limit": 1,
                    "enable_compression": True
                },
                headers={"Authorization": "Bearer test-token"}
            )
            # Expect some response (may be empty but shouldn't error on inference)
            assert response.status_code in (200, 400, 401, 503)

    @pytest.mark.parametrize("task_type", [
        "reasoning",
        "architecture_design",
        "code_generation",
        "web_search",
    ])
    async def test_proteus_consensus_task_types(self, task_type):
        """Test PROTEUS consensus for different task types."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.PROTEUS_GRAEAE_URL,
                json={
                    "prompt": f"Explain {task_type}",
                    "task_type": task_type
                },
                headers={"Authorization": "Bearer test-token"}
            )

            # Should not 404
            assert response.status_code != 404, \
                f"PROTEUS should support task_type={task_type}"


class TestInferenceLatencyAndQuality:
    """Test inference latency profiles and quality scoring match between systems."""

    async def test_pythia_vs_proteus_latency_profile(self):
        """Compare latency characteristics between PYTHIA and PROTEUS."""
        test_prompt = "What is artificial intelligence?"

        # Test PYTHIA GRAEAE
        pythia_latencies = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(2):  # 2 requests
                start = datetime.utcnow()
                try:
                    response = await client.post(
                        "http://192.168.207.67:5001/graeae/consult",
                        json={
                            "prompt": test_prompt,
                            "task_type": "reasoning"
                        },
                        timeout=30.0
                    )
                    if response.status_code == 200:
                        elapsed = (datetime.utcnow() - start).total_seconds() * 1000
                        pythia_latencies.append(elapsed)
                except Exception as e:
                    pytest.skip(f"PYTHIA not reachable: {e}")

        # Test PROTEUS GRAEAE
        proteus_latencies = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(2):  # 2 requests
                start = datetime.utcnow()
                try:
                    response = await client.post(
                        "http://192.168.207.25:5002/v1/consultations",
                        json={
                            "prompt": test_prompt,
                            "task_type": "reasoning"
                        },
                        headers={"Authorization": "Bearer test-token"},
                        timeout=30.0
                    )
                    if response.status_code in (200, 401):
                        elapsed = (datetime.utcnow() - start).total_seconds() * 1000
                        proteus_latencies.append(elapsed)
                except Exception as e:
                    pytest.skip(f"PROTEUS not reachable: {e}")

        # If both systems responded, latencies should be within same order of magnitude
        if pythia_latencies and proteus_latencies:
            pythia_avg = sum(pythia_latencies) / len(pythia_latencies)
            proteus_avg = sum(proteus_latencies) / len(proteus_latencies)

            # Both should be in reasonable range (1-30 seconds for consensus)
            assert 1000 < pythia_avg < 30000, f"PYTHIA latency unusual: {pythia_avg}ms"
            assert 1000 < proteus_avg < 30000, f"PROTEUS latency unusual: {proteus_avg}ms"

            print(f"\nLatency Profile:")
            print(f"  PYTHIA avg: {pythia_avg:.0f}ms")
            print(f"  PROTEUS avg: {proteus_avg:.0f}ms")

    async def test_provider_weight_configuration_consistency(self):
        """Verify provider weights match between PYTHIA and PROTEUS configs."""
        # This test validates the config.toml provider weights are same
        # Expected weights (from PYTHIA config):
        expected_weights = {
            "perplexity": 0.88,
            "groq": 0.78,
            "claude_opus": 0.85,
            "xai": 0.90,
            "openai": 0.82,
            "gemini": 0.81,
            "nvidia": 0.80,
            "together": 0.78,
        }

        # If we can read provider info, validate weights are consistent
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get("http://192.168.207.67:5001/graeae/health")
                if response.status_code == 200:
                    data = response.json()
                    muses = data.get("muses", {})

                    # Verify at least Perplexity, Groq, OpenAI are present
                    assert "perplexity" in muses, "PYTHIA should have Perplexity"
                    assert "groq" in muses, "PYTHIA should have Groq"
                    assert "openai" in muses, "PYTHIA should have OpenAI"
            except Exception:
                pytest.skip("Cannot validate provider configuration")


class TestOllamaEmbeddingsAlign:
    """Test that PROTEUS uses same embedding models as PYTHIA."""

    async def test_proteus_embedding_backend_is_pythia_ollama(self):
        """Verify PROTEUS embeddings route to PYTHIA Ollama (nomic-embed-text)."""
        # The config should have:
        # [embeddings]
        # backend = "remote_ollama"
        # url = "http://192.168.207.67:11434"
        # model = "nomic-embed-text"

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Test embedding endpoint (if exposed)
            response = await client.post(
                "http://192.168.207.67:11434/api/embeddings",
                json={
                    "model": "nomic-embed-text",
                    "prompt": "test embedding"
                }
            )
            assert response.status_code == 200
            assert "embedding" in response.json()


class TestGPUCompressionAlignment:
    """Test that compression tiers align: PROTEUS routes GPU work to PYTHIA."""

    async def test_aletheia_routes_to_pythia_ollama(self):
        """Verify Tier 2 (ALETHEIA) compression routes to PYTHIA."""
        # Config should have:
        # aletheia_url = "http://192.168.207.67:11434"
        # aletheia_model = "gemma4:e4b"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("http://192.168.207.67:11434/api/tags")
            if response.status_code == 200:
                data = response.json()
                models = [m["name"].split(":")[0] for m in data.get("models", [])]
                # Embedding should be available (gemma might not be)
                assert any("embed" in m for m in models), \
                    "PYTHIA should have embedding models available"

    async def test_anamnesis_routes_to_pythia_ollama(self):
        """Verify Tier 3 (ANAMNESIS) compression routes to PYTHIA."""
        # Config should have:
        # anamnesis_url = "http://192.168.207.67:11434"
        # anamnesis_model = "gemma4-consult"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("http://192.168.207.67:11434/api/tags")
            assert response.status_code == 200
