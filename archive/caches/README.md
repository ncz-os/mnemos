# Archive: Caching Layer

**Source**: `mnemos-production.git.broken` / feature/background-embedding-job (Feb 2026)
**Status**: NOT WIRED — production has no embedding or compression caching
**Integration effort**: MEDIUM

## Files

### `embedding_cache.py` (171 lines)
SHA256-keyed in-memory LRU cache for 768-dim Nomic embedding vectors.
- Key: `memory_id` or SHA256 hash of text content
- Configurable max size (default 1000 entries) and optional TTL
- Thread-safe; `get()`, `put()`, `invalidate()`, `stats()` interface
- **Wire into**: `api/lifecycle.py` `_get_embedding()` — wrap the Ollama call

### `compression_cache.py` (100 lines)
MD5-keyed LRU cache for compression results with 24-hour TTL.
- Key: MD5 of original content
- Prevents re-compressing the same text on repeated queries
- **Wire into**: `compression/distillation_engine.py` before calling extractive token filter/SENTENCE

### `dual_layer_cache.py` (286 lines)
L1 (Python dict, fast) + L2 (PostgreSQL, persistent) two-tier cache.
- L1 warms from L2 on startup; L1 writes through to L2
- Thread-safe with RLock; LRU eviction on L1
- Survives service restarts (L2 persistence)
- **Wire into**: GRAEAE response caching (`graeae/_cache.py` currently in-process only)

## Integration order

1. `embedding_cache.py` — lowest risk, highest payoff (Ollama calls are ~200ms each)
2. `compression_cache.py` — prevents extractive token filter/SENTENCE from running on already-seen content
3. `dual_layer_cache.py` — only if GRAEAE cache needs to survive restarts

## Notes

- All three are self-contained; no cross-dependencies
- `dual_layer_cache.py` requires a DB table — add migration before wiring
