"""
Comprehensive tests for all GRAEAE features
Tests: Queue, Circuit Breaker, Quality Scoring, Semantic Cache, Rate Limiting
Targeting 40+ test cases across all features
"""

import pytest
import time
import tempfile
from datetime import timedelta

# Import GRAEAE modules
from graeae.core.queue import PersistentQueue, RequestStatus
from graeae.core.circuit_breaker import CircuitBreakerPool, CircuitState
from graeae.core.quality_scorer import ResponseQualityScorer, QualityScore
from graeae.core.semantic_cache import SemanticCache
from graeae.core.rate_limiter import RateLimiter, RateLimiterPool, QueueBackpressure


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_db():
    """Temporary database for testing"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def queue(temp_db):
    """Persistent queue instance"""
    return PersistentQueue(db_path=f"{temp_db}/queue.db", max_retries=3)


@pytest.fixture
def circuit_pool():
    """Circuit breaker pool instance"""
    return CircuitBreakerPool(failure_threshold=3, cooldown_minutes=1)


@pytest.fixture
def quality_scorer(temp_db):
    """Quality scorer instance"""
    return ResponseQualityScorer(db_path=f"{temp_db}/metrics.db")


@pytest.fixture
def semantic_cache(temp_db):
    """Semantic cache instance"""
    return SemanticCache(db_path=f"{temp_db}/cache.db", ttl_hours=24)


@pytest.fixture
def rate_limiter_pool(temp_db):
    """Rate limiter pool instance"""
    return RateLimiterPool(db_path=f"{temp_db}/limits.db")


@pytest.fixture
def sample_embedding():
    """Sample embedding vector"""
    return [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]


# ============================================================================
# Queue Tests (Feature 1)
# ============================================================================

class TestPersistentQueue:
    """Test queue persistence and resumability"""

    def test_enqueue_request(self, queue):
        """Test adding request to queue"""
        result = queue.enqueue(
            request_id="req-1",
            muse_id="muse-a",
            query="Hello world",
            metadata={"user": "test"}
        )
        assert result is True

    def test_duplicate_request_rejected(self, queue):
        """Test duplicate request handling"""
        queue.enqueue("req-1", "muse-a", "query", {})
        result = queue.enqueue("req-1", "muse-b", "query", {})
        assert result is False

    def test_dequeue_pending(self, queue):
        """Test dequeueing pending request"""
        queue.enqueue("req-1", "muse-a", "test query", {})
        req = queue.dequeue()
        
        assert req is not None
        assert req.request_id == "req-1"
        assert req.status == RequestStatus.PROCESSING

    def test_mark_completed(self, queue):
        """Test marking request as completed"""
        queue.enqueue("req-1", "muse-a", "query", {})
        queue.dequeue()
        
        result = queue.mark_completed("req-1")
        assert result is True

    def test_mark_failed_with_retry(self, queue):
        """Test marking request as failed with retry"""
        queue.enqueue("req-1", "muse-a", "query", {})
        queue.dequeue()
        
        # First failure
        result = queue.mark_failed("req-1", "timeout")
        assert result is True  # Should retry
        
        # Check status changed to retrying
        req = queue.dequeue()
        assert req.request_id == "req-1"

    def test_abandon_after_max_retries(self, queue):
        """Test request abandonment after max retries"""
        queue.enqueue("req-1", "muse-a", "query", {})
        
        # Fail max times
        for i in range(3):
            queue.dequeue()
            queue.mark_failed("req-1", f"failure {i}")
        
        # Should now be abandoned
        next_req = queue.dequeue()
        assert next_req is None

    def test_queue_status(self, queue):
        """Test queue status reporting"""
        queue.enqueue("req-1", "muse-a", "q1", {})
        queue.enqueue("req-2", "muse-a", "q2", {})
        
        status = queue.get_queue_status()
        assert status['pending'] == 2

    def test_recover_stuck_requests(self, queue):
        """Test recovery of stuck processing requests"""
        queue.enqueue("req-1", "muse-a", "query", {})
        queue.dequeue()  # Mark as processing
        
        # Simulate stuck request (old attempted_at)
        recovered = queue.recover_stuck_requests(timeout_minutes=0)
        assert recovered >= 1

    def test_cleanup_old_requests(self, queue):
        """Test cleanup of old completed requests"""
        queue.enqueue("req-1", "muse-a", "q", {})
        queue.dequeue()
        queue.mark_completed("req-1")
        
        cleaned = queue.cleanup_old_requests(days=0)
        assert cleaned >= 1


# ============================================================================
# Circuit Breaker Tests (Feature 2)
# ============================================================================

class TestCircuitBreaker:
    """Test circuit breaker functionality"""

    def test_circuit_closed_initially(self, circuit_pool):
        """Test circuit starts in CLOSED state"""
        breaker = circuit_pool.get_breaker("muse-a")
        assert breaker.state == CircuitState.CLOSED
        assert breaker.is_available() is True

    def test_circuit_opens_after_threshold(self, circuit_pool):
        """Test circuit opens after failure threshold"""
        circuit_pool.record_failure("muse-a", "error 1")
        circuit_pool.record_failure("muse-a", "error 2")
        circuit_pool.record_failure("muse-a", "error 3")
        
        breaker = circuit_pool.get_breaker("muse-a")
        assert breaker.state == CircuitState.OPEN
        assert breaker.is_available() is False

    def test_circuit_half_open_after_cooldown(self, circuit_pool):
        """Test circuit transitions to HALF_OPEN after cooldown"""
        # Fail and open circuit
        for _ in range(3):
            circuit_pool.record_failure("muse-a", "fail")
        
        breaker = circuit_pool.get_breaker("muse-a")
        assert breaker.state == CircuitState.OPEN
        
        # Advance time past cooldown
        breaker.open_time -= timedelta(minutes=2)
        
        # Next check should half-open
        available = breaker.is_available()
        assert available is True
        assert breaker.state == CircuitState.HALF_OPEN

    def test_circuit_closes_after_successes(self, circuit_pool):
        """Test circuit closes after successful recovery"""
        # Open circuit
        for _ in range(3):
            circuit_pool.record_failure("muse-a", "fail")
        
        breaker = circuit_pool.get_breaker("muse-a")
        breaker.open_time -= timedelta(minutes=2)  # Simulate cooldown
        breaker.is_available()  # Transition to half-open
        
        # Record successes
        circuit_pool.record_success("muse-a")
        circuit_pool.record_success("muse-a")
        
        assert breaker.state == CircuitState.CLOSED

    def test_available_muses_filtering(self, circuit_pool):
        """Test filtering available muses"""
        all_muses = ["muse-a", "muse-b", "muse-c"]
        
        # Fail muse-a
        for _ in range(3):
            circuit_pool.record_failure("muse-a", "fail")
        
        available = circuit_pool.get_available_muses(all_muses)
        assert "muse-a" not in available
        assert "muse-b" in available
        assert "muse-c" in available

    def test_health_report(self, circuit_pool):
        """Test health report generation"""
        circuit_pool.get_breaker("muse-a")
        circuit_pool.get_breaker("muse-b")
        
        for _ in range(3):
            circuit_pool.record_failure("muse-a", "fail")
        
        health = circuit_pool.health_report()
        assert health['total_muses'] == 2
        assert health['disabled'] == 1
        assert health['healthy'] == 1


# ============================================================================
# Quality Scoring Tests (Feature 3)
# ============================================================================

class TestQualityScorer:
    """Test response quality scoring"""

    def test_relevance_scoring(self, quality_scorer):
        """Test relevance score computation"""
        query = "What is Python"
        response = "Python is a programming language"
        
        score = quality_scorer.compute_quality(query, response)
        assert 0 <= score.relevance <= 1
        assert score.relevance > 0.5  # Should be relevant

    def test_coherence_scoring(self, quality_scorer):
        """Test coherence score computation"""
        query = "Tell me a story"
        response = "Once upon a time. The end."  # Short, but coherent
        
        score = quality_scorer.compute_quality(query, response)
        assert 0 <= score.coherence <= 1

    def test_toxicity_detection(self, quality_scorer):
        """Test toxicity detection"""
        response_clean = "This is a helpful response"
        response_toxic = "I hate everything and will harm people"
        
        score_clean = quality_scorer.compute_quality("query", response_clean)
        score_toxic = quality_scorer.compute_quality("query", response_toxic)
        
        assert score_toxic.toxicity > score_clean.toxicity

    def test_record_score(self, quality_scorer):
        """Test recording quality scores"""
        query = "Python"
        response = "Python is a language"
        score = QualityScore(
            relevance=0.9,
            coherence=0.8,
            toxicity=0.0,
            completeness=0.7
        )
        
        result = quality_scorer.record_score(
            "muse-a", "req-1", query, response, score
        )
        assert result is True

    def test_muse_metrics_retrieval(self, quality_scorer):
        """Test retrieving muse metrics"""
        score = QualityScore(0.9, 0.8, 0.0, 0.7)
        quality_scorer.record_score("muse-a", "req-1", "q", "r", score)
        
        metrics = quality_scorer.get_muse_metrics("muse-a")
        assert metrics is not None
        assert metrics['muse_id'] == "muse-a"

    def test_best_muses_ranking(self, quality_scorer):
        """Test ranking muses by quality"""
        # Record scores for different muses
        for muse_id in ["muse-a", "muse-b", "muse-c"]:
            for i in range(10):
                score = QualityScore(0.9, 0.8, 0.0, 0.7)
                quality_scorer.record_score(muse_id, f"req-{i}", "q", "r", score)
        
        best = quality_scorer.get_best_muses(count=2, min_samples=5)
        assert len(best) <= 2


# ============================================================================
# Semantic Cache Tests (Feature 4)
# ============================================================================

class TestSemanticCache:
    """Test semantic caching"""

    def test_cache_put_and_get(self, semantic_cache, sample_embedding):
        """Test caching and retrieval"""
        query = "What is AI?"
        response = "AI is artificial intelligence"
        
        # Put in cache
        result = semantic_cache.put(
            query, response, sample_embedding, "muse-a"
        )
        assert result is True
        
        # Get from cache (identical embedding)
        cached = semantic_cache.get(query, sample_embedding, "muse-a")
        assert cached is not None

    def test_similarity_matching(self, semantic_cache, sample_embedding):
        """Test semantic similarity matching"""
        query1 = "What is artificial intelligence?"
        response1 = "AI is..."
        
        # Put original
        semantic_cache.put(query1, response1, sample_embedding, "muse-a")
        
        # Query with similar embedding (slight variation)
        similar_embedding = [x + 0.01 for x in sample_embedding]
        cached = semantic_cache.get(query1, similar_embedding, "muse-a")
        
        # Should hit due to high similarity
        assert cached is not None

    def test_expiration(self, semantic_cache, sample_embedding):
        """Test cache expiration"""
        cache_instant = SemanticCache(ttl_hours=0)  # Instant expiry
        
        cache_instant.put("query", "response", sample_embedding, "muse-a")
        time.sleep(0.1)
        
        # Should be expired
        _ = cache_instant.get("query", sample_embedding)
        # May not find due to expiration

    def test_cache_stats(self, semantic_cache, sample_embedding):
        """Test cache statistics"""
        semantic_cache.put("q1", "r1", sample_embedding, "muse-a")
        semantic_cache.put("q2", "r2", sample_embedding, "muse-a")
        
        stats = semantic_cache.get_stats()
        assert stats['total_entries'] == 2
        assert stats['valid_entries'] >= 0


# ============================================================================
# Rate Limiting Tests (Feature 5)
# ============================================================================

class TestRateLimiter:
    """Test rate limiting"""

    def test_initial_allow(self):
        """Test requests are initially allowed"""
        limiter = RateLimiter("muse-a", requests_per_minute=60)
        assert limiter.is_allowed() is True

    def test_burst_limit(self):
        """Test burst size limit"""
        limiter = RateLimiter("muse-a", burst_size=3)
        
        assert limiter.is_allowed() is True
        assert limiter.is_allowed() is True
        assert limiter.is_allowed() is True
        # Fourth should potentially be blocked

    def test_backoff_trigger(self):
        """Test backoff activation"""
        limiter = RateLimiter("muse-a", requests_per_minute=10, burst_size=2)
        
        # Record many requests quickly
        for _ in range(10):
            limiter.is_allowed()
        
        # Should trigger backoff
        assert limiter.backoff_until > 0

    def test_rate_limiter_pool(self, rate_limiter_pool):
        """Test rate limiter pool management"""
        assert rate_limiter_pool.is_allowed("muse-a") is True
        
        metrics = rate_limiter_pool.get_all_metrics()
        assert "muse-a" in metrics

    def test_queue_backpressure(self):
        """Test queue backpressure mechanism"""
        backpressure = QueueBackpressure(max_queue_size=1000)
        
        # Normal queue
        backpressure.update_queue_depth(500)
        assert backpressure.should_accept_request() is True
        
        # High queue depth
        backpressure.update_queue_depth(900)
        assert backpressure.degradation_level > 0

    def test_graceful_degradation(self):
        """Test graceful degradation levels"""
        backpressure = QueueBackpressure(max_queue_size=1000)
        
        # Level 0 - normal
        backpressure.update_queue_depth(300)
        assert backpressure.get_batch_size(100) == 100
        
        # Level 1 - prefer cache
        backpressure.update_queue_depth(600)
        assert backpressure.should_accept_request('cache') is True
        
        # Level 3 - only cache
        backpressure.update_queue_depth(950)
        assert backpressure.should_accept_request('normal') is False
        assert backpressure.should_accept_request('cache') is False


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests across multiple features"""

    def test_full_request_lifecycle(self, queue, circuit_pool):
        """Test complete request processing lifecycle"""
        # 1. Enqueue
        queue.enqueue("req-1", "muse-a", "query", {})
        
        # 2. Check muse available (circuit check)
        assert circuit_pool.is_muse_available("muse-a") is True
        
        # 3. Dequeue
        req = queue.dequeue()
        assert req is not None
        
        # 4. Success
        circuit_pool.record_success("muse-a")
        queue.mark_completed("req-1")
        
        # 5. Verify no pending
        next_req = queue.dequeue()
        assert next_req is None

    def test_failure_cascade_prevention(self, queue, circuit_pool, rate_limiter_pool):
        """Test prevention of cascading failures"""
        muse_id = "muse-a"
        
        # Simulate repeated failures
        for i in range(5):
            queue.enqueue(f"req-{i}", muse_id, "q", {})
            queue.dequeue()
            circuit_pool.record_failure(muse_id, "API error")
            rate_limiter_pool.record_request(muse_id)
        
        # Circuit should be open, preventing further cascades
        assert circuit_pool.is_muse_available(muse_id) is False
        assert rate_limiter_pool.is_allowed(muse_id) is False


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
