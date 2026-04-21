# Changelog

All notable changes to MNEMOS are documented here.

## [3.0.0] — 2026-04-21

### Added
- **Unified API under `/v1/` namespace** — new primary routes for memories, consultations, providers, sessions. Pre-v3 paths remain functional as deprecated aliases.
- **Consultations domain (`/v1/consultations`)** — multi-LLM reasoning with cited memory artifacts, hash-chained audit log (SHA-256), and `audit/verify` chain-integrity check.
- **Providers domain (`/v1/providers`)** — unified provider catalog, health tracking, task-aware model recommendation (`/recommend`, `/best`).
- **OpenAI-compatible gateway** — `POST /v1/chat/completions`, `GET /v1/models`. Drop-in for OpenAI SDK consumers with automatic provider routing and optional memory injection.
- **Stateful session management (phase0)** — `/v1/sessions/*` endpoints; multi-turn state with memory injection at turn boundaries.
- **DAG memory versioning (phase3)** — content-addressed commits, branches, merge; `/v1/memories/{id}/{log,branch,merge,commits}`.
- **MCP tools for DAG and optimizer (phase6)** — programmatic access to versioning and model optimizer via stdio MCP server.
- **Model optimizer integration (phase5)** — gateway selects models per task-type + budget, feeds quality back into provider scores.
- **Distillation worker lifecycle integration (phase7)** — background worker starts with app lifespan; health tracked in worker status dict.
- **Webhook subscriptions (v3 roadmap)** — `POST/GET/DELETE /v1/webhooks`, delivery log at `/v1/webhooks/{id}/deliveries`. HMAC-SHA256 signatures, 4-retry exponential backoff (1m/5m/30m/2h), durable delivery log replayed on restart via recovery worker.
- **OAuth/OIDC authentication (v3 roadmap)** — browser-based login via Google, GitHub, Azure AD, or any generic OIDC provider (Keycloak, Authentik, Auth0, Okta). `/auth/oauth/*` endpoints for login/callback/logout/me; `/admin/oauth/providers` for admin-side provider management; `/admin/oauth/identities` for inspection. DB-backed sessions stored in `oauth_sessions` (revocable, 30-day default TTL), expired-session GC worker runs hourly. Coexists with API-key Bearer auth — `api/auth.py::get_current_user` checks Bearer first, then `mnemos_session` cookie. User provisioning: reuse existing user on `(provider, external_id)` match, link to existing user on email match, else create a fresh user.
- **Cross-instance memory federation (v3 roadmap)** — pull-based one-way sync between MNEMOS instances. `/v1/federation/peers` admin CRUD, `/v1/federation/peers/{id}/sync` manual trigger, `/v1/federation/peers/{id}/log` per-peer history, `/v1/federation/status` aggregate view, `/v1/federation/feed` serving endpoint (requires `role IN ('federation', 'root')`). Tables: `federation_peers`, `federation_sync_log`; `memories` gains `federation_source` + `federation_remote_updated` columns. Federated memories stored with ids of the form `fed:{peer_name}:{remote_id}` and `owner_id='federation'`, read-only by convention. Background sync worker runs every 60s, pulls from peers whose interval has elapsed. Loop prevention: the feed endpoint excludes memories with `federation_source IS NOT NULL`, so federated memories don't re-propagate.
- **Dual-license LICENSE file** — Apache 2.0 + Commons Clause (Tier 1: free for personal, team, educational, non-profit, internal enterprise) and proprietary commercial tier (Tier 2: SaaS, resale, white-label).
- **Consultation → memory reference table (`consultation_memory_refs`)** — EMIR Article 57 audit support: which consultations cited which memories.
- **Anti-memory-poisoning guide** (`ANTI_MEMORY_POISONING.md`) — DAG versioning rationale and operational procedures for detecting drift.
- **`tools/docling_import.py`** — IBM Docling integration: import PDF / DOCX / HTML / MD / PPTX / TXT as chunked MNEMOS memories.
- **`tools/memory_import.py`** — generic bulk import helper.
- **`CONTRIBUTING.md`, `SECURITY.md`** — contributor + security-disclosure docs.
- **`v3.0.0_RELEASE_SUMMARY.md`, `V3_0_0_IMPLEMENTATION_SUMMARY.md`** — release + implementation documentation.
- **`uv` package manager integration** — faster dependency resolution and builds.

### Changed
- MNEMOS API and GRAEAE consolidated into a single service on port **5002** (was: separate :5000 + :5001).
- `api/handlers/memories.py` emits webhook events on create / update / delete (dispatches tolerate handler failure).
- `api/handlers/consultations.py` emits `consultation.completed` webhook after audit write.
- README rewritten for v3.0.0 surface; "What works now" sections expanded with consultations, providers, OpenAI gateway, sessions; Roadmap relabeled for accuracy.
- Test reshuffle: legacy `test_hooks.py`, `toggle_auth.py`, `verify_v1.sh`, `verify_v2.sh` removed in favor of consolidated `test_integration.py` + `test_unit.py`.

### Removed
- Internal-infrastructure references scrubbed from all public docs.
- Pre-release `MNEMOS_v24_IMPLEMENTATION_NOTES.md` and its in-code reference in `api/handlers/memories.py`.

### Security
- Webhook deliveries are HMAC-signed per-subscription; receivers verify before trusting payloads.
- Webhook revocation is soft-delete — delivery log retained for audit even after subscription revocation.
- OAuth state (PKCE verifier + CSRF nonce) is carried in a Starlette-signed short-lived cookie (`mnemos_oauth_state`, 10-minute TTL, separate from the application session cookie). Secret key via `MNEMOS_SESSION_SECRET` env; auto-generated on startup if unset (warning logged).
- Sessions are DB-backed (not JWT) so individual sessions can be revoked instantly; raw session ids are never logged. Cookie flags: `HttpOnly`, `SameSite=Lax`, `Secure` when served over HTTPS.

---

## [2.3.0] — 2026-04-12

### Added
- Knowledge Graph API (`/triples`, `GET /timeline/{subject}`) — temporal triple store with `valid_from`/`valid_until`
- Journal API (`/journal`) — date-partitioned operational log
- Key-Value State API (`/state/{key}`) — persistent session state store
- Entity tracking API (`/entities`) — people, projects, concepts with bidirectional links
- Model Registry (`/model-registry/`) — live provider model catalog with Arena.ai Elo sync
- LETHE compression (Tier 1 CPU) — unified module with two modes: `token` (~57% reduction at 0.5ms, formerly extractive token filter) and `sentence` (structure-preserving, formerly SENTENCE). Zero external calls.
- DistillationEngine — AUTO strategy selects LETHE token vs sentence mode; falls back to ALETHEIA (GPU) or ExternalInferenceProvider on quality < threshold
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
- Compression pipeline: distillation_worker uses LETHE (CPU) first, then ALETHEIA (GPU) if configured, then ExternalInferenceProvider as last resort
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
