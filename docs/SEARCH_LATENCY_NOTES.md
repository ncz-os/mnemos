# Search latency notes

The May 2026 figures previously quoted here came from an older deployment.
They do not establish latency for the current code, backend, model or host.
Use `STRESS_TEST_HARNESS_DESIGN.md` for the measurement requirements.

`mnemos.core.lifecycle._get_embedding` delegates to the runtime embedder.
Local OpenVINO/llama.cpp and explicitly selected HTTP embedding are supported.
The HTTP backend uses a persistent `httpx.AsyncClient`, a concurrency limit,
timeout and circuit breaker. It does not create a new client for every search.
Configure it with `MNEMOS_EMBED_BACKEND=http` and the `MNEMOS_EMBED_HTTP_*`
settings defined in `mnemos/core/config.py`; keep vector dimensions consistent
with database provisioning. Local models avoid an HTTP request but consume
per-worker model memory and CPU. Measure embedding and admission wait rather
than assuming either mode dominates.

The search route validates the request, resolves caller visibility, checks
its configured response cache, obtains an embedding for semantic search,
executes the backend repository search and serializes the result. Measure
cache hits and misses separately. PostgreSQL uses a pgvector HNSW cosine
index; exact query plans, visibility selectivity and search breadth determine
latency and recall. Recency reranking adds candidate work. SQLite vec0 uses
native exact cosine top-K; an incomplete index or restrictive filters can
require an authoritative fallback scan. MySQL's Python fallback pages rows
and retains K results, but still scans all eligible embeddings.

For each corpus and filter distribution record end-to-end percentiles,
embedding wait/inference, connection acquisition, query/reranking and
serialization. Existing search trace timestamps are cumulative; subtract
adjacent boundaries for deltas and do not mix different trace IDs. Capture
`EXPLAIN (ANALYZE, BUFFERS)` for PostgreSQL and count eligible rows/fallbacks
for SQLite/MySQL. No current latency or capacity guarantee follows from
old isolated reranking measurements.
