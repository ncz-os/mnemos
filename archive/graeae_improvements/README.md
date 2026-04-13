# Archive: GRAEAE Provider Routing Improvements

**Source**: `graeae.git.old` (Jan–Feb 2026 standalone GRAEAE repo)
**Status**: NOT WIRED — current production uses static provider weights from Arena.ai Elo
**Integration effort**: HIGH (review against graeae/engine.py before merging)

## Files

### `fitness_calculator.py` (~150 lines)
Task-type–weighted provider scoring model.
- Weights: Elo 30%, latency 25%, cost 20%, reliability 15%, specialization 10%
- Adjusts per task: reasoning tasks upweight Elo; speed tasks upweight latency; budget tasks upweight cost
- Returns `FitnessScore(provider, model, total, breakdown)` dataclass
- **Key value**: the task-type weight adjustment table — current production uses flat Elo weights
  for all task types regardless of whether the user asked for reasoning vs fast code completion

### `dynamic_router.py` (~200 lines)
Persists per-provider latency/cost metrics to disk, routes by fitness score.
- `PerformanceTracker`: rolling 100-sample latency window per provider, P50/P95 latency
- `DynamicRouter`: uses `FitnessCalculator` + live latency data to rank providers at query time
- Depends on `fitness_calculator.py`
- **Caution**: routes by persisted JSON metrics file — production uses DB-backed Arena scores.
  The persistence mechanism needs to be replaced with a DB query before use.

### `graeae_response_cache.py` (~180 lines)
In-memory LRU consensus response cache.
- Hashes `prompt + task_type` as key; 24h TTL
- Stores full consensus result dict, not just the winning response
- Tracks cache hits, misses, latency savings
- **Complements** (does not replace) `graeae/_cache.py` — this caches at the consensus level,
  `_cache.py` caches at the individual-provider response level

### `graeae_response_ranker.py` (~120 lines)
Annotates each provider response with star rating and per-category label.
- Loads `provider_ranking.json` to get Elo-derived tier (⭐⭐⭐ to ⭐)
- Tags responses: "Best for reasoning", "Best for speed", etc.
- **Useful for**: the `/graeae/consult` response could surface these annotations to callers
  so they can choose which response to use rather than always taking the top-score winner

### `consultation_analytics.py` (~100 lines)
Stores GRAEAE query patterns to MNEMOS.
- Tracks: task_type distribution, provider win rates, latency by provider
- Posts analytics summaries to `/memories` under category `decisions`
- **Wire into**: `graeae/engine.py` post-consultation callback

## Integration notes

- `fitness_calculator.py` task weight table is the most immediately useful piece —
  copy the `TASK_WEIGHTS` dict into `graeae/engine.py` to weight provider selection per task type
- `dynamic_router.py` latency tracking needs DB storage (not JSON file) before production use
- These files predate the current provider registry; model IDs will need updating
