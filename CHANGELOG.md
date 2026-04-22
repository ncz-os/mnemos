# Changelog

All notable changes to MNEMOS are documented here.

## [3.0.0] — 2026-04-21

### Added
- **Unified API under `/v1/` namespace** — primary routes for consultations, providers, memories, versions, sessions. Pre-v3 paths (`/graeae/*`, `/memories/*`, `/model-registry/*`) remain functional as deprecated aliases for backward compatibility.
- **Consultations domain (`/v1/consultations`)** — GRAEAE multi-LLM reasoning with cited memory artifacts, hash-chained audit log (SHA-256), `audit/verify` chain-integrity check. Memory injection tracking per consultation via `consultation_memory_refs` table (EMIR Article 57 audit support).
- **Providers domain (`/v1/providers`)** — unified provider catalog, health tracking, task-aware model recommendation (`/recommend`, `/best`). Model registry with graceful fallback to static provider config when empty.
- **OpenAI-compatible gateway** — `POST /v1/chat/completions`, `GET /v1/models`. Drop-in for OpenAI SDK consumers with automatic provider routing and optional memory injection.
- **Stateful session management (phase0)** — `/sessions/*` endpoints; multi-turn state with memory injection at turn boundaries.
- **DAG memory versioning (phase3)** — content-addressed commits, branches, merge; `/v1/memories/{id}/{log,branch,merge,commits}`.
- **MCP tools for DAG and optimizer (phase6)** — programmatic access to versioning and model optimizer via stdio MCP server.
- **Model optimizer integration (phase5)** — gateway selects models per task-type + budget, feeds quality back into provider scores.
- **Distillation worker lifecycle integration (phase7)** — background worker starts with app lifespan; health tracked in worker status dict.
- **Webhook subscriptions** — `POST/GET/DELETE /v1/webhooks`, delivery log at `/v1/webhooks/{id}/deliveries`. HMAC-SHA256 signatures, 4-retry exponential backoff (1m/5m/30m/2h), durable delivery log replayed on restart via recovery worker.
- **OAuth/OIDC authentication** — browser-based login via Google, GitHub, Azure AD, or any generic OIDC provider (Keycloak, Authentik, Auth0, Okta). `/auth/oauth/*` endpoints for login/callback/logout/me; `/admin/oauth/{providers,identities}` admin surface. DB-backed sessions (revocable, 30-day default TTL), hourly GC worker. Coexists with API-key Bearer auth — `get_current_user` checks Bearer first, then `mnemos_session` cookie. Provisioning: reuse on `(provider, external_id)` match, link to existing user on email match, else mint fresh user.
- **Cross-instance memory federation** — pull-based one-way sync between MNEMOS instances. `/v1/federation/peers` admin CRUD, `/v1/federation/peers/{id}/{sync,log}`, `/v1/federation/status`, `/v1/federation/feed` (requires `role IN ('federation', 'root')`). Tables: `federation_peers`, `federation_sync_log`; `memories` gains `federation_source` + `federation_remote_updated`. Federated memories stored with ids `fed:{peer_name}:{remote_id}`, `owner_id='federation'`, read-only by convention. Background sync every 60s. Loop prevention via `federation_source IS NOT NULL` exclusion.
- **Dual-licensing** — Apache-2.0 for the OSS distribution (`LICENSE`). A separate proprietary commercial license is available by agreement (`LICENSE-PROPRIETARY.md`).
- **Anti-memory-poisoning guide** (`ANTI_MEMORY_POISONING.md`) — DAG versioning rationale and operational procedures for detecting drift.
- **`tools/docling_import.py`** — IBM Docling integration for PDF / DOCX / HTML / MD / PPTX / TXT import (Docling 2.69+ API).
- **`tools/memory_import.py`** — generic bulk import helper.
- **Integrations bundle** (`integrations/`) — drop-in hooks, skills, and MCP configs for Claude Code, OpenClaw, ZeroClaw, and Hermes. Each framework has SKILL.md + MCP config + enforcement snippet; Claude Code also includes idempotent install/uninstall scripts.
- **Free-tier LLM defaults** — Together AI and Groq prioritized; OpenAI, Claude, Perplexity as fallbacks. No paid account required for a working install.
- **Generic GPU provider config** — `GPU_PROVIDER_HOST` + `GPU_PROVIDER_PORT` env vars (optional; system works CPU-only with external LLM providers).
- **DEPLOYMENT.md**, **`.env.example`** — public deployment guide + complete configuration template with sensible defaults.
- **`CONTRIBUTING.md`, `SECURITY.md`** — contributor + security-disclosure docs.
- **`v3.0.0_RELEASE_SUMMARY.md`, `V3_0_0_IMPLEMENTATION_SUMMARY.md`** — release + implementation documentation.
- **`uv` package manager integration** — faster dependency resolution and builds.
- **Python 3.9 compatibility** — `tomllib` fallback to `tomli` for older interpreters.

### Changed
- MNEMOS API and GRAEAE consolidated into a single service on port **5002** (was: separate :5000 + :5001).
- SBOM simplified — FastAPI + asyncpg unified async stack; no Flask / gunicorn / sync-psycopg legacy.
- `memories.py` emits webhook events on create / update / delete (dispatches tolerate handler failure).
- `consultations.py` emits `consultation.completed` webhook after audit write; static `/audit` routes declared before dynamic `/{consultation_id}` to prevent path-param shadowing.
- Compression module renamed surfaces: public docs + config now use LETHE (token + sentence modes) rather than extractive token filter / SENTENCE; code retains the `was extractive token filter` / `was SENTENCE` annotations for traceability.
- README rewritten for v3.0.0 surface; Roadmap restructured; "What works now" expanded with consultations, providers, OpenAI gateway, sessions, webhooks, OAuth, federation.
- Test reshuffle: legacy `test_hooks.py`, `toggle_auth.py`, `verify_v1.sh`, `verify_v2.sh` removed; consolidated `tests/test_unit.py`, `test_integration.py`, `test_v3_integration.py`, plus new `test_webhooks.py` / `test_oauth.py` / `test_federation.py`.
- Minimal setup — 5 required env vars (PG_* + MNEMOS_API_KEY + one LLM provider).
- Conftest rebuilt as in-process harness for deterministic integration tests.

### Fixed
- Audit trail insert path now matches schema (`af249ce`).
- Consultation audit routes no longer shadowed by `/{consultation_id}` (route ordering).
- DAG migration view column mismatches (`v_compression_stats`, `v_unreviewed_compressions`) aligned with actual `compression_quality_log` schema.
- DAG routes now mount under `/v1/memories/...`.
- `memories.metadata`, `memories.subcategory`, `memories.verbatim_content` — added via idempotent `ALTER TABLE` on v2_versioning migration (columns the handler expected but the base migration omitted).
- v2_versioning trigger: `current_setting(...) OR 'main'` (invalid SQL) corrected to `COALESCE(NULLIF(...), 'main')`.
- v3_dag migration: `DROP VIEW` now precedes `ALTER COLUMN` on `compression_quality_log.memory_id`.
- `compression_quality_log.memory_id` type mismatch (UUID → TEXT) reconciled with `memories.id TEXT` in base migration.
- `consultations.py` SQL aliases `created AS created_at` to align with handler response shape.
- `sessions.py`: SELECT DISTINCT + ORDER BY rewritten as GROUP BY + MAX.
- `providers/recommend` falls back to static GRAEAE provider config when `model_registry` is empty (fresh-install case).
- Docling import updated for v2.69+ API changes.
- Python 3.9 `tomllib` → `tomli` fallback.
- Lifecycle log line version string corrected to v3.0.0.

### Backward Compatibility
- All v2.x endpoints (`/graeae/*`, `/memories/*`, `/model-registry/*`) remain unchanged and functional.
- v2.x API coexists with v3 — no breaking changes for existing MNEMOS deployments.

### Removed
- Internal-infrastructure references scrubbed from all public docs and code paths (PYTHIA / CERBERUS / PROTEUS / ARGONAS; hardcoded 192.168.207.x addresses). See `GPU_PROVIDER_HOST` for the generic alternative.
- Pre-release `MNEMOS_v24_IMPLEMENTATION_NOTES.md` and its in-code reference in `api/handlers/memories.py`.

### Security
- Hash-chained audit log for all consultations (tamper-evident reasoning trail).
- Memory injection tracking per consultation (auditable context).
- Webhook deliveries are HMAC-signed per subscription; receivers verify before trusting payloads.
- Webhook revocation is soft-delete — delivery log retained for audit even after subscription revocation.
- OAuth state (PKCE verifier + CSRF nonce) in a Starlette-signed short-lived cookie (`mnemos_oauth_state`, 10-min TTL, separate from the application session cookie). Signing key via `MNEMOS_SESSION_SECRET`; auto-generated on startup if unset.
- Sessions are DB-backed (not JWT) so individual sessions can be revoked instantly; raw session ids are never logged. Cookies use `HttpOnly`, `SameSite=Lax`, `Secure` when served over HTTPS.

## [2.4.0] — 2026-04-19 (intermediate, v3.0.0 baseline)

**Note**: v2.4.0 was the development baseline for v3.0.0 unification. Features from v2.4.0 (OpenAI gateway, sessions, DAG versioning, compression tiers) are all included in v3.0.0 without modification.

- OpenAI-compatible gateway with auto memory injection
- Server-side session management with stateful chat
- Git-like DAG versioning (branch, merge, revert)
- THE MOIRAI compression (LETHE, ALETHEIA, ANAMNESIS)
- Distillation worker for async compression

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
