# Changelog

All notable changes to MNEMOS are documented here.

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
