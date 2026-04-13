# Archive: Test Suites from Old Branches

**Source**: `argonas/launch-prep` branch
**Status**: NOT WIRED — these are standalone test files that can be run against current code
**Integration effort**: LOW (pytest-compatible; review imports before running)

## Files

### `test_graeae_features.py` (~40 tests)
Comprehensive tests for GRAEAE infrastructure modules:
- Circuit breaker state transitions (CLOSED → OPEN → HALF_OPEN → CLOSED)
- Rate limiter: token bucket, per-provider limits
- Quality scorer: relevance/coherence/toxicity metric calculation
- Semantic cache: similarity-based hit/miss, TTL expiry
- Queue: enqueue, dequeue, retry, abandon lifecycle
- **Note**: some tests reference SQLite-backed modules (`graeae/core/`) that were correctly
  removed. Those tests will need mocking or module stubs. The circuit breaker and rate limiter
  tests should work against `graeae/_circuit_breaker.py` and `graeae/_rate_limiter.py` directly.

### `test_hooks.py` (~15 tests)
pytest-asyncio tests for `modules/hooks/HookRegistry`:
- `register()` / `unregister()` — callback registration
- `trigger()` — sync and async callbacks, context mutation, error isolation
- `enable_hook()` / `disable_hook()` — runtime enable/disable
- `get_history()` — event history with filtering
- **Status**: should work against the restored `modules/hooks/hook_registry.py` without changes.
  Run: `pytest archive/tests/test_hooks.py -v`

## Running the tests

```bash
cd /opt/mnemos
# Hook tests (should pass now)
pytest archive/tests/test_hooks.py -v

# GRAEAE tests (partial — need import fixes for removed modules)
pytest archive/tests/test_graeae_features.py -v --ignore-glob="*queue*" 2>&1 | head -50
```
