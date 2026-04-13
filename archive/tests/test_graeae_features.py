"""
GRAEAE Feature Tests — archive/tests edition
Archived: 2026-04-12

Tests production modules (graeae v2, no graeae.core.* sub-packages):
  - graeae._circuit_breaker  : CircuitBreaker, CircuitBreakerPool, CircuitState
  - graeae._rate_limiter     : RateLimiter, RateLimiterPool
  - graeae._quality          : ProviderQuality, QualityTracker
  - graeae._cache            : ResponseCache

All modules are in-memory only; no db_path or SQLite fixtures required.
"""

import time
import pytest

from graeae._circuit_breaker import CircuitBreaker, CircuitBreakerPool, CircuitState
from graeae._rate_limiter import RateLimiter, RateLimiterPool
from graeae._quality import ProviderQuality, QualityTracker
from graeae._cache import ResponseCache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROVIDERS = ['perplexity', 'groq', 'xai', 'openai', 'gemini', 'together', 'nvidia', 'ollama']

PROVIDER_WEIGHTS = {p: 1.0 for p in PROVIDERS}


@pytest.fixture
def cb_pool():
    """CircuitBreakerPool with low threshold for fast tests."""
    return CircuitBreakerPool(failure_threshold=3, cooldown_seconds=300)


@pytest.fixture
def cb():
    """Single CircuitBreaker with low threshold."""
    return CircuitBreaker('perplexity', failure_threshold=3, cooldown_seconds=300)


@pytest.fixture
def rl():
    """Single RateLimiter with small window for testing."""
    return RateLimiter('perplexity', rpm=3)


@pytest.fixture
def rl_pool():
    """RateLimiterPool with default limits."""
    return RateLimiterPool()


@pytest.fixture
def qt():
    """QualityTracker over the full provider set."""
    return QualityTracker(PROVIDER_WEIGHTS)


@pytest.fixture
def pq():
    """ProviderQuality with base weight 1.0."""
    return ProviderQuality(base_weight=1.0)


@pytest.fixture
def cache():
    """ResponseCache with short TTL for TTL tests."""
    return ResponseCache(ttl_seconds=3600)


# ===========================================================================
# Circuit Breaker Tests
# ===========================================================================

class TestCircuitBreaker:
    """Tests for CircuitBreaker and CircuitBreakerPool."""

    # --- 1. Initial state ---
    def test_initial_state_closed(self, cb):
        """Breaker starts in CLOSED state and allows requests."""
        assert cb.state == CircuitState.CLOSED
        assert cb.is_allowed() is True

    # --- 2. Trip on failure threshold ---
    def test_trips_to_open_after_threshold(self, cb):
        """After failure_threshold failures the breaker opens."""
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.is_allowed() is False

    # --- 3. Half-open recovery via _opened_at manipulation ---
    def test_half_open_after_cooldown(self):
        """Breaker transitions to HALF_OPEN once cooldown elapses."""
        from datetime import datetime, timezone, timedelta
        breaker = CircuitBreaker('groq', failure_threshold=2, cooldown_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # Wind back the clock so cooldown appears elapsed
        breaker._opened_at = datetime.now(timezone.utc) - timedelta(seconds=61)

        assert breaker.is_allowed() is True
        assert breaker.state == CircuitState.HALF_OPEN

    # --- 4. HALF_OPEN → CLOSED on success_threshold successes ---
    def test_closes_after_probe_successes(self):
        """Breaker returns to CLOSED after enough probe successes."""
        from datetime import datetime, timezone, timedelta
        breaker = CircuitBreaker('openai', failure_threshold=2, cooldown_seconds=60,
                                 success_threshold=2)
        breaker.record_failure()
        breaker.record_failure()
        breaker._opened_at = datetime.now(timezone.utc) - timedelta(seconds=61)
        breaker.is_allowed()  # → HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

        breaker.record_success()
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED

    # --- 5. Pool: providers are independent ---
    def test_pool_providers_independent(self, cb_pool):
        """Tripping one provider does not affect others."""
        for _ in range(3):
            cb_pool.record_failure('perplexity')
        assert cb_pool.is_allowed('perplexity') is False
        assert cb_pool.is_allowed('groq') is True
        assert cb_pool.is_allowed('openai') is True

    # --- 6. Pool status dict ---
    def test_pool_status_reflects_state(self, cb_pool):
        """status() returns state and failure count for all seen providers."""
        cb_pool.record_failure('xai')
        status = cb_pool.status()
        assert 'xai' in status
        assert status['xai']['state'] == 'closed'  # 1 failure, threshold=3
        assert status['xai']['failures'] == 1

    # --- 7. Failure in HALF_OPEN re-opens ---
    def test_failure_in_half_open_reopens(self):
        """A failure during probe puts the breaker back to OPEN."""
        from datetime import datetime, timezone, timedelta
        breaker = CircuitBreaker('nvidia', failure_threshold=2, cooldown_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        breaker._opened_at = datetime.now(timezone.utc) - timedelta(seconds=61)
        breaker.is_allowed()  # → HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

        breaker.record_failure()  # back to OPEN (failures >= threshold again)
        assert breaker.state == CircuitState.OPEN

    # --- 8. Success in CLOSED decays failure count ---
    def test_success_in_closed_decays_failures(self, cb):
        """record_success in CLOSED state decrements failure count."""
        cb.record_failure()
        cb.record_failure()
        assert cb._failures == 2
        cb.record_success()
        assert cb._failures == 1


# ===========================================================================
# Rate Limiter Tests
# ===========================================================================

class TestRateLimiter:
    """Tests for RateLimiter and RateLimiterPool."""

    # --- 1. Requests under limit are allowed ---
    def test_allows_under_limit(self, rl):
        """Three requests against an rpm=3 limiter are all allowed."""
        assert rl.is_allowed() is True
        assert rl.is_allowed() is True
        assert rl.is_allowed() is True

    # --- 2. Rejects when limit reached ---
    def test_rejects_over_limit(self, rl):
        """The 4th request against rpm=3 is rejected."""
        rl.is_allowed()
        rl.is_allowed()
        rl.is_allowed()
        assert rl.is_allowed() is False

    # --- 3. current_rpm reflects window ---
    def test_current_rpm_count(self, rl):
        """current_rpm returns the number of requests in the window."""
        rl.is_allowed()
        rl.is_allowed()
        assert rl.current_rpm() == 2

    # --- 4. Window reset: old timestamps expire ---
    def test_window_expiry_allows_new_requests(self):
        """After the 60-second window, old entries expire and new requests are allowed."""
        limiter = RateLimiter('gemini', rpm=2)
        limiter.is_allowed()
        limiter.is_allowed()
        assert limiter.is_allowed() is False  # at limit

        # Backdate all timestamps past the window
        limiter._timestamps = [t - 61 for t in limiter._timestamps]
        assert limiter.is_allowed() is True  # window cleared

    # --- 5. Pool: per-provider independence ---
    def test_pool_per_provider_independence(self, rl_pool):
        """Different providers have independent rate limits."""
        # Both should be allowed under default limits
        assert rl_pool.is_allowed('perplexity') is True
        assert rl_pool.is_allowed('groq') is True

    # --- 6. Pool: unknown provider gets default limit ---
    def test_pool_unknown_provider_default(self, rl_pool):
        """An unlisted provider gets the default _DEFAULT_RPM limit."""
        assert rl_pool.is_allowed('new_provider') is True

    # --- 7. Pool status dict ---
    def test_pool_status_returns_rpm_counts(self, rl_pool):
        """status() returns current rpm counts for all limiters."""
        rl_pool.is_allowed('together')
        status = rl_pool.status()
        assert 'together' in status
        assert status['together'] >= 1

    # --- 8. Exhausting pool limiter ---
    def test_pool_exhausts_tight_provider(self):
        """A pool entry with rpm=2 rejects the 3rd consecutive request."""
        pool = RateLimiterPool(overrides={'tight_provider': 2})
        assert pool.is_allowed('tight_provider') is True
        assert pool.is_allowed('tight_provider') is True
        assert pool.is_allowed('tight_provider') is False


# ===========================================================================
# Quality Tracker Tests
# ===========================================================================

class TestQualityTracker:
    """Tests for ProviderQuality and QualityTracker."""

    # --- 1. Fresh tracker returns base weight ---
    def test_fresh_tracker_returns_base_weight(self, pq):
        """With no outcomes recorded, dynamic_weight equals base_weight."""
        assert pq.dynamic_weight() == 1.0

    # --- 2. All successes keeps weight at base ---
    def test_all_successes_full_weight(self, pq):
        """100% success rate → multiplier 1.0 → weight unchanged."""
        for _ in range(5):
            pq.record_success(latency_ms=100)
        assert pq.dynamic_weight() == 1.0

    # --- 3. All failures halves the weight ---
    def test_all_failures_halves_weight(self, pq):
        """0% success rate → multiplier 0.5 → weight halved."""
        for _ in range(5):
            pq.record_failure()
        assert pq.dynamic_weight() == pytest.approx(0.5, abs=1e-4)

    # --- 4. avg_latency_ms tracks latency ---
    def test_avg_latency(self, pq):
        """avg_latency_ms is the mean of recorded latencies."""
        pq.record_success(200)
        pq.record_success(400)
        assert pq.avg_latency_ms() == 300

    # --- 5. QualityTracker record and dynamic_weight ---
    def test_quality_tracker_record_success(self, qt):
        """Recording successes via QualityTracker increases dynamic_weight."""
        for _ in range(5):
            qt.record_success('perplexity', latency_ms=50)
        assert qt.dynamic_weight('perplexity') == 1.0

    # --- 6. QualityTracker record failure lowers weight ---
    def test_quality_tracker_record_failure_lowers_weight(self, qt):
        """Recording failures reduces the dynamic weight toward 0.5."""
        for _ in range(10):
            qt.record_failure('groq')
        weight = qt.dynamic_weight('groq')
        assert weight < 1.0
        assert weight >= 0.5

    # --- 7. QualityTracker status ---
    def test_quality_tracker_status(self, qt):
        """status() returns dict with dynamic_weight, base_weight, avg_latency_ms."""
        qt.record_success('openai', 150)
        status = qt.status()
        assert 'openai' in status
        entry = status['openai']
        assert 'dynamic_weight' in entry
        assert 'base_weight' in entry
        assert 'avg_latency_ms' in entry

    # --- 8. Unknown provider returns 0.0 ---
    def test_unknown_provider_returns_zero(self, qt):
        """Querying a provider not in the tracker returns 0.0."""
        assert qt.dynamic_weight('unknown_llm') == 0.0


# ===========================================================================
# Response Cache Tests
# ===========================================================================

class TestResponseCache:
    """Tests for ResponseCache."""

    # --- 1. Cache miss on empty cache ---
    def test_cache_miss_on_empty(self, cache):
        """get() returns None when no entry exists."""
        result = cache.get('What is Python?', 'reasoning')
        assert result is None

    # --- 2. Cache hit after set ---
    def test_cache_hit_after_set(self, cache):
        """get() returns the stored value after set()."""
        cache.set('What is Python?', 'reasoning', {'answer': 'A language'})
        result = cache.get('What is Python?', 'reasoning')
        assert result == {'answer': 'A language'}

    # --- 3. Different keys do not collide ---
    def test_different_keys_no_collision(self, cache):
        """Two prompts with the same task_type are stored independently."""
        cache.set('prompt A', 'architecture_design', 'response A')
        cache.set('prompt B', 'architecture_design', 'response B')
        assert cache.get('prompt A', 'architecture_design') == 'response A'
        assert cache.get('prompt B', 'architecture_design') == 'response B'

    # --- 4. Same prompt, different task_type → different keys ---
    def test_task_type_differentiates_keys(self, cache):
        """Same prompt with different task_type produces separate cache entries."""
        cache.set('hello', 'reasoning', 'r1')
        cache.set('hello', 'code_generation', 'r2')
        assert cache.get('hello', 'reasoning') == 'r1'
        assert cache.get('hello', 'code_generation') == 'r2'

    # --- 5. Prompt normalisation (case-insensitive) ---
    def test_prompt_normalisation(self, cache):
        """Prompts are normalised to lowercase before hashing."""
        cache.set('Hello World', 'reasoning', 'normalised')
        assert cache.get('hello world', 'reasoning') == 'normalised'
        assert cache.get('HELLO WORLD', 'reasoning') == 'normalised'

    # --- 6. TTL expiry ---
    def test_ttl_expiry(self):
        """Entries expire after ttl_seconds."""
        short_cache = ResponseCache(ttl_seconds=1)
        short_cache.set('expiring', 'reasoning', 'soon gone')
        assert short_cache.get('expiring', 'reasoning') == 'soon gone'
        time.sleep(1.1)
        assert short_cache.get('expiring', 'reasoning') is None

    # --- 7. Stats: hits and misses tracked ---
    def test_stats_tracks_hits_misses(self, cache):
        """stats() correctly counts hits and misses."""
        cache.set('q', 'reasoning', 'v')
        cache.get('q', 'reasoning')   # hit
        cache.get('missing', 'reasoning')  # miss
        stats = cache.stats()
        assert stats['hits'] == 1
        assert stats['misses'] == 1

    # --- 8. LRU eviction at max_entries ---
    def test_lru_eviction(self):
        """Old entries are evicted when max_entries is exceeded."""
        tiny = ResponseCache(ttl_seconds=3600, max_entries=3)
        tiny.set('a', 't', 'v1')
        tiny.set('b', 't', 'v2')
        tiny.set('c', 't', 'v3')
        tiny.set('d', 't', 'v4')  # should evict 'a'
        assert tiny.stats()['entries'] == 3
        assert tiny.get('a', 't') is None
        assert tiny.get('d', 't') == 'v4'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
