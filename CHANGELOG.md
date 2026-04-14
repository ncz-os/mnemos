# Changelog

All notable changes to MNEMOS are documented here.

## [2.3.0] — 2026-04-12

### Added
- Knowledge Graph API (`/triples`, `GET /timeline/{subject}`) — temporal triple store with `valid_from`/`valid_until`
- Journal API (`/journal`) — date-partitioned operational log
- Key-Value State API (`/state/{key}`) — persistent session state store
- Entity tracking API (`/entities`) — people, projects, concepts with bidirectional links
- Model Registry (`/model-registry/`) — live provider model catalog with Arena.ai Elo sync
- extractive token filter compression (Hybrid Compression with Online Learning) — 57% reduction at 0.48ms, no ML required
- SENTENCE compression (Semantic-Anchor Compression) — structure-preserving sentence selection
- DistillationEngine — AUTO strategy selects extractive token filter vs SENTENCE; LLM fallback on quality < 60
- Background embedding worker (archive/) — NULL-embedding backfill with GPU inference pre-compression
- Prometheus instrumentation (archive/) — request counters, latency histograms, pool utilization
- Cryptographic audit chain — SHA-256 hash-chained GRAEAE prompt/response log
- Per-provider circuit breakers, rate limiters, quality tracking in GRAEAE engine
- Multi-user RLS (Row Level Security) via PostgreSQL session variables
- Memory versioning with diff and revert

### Changed
- GRAEAE is now embedded in the MNEMOS API (port 5002) — no separate server process required
- Database port changed from 5000 to 5002
- `memories.id` changed from UUID to TEXT (supports `mem_XXXXX` prefixed IDs)
- Compression pipeline: distillation_worker now uses extractive token filter/SENTENCE before LLM fallback
- GRAEAE providers: Arena.ai Elo scores used for dynamic provider weighting

### Fixed
- `kg_triples` table missing from migrations (KG API 500'd on clean installs)
- `memories.id UUID` rejected app's `mem_xxx` TEXT IDs
- `compression_quality_log.memory_id` type mismatch
- `CREATE EXTENSION vector/pgcrypto` must precede tables that use them
- Five `created_at` → `created` column reference bugs across migrations and views
- `distillation_worker.py` crash on reconnect (`self.db` → `self.db_pool`)

### Security
- CORS: default origin changed from `*` to explicit localhost allowlist
- Rate limiting enabled by default (`RATE_LIMIT_ENABLED=true`)
- `model_registry` admin routes require root authentication
- Body size limit: 10MB cap on request bodies
- `phi_server.py` CORS narrowed from wildcard

## [2.0.0] — 2026-02-18

Initial modular release. Refactored from monolithic `api_server.py` into `api/handlers/` package.
Added asyncpg connection pooling, pgvector semantic search, GRAEAE multi-provider consensus.

## [1.0.0] — 2025-12-01

Initial release. Single-file FastAPI server with PostgreSQL + pgvector.
GRAEAE integration as separate service on port 5001.
