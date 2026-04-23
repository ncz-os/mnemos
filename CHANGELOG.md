# Changelog

All notable changes to MNEMOS are documented here.

## [3.1.0] — 2026-04-23

Compression platform release. Adds a plugin `CompressionEngine` ABC open
to operator-registered engines, a competitive per-memory contest across
three built-in engines, and a persisted audit log recording every
winner AND loser per contest with its score and disqualification
reason — not just the chosen output. Extends the v3.0 schema with three
new tables (`memory_compression_queue`, `memory_compression_candidates`,
`memory_compressed_variants`) wired through a GPU circuit breaker that
fast-fails when the inference endpoint is unreachable.

Ships the Tier 1 small-fix unblocks already on master since 2026-04-22
under the v3.1 umbrella; Tier 3 tenancy fixes are explicitly deferred
to v3.1.1; APOLLO (the fourth engine, schema-aware dense encoding for
LLM-to-LLM wire use) is staged across v3.2–v3.4 per `ROADMAP.md`.

### Added

- **Plugin `CompressionEngine` ABC** (`compression/base.py`). Open
  interface for first-party and operator-registered engines. Declares
  `id`, `label`, `version`, `gpu_intent` at class level. One async
  method, `compress(CompressionRequest) -> CompressionResult`. Adapted
  from OpenClaw's `CompactionProvider` pattern (Apache-2.0, credited in
  module docstring).

- **Three engines under the ABC**: LETHEEngine (extractive, CPU),
  ALETHEIAEngine (LLM-assisted token importance, GPU), ANAMNESISEngine
  (LLM fact extraction, GPU). All three compose the existing v3.0
  engines; existing sync callers (manager.py, distillation_engine.py)
  continue to work unchanged.

- **Competitive-selection contest** (`compression/contest.py`). The
  distillation worker runs every eligible engine per memory via
  `asyncio.gather`, scores each candidate via a composite function
  (`quality * ratio_term * speed_factor`, with a quality floor that
  disqualifies damaged output), and picks the highest-scoring survivor.
  Scoring profile configurable via `~/.mnemos/compression_scoring.toml`:
  `balanced` | `quality_first` | `speed_first` | `custom`.

- **Persisted contest audit log** (`compression/contest_store.py`).
  `persist_contest()` writes every candidate (winner AND losers)
  into `memory_compression_candidates` and upserts the winner into
  `memory_compressed_variants` in a single transaction. Operators
  get a full record of what was tried, what scored how, and why each
  engine was or wasn't picked.

- **GPU circuit breaker** (`compression/gpu_guard.py`). Per-endpoint
  three-state breaker (CLOSED → OPEN → HALF_OPEN → CLOSED) tracks
  health of each configured `GPU_PROVIDER_HOST`. `gpu_required`
  engines (ALETHEIA, ANAMNESIS) fast-fail with
  `reject_reason='disabled'` when the circuit is open instead of
  piling doomed requests onto a dead endpoint. Process-local
  registry (v3.2 horizontal-scaling work makes it shared-state).

- **Distillation-worker queue drain** (`compression/worker_contest.py`
  + `distillation_worker.py`). `process_contest_queue()` atomically
  dequeues pending rows via `FOR UPDATE SKIP LOCKED`, runs the
  contest, persists the outcome, transitions the queue row
  `pending → running → done/failed` with an honest rejection-reason
  summary on failure. Runs alongside the existing v3.0 direct-memory
  polling loop; failure-isolated so a contest error doesn't stall
  the legacy path.

- **`GET /v1/memories/{id}/compression-manifests`** endpoint
  (`api/handlers/memories.py`). Returns the current winning variant
  and every historical contest grouped by `contest_id`, with
  scoring fields and reject_reason per engine attempt.
  `?include_content=true` returns full compressed content; default
  is a 200-char preview. RLS-gated via the underlying memories
  table.

- **v3.1 schema** (`db/migrations_v3_1_compression.sql`). Three new
  tables wired idempotently: `memory_compression_queue` (write-time
  task queue), `memory_compression_candidates` (full contest log),
  `memory_compressed_variants` (current winner per memory). Dry-run
  validated against real Postgres.

- **Environment flags**:
  - `MNEMOS_CONTEST_ENABLED` (default `true`) — gates the whole v3.1
    path. Operators who want to run v3.0 behavior exclusively can
    flip to `false`.
  - `MNEMOS_ALETHEIA_ENABLED` (default `false`) — see "Changed"
    below.

- **First real benchmark**:
  `docs/benchmarks/compression-2026-04-23.md`. 464 stratified memories
  from PYTHIA MNEMOS (uncompressed only, small/medium/large buckets)
  drained through the contest on a CERBERUS test deployment with
  gemma-4-E4B-it-Q6_K as the judge model. Winner distribution,
  per-category breakdown, ratio histogram, timing histogram per
  engine, outlier cases, and the one real bug the drain surfaced
  and fixed.

- **`ROADMAP.md`**. Committed scope for v3.1 and the v3.2–v3.4
  "Apollo Program" staged rollout. Explicit deferrals with
  rationale.

### Changed

- **ALETHEIA is disabled by default** (`MNEMOS_ALETHEIA_ENABLED=false`).
  The v3.0 engine's index-list scoring prompt ("output comma-separated
  token indices to keep") doesn't survive instruction-tuned chat
  models — tested against Qwen2.5-Coder-7B and gemma-4-E4B-it, both
  return off-spec text the parser can't interpret. Parser falls
  through to first-N truncation with honest `quality_score=0.60`,
  which the balanced profile's 0.70 quality_floor correctly rejects.
  Engine never wins and burns GPU time. Default engine roster is now
  LETHE + ANAMNESIS. Operators with a tuned prompt/model combination
  opt in via the env var. The prompt redesign is v3.x scope.

- **README.md + ROADMAP.md reality-alignment audit**. Stripped APOLLO
  from v3.1 descriptions (moved to v3.2–v3.4). Switched "four engines"
  → "three engines under a plugin ABC". Normalized stale v3.0.0
  language to v3.0 (release line). Removed "on the roadmap" claims
  for integration adapters not actually in the roadmap. Generalized
  specific production-count numbers that would age.

### Fixed

- **Tier 1 unblocks** (already on master as 2026-04-22 commits, now
  under the v3.1 umbrella):
  - MCP stdio server path prefix (`#M31-01`). The published stdio
    MCP server called `/memories*` but the REST router registers
    `/v1/memories*` — nine of fourteen memory tools returned 404
    against a default install.
  - Installer `api_keys` schema alignment (`#M31-04`). Fresh
    auth-enabled installs failed at seed because `installer/db.py`
    wrote columns the current schema no longer has.
  - Admin `create_user` accepts `role='federation'` (`#M31-03`).
    Federation peer onboarding previously required direct SQL writes
    because the admin validator and the v1_multiuser CHECK
    constraint both rejected the role at creation time.

- **`mnemos_version_snapshot()` trigger bytea crash on backslash
  content** (`db/migrations_v3_1_versioning_fix.sql`). The v2
  versioning trigger computed `commit_hash` via direct `text::bytea`
  cast on concatenated memory content. Postgres interprets
  backslash-escape sequences (`\x47`, `\d+`, `\0`, `\n`, `\x1b[...`)
  as bytea escape syntax and rejects the INSERT outright with
  "invalid input syntax for type bytea". Affected any production
  install ingesting memories that contain code, paths, or regex
  patterns — which is most real content. Latent since v2 shipped;
  surfaced by the v3.1 CERBERUS test deployment running real PYTHIA
  memories. Fix replaces `(text)::bytea` with `convert_to(text,
  'UTF8')` which returns raw UTF-8 bytes without trying to parse
  escape sequences. Idempotent migration; `CREATE OR REPLACE
  FUNCTION` replaces the existing definition in place.

- **Composite-zero winner CHECK-constraint violation**
  (`compression/contest.py`). Short memories where every engine
  scored `composite_score=0` (ratio at or below MIN_CHUNK_RATIO
  or >= 1.0) previously "won" the contest with
  `persist_contest`'s NULL coercion violating
  `mcc_winner_has_output`. Surfaced during the 49-memory CERBERUS
  drain. `run_contest` now requires `composite_score > 0` for
  winner eligibility; zero-composite survivors fall through to
  `reject_reason='inferior'`, and the queue row is marked `failed`
  with an honest "no winner" message rather than silently storing a
  degenerate "winner" variant.

- **ALETHEIA parser returns first-N fallback on unparseable model
  responses** (`compression/aletheia.py`). Pre-existing v3.0 bug
  where the importance-score parser returned empty content when
  zero valid indices survived filtering (as opposed to an actual
  exception). Now explicitly raises on empty-indices → existing
  first-N fallback fires. Compress result reports honest
  `quality_score=0.60` and `method='aletheia_parse_fallback'` when
  fallback is used. Surfaced during live-GPU testing against Qwen
  and gemma; the contest correctly filters the degenerate output
  via the ratio_term floor, but the audit log now accurately shows
  WHAT happened rather than reporting "aletheia" with empty content.

- **`ratio_term` floor below MIN_CHUNK_RATIO** (`compression/contest.py`).
  Scoring function returned `1.0 - ratio` for any ratio, which
  rewarded degenerate empty-output engines (ratio=0) with maximum
  score. Now returns 0 for ratios below `MIN_CHUNK_RATIO` (0.15) or
  at/above 1.0 — empty output and non-compression both score zero.
  Surfaced by live-GPU testing of ALETHEIA.

### Deferred

- **Tier 3 tenancy fixes** — v3.1.1 patch series with migration
  guides and per-fix regression coverage. Covers KG `owner_id`
  column + handler enforcement, namespace enforcement on memory
  paths, application-layer owner filter (defense-in-depth beside
  RLS), and registry-backed `/v1/models` (instead of hardcoded list).
- **APOLLO engine + schema-aware dense encoding** — v3.2–v3.4
  Saturn V-staged rollout per `ROADMAP.md`. Design informed by
  InvestorClaw's consultative-LLM pipeline pattern, not by raw
  Apollo-era telemetry specs.
- **Narration endpoint** (`GET /v1/memories/{id}/narrate`) — v3.2,
  APOLLO's companion read path.
- **Hot-path compression-variant reads** (rehydrate / gateway inject
  / session context serving winner variants instead of raw
  `memories.content`) — v3.2 alongside APOLLO.
- **Judge-LLM quality scoring** replacing engine self-reports —
  v3.2 alongside APOLLO. Today's scoring depends on engines'
  self-reported quality; a real judge would likely shift some
  wins between engines.

## [3.0.1] — 2026-04-22

Patch release fixing three credibility-sensitive defects in the initial
public cut of v3.0.0. No feature changes, no schema changes, safe in-place
upgrade.

### Fixed

- **OpenAI gateway: full conversation history reaches the provider**
  (`api/handlers/openai_compat.py`). The `_route_to_provider` helper used
  by `/v1/chat/completions` and `/sessions/*/messages` previously
  collapsed the request to `messages[-1]["content"]`, silently dropping
  the system prompt, injected memory context, and every prior assistant
  turn before the provider call. A new `_flatten_messages_for_prompt`
  helper serializes the full `messages` array with role boundaries so
  multi-turn chat and session history reach the provider intact. Silent
  regression — no error, just degraded responses — fixed.

- **Docker Compose applies all 11 migrations, not 4**
  (`docker-compose.yml`). The v3.0 Compose file mounted only the first
  four migration files into `docker-entrypoint-initdb.d/`. Fresh Compose
  installs booted without sessions, DAG, consultations audit, webhooks,
  OAuth, federation, or ownership tables — every v3 route 500'd on first
  use. All eleven migration files are now mounted in the canonical
  order (matches `installer/db.py::run_migrations()`).

- **Session compression metrics tightened** (`api/handlers/sessions.py`).
  The session-injection path currently ships raw-slice truncation, not
  real compression; the `compression_ratio` columns on
  `session_messages` and `session_memory_injections` now write `NULL`
  rather than placeholder constants. Real ratios are populated in v3.1
  once compression is wired into the session path.

### Also

- Internal renaming: compression mode aliases in `compression/lethe.py`
  and `compression/distillation_engine.py` updated to accurate
  descriptors. No behavior change; source-tree honesty pass.

## [3.0.0] — 2026-04-22

First public release.

MNEMOS has been in daily production use since December 2025, backing multiple
active agentic systems. This is the first cut shipped as open source — a
single unified FastAPI service covering memory, multi-LLM consensus
reasoning, DAG versioning, provider routing, and an OpenAI-compatible
gateway.

### What's in

**Unified API under `/v1/*`**

- **Consultations** (`/v1/consultations`) — GRAEAE multi-LLM consensus
  reasoning with cited memory artifacts and a tamper-evident SHA-256
  hash-chained audit log. Memory-injection tracking per consultation via
  `consultation_memory_refs`. Atomic persistence: consultation row, audit
  entry, and memory refs commit in a single transaction; audit-write
  failure aborts the consultation.
- **Memories** (`/v1/memories`) — CRUD, semantic + FTS search, DAG
  versioning (git-like: `log`, `branch`, `merge`, `revert`), three-tier
  compression pipeline (LETHE CPU / ALETHEIA GPU / ANAMNESIS archival)
  with a written quality manifest on every transformation.
- **Providers** (`/v1/providers`) — unified catalog, health tracking,
  task-aware model recommendation. Falls back to static config when the
  model-registry table is empty (fresh-install friendly).
- **OpenAI-compatible gateway** (`POST /v1/chat/completions`,
  `GET /v1/models`) — drop-in for OpenAI SDK consumers with automatic
  provider routing and optional memory injection.
- **Sessions** (`/sessions`) — stateful multi-turn chat with memory
  injection at turn boundaries.
- **Webhooks** (`/v1/webhooks`) — HMAC-SHA256-signed outbound event
  delivery. SSRF-hardened URL validation at both subscription and
  dispatch time (loopback, private, link-local, cloud-metadata endpoints
  all rejected). Durable retry log replayed on restart (1m / 5m / 30m /
  2h backoff; `abandoned` after four attempts).
- **OAuth / OIDC** (`/auth/oauth/*`) — browser login via Google, GitHub,
  Azure AD, or any generic OIDC provider (Keycloak, Authentik, Auth0,
  Okta). DB-backed sessions, hourly GC, `email_verified` required for
  cross-provider account linking. Coexists with API-key Bearer auth.
- **Federation** (`/v1/federation/*`) — pull-based cross-instance memory
  sync. Per-memory opt-in via `permission_mode` (others-read bit).
  Admin-only peer management, `federation`-role `/feed` endpoint,
  loop-prevention via `federation_source`.
- **Knowledge graph** (`/kg/triples`, `/kg/timeline/{subject}`) —
  temporal triple store with `valid_from` / `valid_until` windows.
- **Per-owner multi-tenant isolation** on memories, consultations,
  state, journal, entities. Root-only override for cross-owner
  operations.

**Infrastructure and tooling**

- Python 3.11+, PostgreSQL + pgvector, asyncpg.
- Body size limit enforced as streaming ASGI middleware (chunked-upload
  safe, default 5 MB, `MAX_BODY_BYTES` configurable).
- Rate limiter keyed on socket peer by default; honours `X-Forwarded-For`
  when `RATE_LIMIT_TRUST_PROXY=true`.
- Distillation worker supervised with exponential-backoff restart
  (cap 5 min).
- TLS enforced on federation peer URLs (opt-out via
  `FEDERATION_ALLOW_INSECURE`).
- CI runs under `uv` with a reproducible `.venv`. Ruff-clean tree.
- Installer CLI (`mnemos-install`) shipped as a `[project.scripts]`
  entry point so `pip install mnemos-os` gives you a working install
  binary without needing the source tree.
- All eleven SQL migrations ride inside the wheel as `db/*.sql`
  package data — accessible at runtime via
  `importlib.resources.files("db")`.
- Dual-licensed: Apache-2.0 for the OSS distribution; a separate
  proprietary commercial license is available by agreement.

**Integrations**

- Drop-in hooks, skills, and MCP configs for Claude Code, OpenClaw,
  ZeroClaw, and Hermes. Each framework gets SKILL.md + MCP config +
  enforcement snippet; Claude Code also includes idempotent install /
  uninstall scripts.
- IBM Docling integration for PDF / DOCX / HTML / MD / PPTX / TXT
  import (`tools/docling_import.py`).
- Generic bulk-import helper (`tools/memory_import.py`).
- MCP tools for DAG versioning and the model optimizer (stdio MCP server).

### Security posture

- Tamper-evident SHA-256 hash chain on every consultation.
  `audit/verify` walks the chain from genesis; rate-limited 5/min,
  `audit` list 30/min.
- Consultation row + audit entry + memory refs commit atomically.
  Audit-write failure aborts the consultation with 503.
- Webhook URL validation blocks loopback, RFC1918 private, link-local,
  multicast, reserved, cloud-metadata endpoints (Google / AWS / Azure /
  Alibaba / Tencent / IPv6 variants). Async DNS resolution so a slow
  resolver can't freeze the ASGI worker.
- Webhook payloads HMAC-SHA256 signed per subscription. Delivery log
  retained after soft-delete for audit.
- OAuth cookie `Secure` flag honours `X-Forwarded-Proto` behind a
  trusted proxy (`OAUTH_TRUST_PROXY=true`). Sessions DB-backed,
  revocable.
- OAuth account-linking requires `email_verified=true` from the
  provider (strict — the string `"false"` does not count as verified).
- DAG merge wrapped in a single transaction held under
  `pg_advisory_xact_lock` keyed on `(memory_id, target_branch)` so
  concurrent merges cannot produce orphan commits or duplicate version
  numbers.
- Memory `owner_id` / `namespace` override on create requires
  `role='root'`.
- Explicit `owner_id = $2` filter on memory PATCH / DELETE as
  defense-in-depth beyond RLS.

### License

Apache-2.0 for the OSS distribution (`LICENSE`). A separate proprietary
commercial license is available by agreement (`LICENSE-PROPRIETARY.md`).
