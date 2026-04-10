"""
End-to-End Tests for MNEMOS

Tests complete workflows:
- Memory creation with compression
- Quality assessment and reversal
- Graeae consultation
- State synchronization
- Hook system
- Bundle routing
"""

import pytest
import asyncio

# Note: In production, these would import from actual modules
# from modules.compression import distill, get_distillation_engine
# from core.memory_store import MemoryStore
# from modules.hooks import HookRegistry, HOOK_SESSION_START


class TestCompressionWorkflow:
    """Test compression across write/read/graeae paths"""

    def test_hyco_compression(self):
        """Test extractive token filter fast compression"""
        from modules.compression import distill

        text = """The important project requires immediate attention to critical issues.
        We need to focus on the key deliverables and timeline.
        The team should prioritize high-impact work."""

        result = distill(text, strategy="hyco", ratio=0.40)

        assert result['original_tokens'] > 0
        assert result['compressed_tokens'] > 0
        assert result['compressed_tokens'] < result['original_tokens']
        assert 0.30 < result['compression_ratio'] < 0.50
        assert 0.80 <= result['quality_score'] <= 1.0
        assert len(result['compressed_text']) > 0

    def test_sac_compression(self):
        """Test SENTENCE structure-preserving compression"""
        from modules.compression import distill

        text = """First paragraph about the restaurant.

The restaurant serves Italian food. It has been operating for 20 years.
The chef trained in Florence. Quality is our priority."""

        result = distill(text, strategy="sac", ratio=0.50)

        assert result['compressed_tokens'] < result['original_tokens']
        assert result["compression_ratio"] <= 0.65
        assert result['quality_score'] >= 0.80
        assert 'restaurant' in result['compressed_text'].lower()

    def test_auto_strategy_selection(self):
        """Test intelligent strategy selection"""
        from modules.compression import distill

        # Unstructured text should use extractive token filter
        unstructured = "The quick brown fox jumps over the lazy dog. " * 10
        result = distill(unstructured, strategy="auto")
        assert result['strategy_used'] in ['hyco', 'sac']

        # Structured text should prefer SENTENCE
        structured = """- Item 1
- Item 2
- Item 3

This is a paragraph.
- Another item
- Final item"""
        result = distill(structured, strategy="auto")
        assert result['strategy_used'] in ['hyco', 'sac']

    def test_task_specific_ratios(self):
        """Test task-type specific compression ratios"""
        from modules.compression import distill

        text = "Architecture design discussion. " * 20

        # Architecture should use 0.50 ratio
        result = distill(text, task_type="architecture_design", strategy="auto")
        assert result['compression_ratio'] <= 0.55

        # Code generation should use 0.30 ratio
        result = distill(text, task_type="code_generation", strategy="auto")
        assert result['compression_ratio'] <= 0.35

    def test_batch_compression(self):
        """Test batch compression operations"""
        from modules.compression import get_distillation_engine

        engine = get_distillation_engine()

        texts = [
            "First text to compress. " * 10,
            "Second text with different content. " * 10,
            "Third batch item for testing. " * 10,
        ]

        results = engine.batch_distill(texts, strategy="auto")

        assert len(results) == 3
        for result in results:
            assert 'compressed' in result
            assert 'quality_score' in result
            assert 0 < result['quality_score'] <= 1.0

    def test_distillation_stats(self):
        """Test statistics tracking"""
        from modules.compression import get_distillation_engine

        engine = get_distillation_engine()
        engine.reset_stats()

        # Compress multiple texts
        for i in range(5):
            text = f"Test text {i} content. " * 10
            engine.distill(text, strategy="auto")

        stats = engine.get_stats()

        assert stats['total_compressions'] == 5
        assert stats['total_input_tokens'] > 0
        assert stats['total_output_tokens'] > 0
        assert stats['average_ratio'] < 1.0
        assert stats['compression_efficiency'] > 0


class TestHookSystem:
    """Test hook system"""

    def test_hook_registry_creation(self):
        """Test hook registry initialization"""
        from modules.hooks import HookRegistry

        registry = HookRegistry()
        assert registry is not None
        assert len(registry.hooks) == 0

    def test_hook_registration(self):
        """Test registering hooks"""
        from modules.hooks import HookRegistry
        from unittest.mock import Mock

        registry = HookRegistry()
        callback = Mock()

        registry.register('test.event', callback)

        assert 'test.event' in registry.hooks
        assert callback in registry.hooks['test.event']

    @pytest.mark.asyncio
    async def test_hook_execution(self):
        """Test triggering hooks"""
        from modules.hooks import HookRegistry

        registry = HookRegistry({'hooks': {'enabled': True}})

        async def callback(context):
            context['executed'] = True
            return context

        registry.register('test.event', callback)

        context = {'data': 'test'}
        result = await registry.trigger('test.event', context)

        assert result['executed'] is True

    @pytest.mark.asyncio
    async def test_prompt_submit_hook(self):
        """Test PromptSubmitHook task detection"""
        from modules.hooks.prompt_submit import PromptSubmitHook

        hook = PromptSubmitHook()

        # Test task type detection
        test_cases = [
            ("Write a Python function", "code_generation"),
            ("Design microservices", "architecture_design"),
            ("Explain why", "reasoning"),
            ("Debug this error", "debugging"),
        ]

        for prompt, expected_type in test_cases:
            context = {'prompt': prompt}
            result = await hook(context)
            assert result['detected_task_type'] == expected_type


class TestMemoryTierSystem:
    """Test memory categorization and tiers"""

    def test_tier_definitions(self):
        """Test 4-tier system"""
        from modules.memory_categorization import TIERS

        assert len(TIERS) == 4
        assert TIERS[1].tier_level == 1
        assert TIERS[1].compression_ratio == 0.20
        assert TIERS[4].compression_ratio == 1.00

    def test_tier_selector(self):
        """Test tier selection logic"""
        from modules.memory_categorization import TierSelector

        selector = TierSelector()

        # Simple task
        tiers = selector.select_tiers('reasoning', 'simple')
        assert len(tiers) == 1
        assert tiers[0].tier_level == 2

        # Complex task
        tiers = selector.select_tiers('reasoning', 'complex')
        assert len(tiers) == 4  # All tiers

    def test_tier_selection_by_tokens(self):
        """Test tier selection by token budget"""
        from modules.memory_categorization import TierSelector

        selector = TierSelector()

        # Need 5000 tokens
        tier = selector.select_by_token_budget(5000)
        assert tier.token_budget >= 5000

        # Need 50000 tokens
        tier = selector.select_by_token_budget(50000)
        assert tier.token_budget >= 50000

    def test_complexity_detection(self):
        """Test task complexity detection"""
        from modules.memory_categorization import TierSelector

        selector = TierSelector()

        # Simple task
        complexity = selector.detect_complexity("Hi")
        assert complexity == 'simple'

        # Complex task
        complexity = selector.detect_complexity("Complex task " * 30)
        assert complexity in ['medium', 'complex']


class TestBundleSystem:
    """Test consultation bundles and routing"""

    def test_bundle_definitions(self):
        """Test bundle definitions"""
        from modules.bundles import BUNDLES

        assert 'code_generation' in BUNDLES
        assert 'architecture_design' in BUNDLES
        assert 'reasoning' in BUNDLES

        bundle = BUNDLES['code_generation']
        assert bundle.bundle_type == 'code_generation'
        assert len(bundle.models) > 0
        assert bundle.consensus_score > 0

    def test_bundle_router(self):
        """Test bundle routing"""
        from modules.bundles import BundleRouter

        router = BundleRouter()

        # Select bundle
        bundle = router.select_bundle('code_generation')
        assert bundle is not None
        assert bundle.bundle_type == 'code_generation'

        # Get models
        models = router.select_models('architecture_design')
        assert len(models) > 0

    def test_model_variants(self):
        """Test model variant discovery"""
        from modules.bundles import ModelVariants

        # List provider models
        models = ModelVariants.list_provider_models('openai')
        assert len(models) > 0

        # Find by capability
        reasoning_models = ModelVariants.find_by_capability('reasoning')
        assert len(reasoning_models) > 0

        # Find fastest
        fastest = ModelVariants.find_fastest('coding')
        if fastest:
            assert fastest[0].latency_ms > 0

    def test_bundle_recommendation(self):
        """Test bundle recommendation"""
        from modules.bundles import BundleRouter

        router = BundleRouter()

        # Get recommendation
        rec = router.recommend_bundle("Design REST API for restaurants")
        assert rec['detected_task_type'] in [
            'api_design', 'code_generation', 'architecture_design'
        ]
        assert rec['recommended_bundle'] is not None


class TestIntegrations:
    """Test integration modules"""

    def test_macrodata_hook_adapter(self):
        """Test macrodata integration"""
        from integrations.macrodata import MacrodataHookAdapter

        # Create mock dependencies
        from unittest.mock import MagicMock

        adapter = MacrodataHookAdapter(
            memory_store=MagicMock(),
            state_manager=MagicMock(),
            compression_manager=MagicMock(),
            quality_analyzer=MagicMock(),
        )

        assert adapter is not None

    def test_state_synchronizer(self):
        """Test state synchronization"""
        from integrations.macrodata import StateSynchronizer

        from unittest.mock import MagicMock

        sync = StateSynchronizer(
            state_manager=MagicMock(),
            memory_store=MagicMock(),
        )

        assert sync is not None
        assert sync._state_hashes == {}

    @pytest.mark.asyncio
    async def test_provider_models(self):
        """Test external LLM provider integration"""
        from integrations.external_lms import ProviderModels

        provider_models = ProviderModels()
        assert provider_models is not None


class TestAPIEndpoints:
    """Test API server endpoints"""

    def test_api_imports(self):
        """Test API server imports"""
        try:
            from api_server import app
            assert app is not None
        except ImportError:
            pytest.skip("API server not available in test environment")

    def test_pydantic_models(self):
        """Test API request/response models"""
        try:
            from api_server import MemoryCreate, HealthResponse

            # Test MemoryCreate
            memory = MemoryCreate(
                content="Test memory",
                category="facts",
                task_type="reasoning"
            )
            assert memory.content == "Test memory"
            assert memory.category == "facts"

            # Test HealthResponse
            health = HealthResponse(
                status="healthy",
                timestamp="2026-02-05T00:00:00Z",
                database_connected=True,
                version="2.0.0"
            )
            assert health.status == "healthy"

        except ImportError:
            pytest.skip("API server models not available")


class TestCompleteWorkflow:
    """Integration test: complete memory workflow"""

    @pytest.mark.asyncio
    async def test_memory_creation_to_compression(self):
        """Test: create → compress → assess quality"""
        from modules.compression import distill

        # 1. Create memory
        memory_content = """The project requires immediate attention.
        We need to focus on key deliverables.
        Quality is essential."""

        # 2. Compress
        result = distill(memory_content, task_type="reasoning", strategy="auto")

        assert result['original_tokens'] > 0
        assert result['compressed_tokens'] > 0
        assert result['quality_score'] > 0.80

        # 3. Verify compression ratio
        assert result['compression_ratio'] < 0.50

    def test_task_routing_to_bundle(self):
        """Test: detect task → select bundle → get models"""
        from modules.bundles import BundleRouter
        from modules.hooks.prompt_submit import PromptSubmitHook

        # 1. Detect task type
        hook = PromptSubmitHook()
        prompt = "Design a microservices architecture"

        task_type = hook._detect_task_type(prompt)
        assert task_type in [
            'reasoning', 'architecture_design', 'code_generation'
        ]

        # 2. Route to bundle
        router = BundleRouter()
        bundle = router.select_bundle(task_type)
        assert bundle is not None

        # 3. Get models
        models = bundle.models
        assert len(models) > 0

    def test_compression_quality_assessment(self):
        """Test: compress → assess quality → calculate metrics"""
        from modules.compression import distill

        text = "Important decision requires careful analysis. " * 20

        result = distill(text, ratio=0.40, strategy="auto")

        # Verify all quality metrics present
        assert 'original_tokens' in result
        assert 'compressed_tokens' in result
        assert 'compression_ratio' in result
        assert 'quality_score' in result
        assert 'strategy_used' in result
        assert 'compression_time_ms' in result

        # Verify metrics are reasonable
        assert result['original_tokens'] > result['compressed_tokens']
        assert 0.30 <= result['compression_ratio'] <= 0.50
        assert 0.80 <= result['quality_score'] <= 1.0
        assert result['compression_time_ms'] > 0


# Test fixtures
@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "--tb=short"])
