<p align="center">
  <img src="docs/images/logo.png" alt="MNEMOS" width="220" />
</p>

# MNEMOS + GRAEAE

**A memory operating system for serious agentic work — not a memory-storage provider.**
**In daily production use since December 2025.**

The distinction matters. A *storage provider* gives you a place to put bytes. An *operating system* gives you named subsystems — a scheduler, a compressor, a process manager, a security layer, an auditor — that manage the full lifecycle of a resource the application no longer has to babysit.

MNEMOS is the second thing. It is the operating system for agent memory: a runtime composed of named subsystems that cooperate to manage the full lifecycle of memory across multiple agents, providers, and time horizons — **write, embed, compress, version, reason-over, audit, federate, archive** — each one a first-class citizen of the runtime, not a feature bolted onto a vector store.

- **MNEMOS** is the memory kernel and the overall system name. Storage, versioning, tiered compression, and lifecycle sit here.
- **GRAEAE** is the reasoning bus — a multi-provider consensus layer that scores and selects across live LLM backends with a cryptographic audit chain on every decision.
- **THE MOIRAI** (**LETHE**, **ALETHEIA**, **ANAMNESIS**) is the compression subsystem — three tiers that decide what part of a memory's thread survives, with a written receipt on every transformation.
- A **self-maintaining model registry** keeps itself current from provider APIs and Arena.ai Elo rankings, so the kernel always knows what models exist, what they cost, and how good they currently are.
- **Federation**, **webhooks**, **OAuth**, **RLS**, **DAG versioning**, and the **/v1/** REST surface are services built on top of that kernel, not retrofits onto a library.

This is not vocabulary borrowed from a dictionary of compelling words. Each of those names is a subsystem with an actual code path, an actual SQL table, an actual lifecycle worker, and an actual failure mode. Read the source tree; it's laid out that way.

You can treat MNEMOS like a memory storage provider if you want — `POST /v1/memories`, `GET /v1/memories/{id}`, you're done. The system will happily oblige. But the reason it holds up in production is that everything underneath that surface is operating-system-shaped: write-ahead transactions on the audit chain, supervised workers for distillation, per-provider circuit breakers, hash-chained reasoning logs, named compression tiers with quality manifests, advisory-locked DAG merges, SSRF-hardened outbound webhooks, a dynamically-weighted model registry that updates itself. It is infrastructure built to operate, not a demo feature built to demo.

**What it is, concretely:**

- A FastAPI service (port 5002), PostgreSQL + pgvector backed. Python 3.11+. Apache-2.0.
- A single `/v1/*` REST surface covering memories, consultations, providers, sessions, webhooks, federation, and an OpenAI-compatible chat-completions gateway.
- A multi-LLM consensus reasoning layer (GRAEAE) that distributes one prompt across multiple providers, scores the responses, and writes a tamper-evident SHA-256 hash-chained audit entry — every time.
- Git-like DAG versioning on memory: `log`, `branch`, `merge`, `revert`. Every mutation snapshots.
- Three-tier compression pipeline (LETHE CPU / ALETHEIA GPU / ANAMNESIS archival) with a written quality manifest on every transformation.
- Per-owner multi-tenant isolation, Bearer API keys + OAuth/OIDC session cookies, SSRF-hardened webhooks, cross-instance federation with per-memory opt-in.
- Runs alongside your applications the way Redis, PostgreSQL, or a message bus would. Deploy once, every agent in your stack shares the same memory substrate.

MNEMOS runs as a network service — you deploy it once, alongside PostgreSQL and Redis, and every agent in your stack shares the same memory kernel over REST. It is not a desktop library, not an in-process helper, not a framework you import. Different form factor, different user, and specifically not a replacement for projects like MemPalace that serve the desktop / single-user case well.

---

## Why this exists

MNEMOS was built out of a very practical frustration: serious agentic systems keep losing context at exactly the moment reliability starts to matter.

In most AI tooling, memory is still treated like a convenience feature. A session ends, context evaporates, and the next run has to reconstruct the same decisions, assumptions, architecture tradeoffs, and operating knowledge from scratch. That may be tolerable for hobby projects. It is not good enough for professional users building production systems.

The first version of the problem looked simple. Keep a large context file, inject it into the prompt, and move on. That works until the context becomes expensive, stale, opaque, and impossible to selectively trust. When you compress it, you no longer know exactly what was removed. When multiple agents need it, the whole approach collapses into duplication and drift.

The second version of the problem was operational. Real agentic development means multiple models, multiple providers, failure modes, cost pressure, and different classes of tasks. Memory that cannot survive provider failure, cannot be shared across agents, or cannot explain its own transformations is not really infrastructure.

MNEMOS was built to solve those problems in a way that reflects real platform experience: provenance matters, compression should be inspectable, shared systems need access controls, and memory should behave like a service you can operate, not a feature you hope keeps working.

Its design is informed by years of enterprise platform work, large-vendor systems thinking, open-source infrastructure experience, and current work in the AI industry, without assuming that professional users want marketing language where they really need operational clarity.

**MNEMOS has been in daily production use since December 2025**, backing multiple active agentic systems simultaneously. By April 2026 the running install had stored **6,793 memories** and performed **3,077 compressions**, each with a written quality manifest. The v3.0.0 release unifies that production codebase into the single-service FastAPI shape shipped here.

For the longer story — the original catalyzing moment, the architectural decisions (and mistakes) that took MNEMOS from a single-file prototype to a unified runtime, and the scrubs, refactors, and release-gate audits that landed the public cut — see [`EVOLUTION.md`](./EVOLUTION.md). Written for future contributors as much as for future readers who want to know what they're inheriting.

---

## Who this is for

MNEMOS is built for the teams and operators who have already outgrown the prototype memory layer.

**You probably want MNEMOS if:**

- You run multiple agents, or multiple LLM providers, and they need to share a consistent memory pool that survives process restarts and provider outages.
- Your agents produce outputs someone downstream has to trust — an auditor, a regulator, a customer, a compliance team, yourself in six months.
- You care whether your memory layer can corrupt, silently swallow writes, or quietly truncate things you wanted to keep.
- You need real auth (API keys *and* OAuth/OIDC) and real multi-tenant isolation, not a bearer-token sticker over a single-user SQLite file.
- You have regulatory pressure around reasoning traceability — EMIR Article 57, SOC-2 evidence, GDPR right-to-explanation, or internal model-governance review boards.
- You need a memory substrate that survives schema migrations, provider circuit-breakers, and federation failures without hand-holding.

**Who this is actually serving, concretely:**

- **Agentic-tooling teams** running multi-agent stacks (crews, swarms, orchestrators) that keep losing shared context at the process boundary.
- **Platform teams inside larger orgs** wiring LLM routing + memory into an internal developer platform and needing a substrate they can operate, not babysit.
- **Regulated-industry AI teams** (finance, healthcare, legal, public sector) that need a cryptographic audit trail on every reasoning step and cannot ship without one.
- **Research labs** exploring consensus-reasoning, long-horizon agent memory, and memory-poisoning defenses — MNEMOS ships DAG versioning and an anti-poisoning guide precisely because those problems are real.
- **Founders** who've already hit the ceiling of in-process memory libraries and need something that survives process restarts, schema changes, and multi-agent concurrency.
- **The 56-year-old former IBM / Microsoft veteran** who has been thoroughly indoctrinated into architectural thinking and mission-critical design, and physically cringes at a memory layer that doesn't have the "-isms" and "-itabilities" thought through — atomicity, idempotency, referential integrity, ACIDism on the write path; durability, recoverability, observability, auditability, testability on the operate path; the things your old DBA would have red-pen'd in a review twenty years ago and your old SRE would red-pen now. This is for you. We know.

**You probably don't need MNEMOS if:**

- You are building a single-user chatbot for personal note-taking and raw similarity search over ChromaDB is fine.
- You only need short-term conversation history within a single session and your SDK already handles that.
- You don't care whether compressed context is faithful to the original — the "toy" solutions are honest about not providing that guarantee.
- The phrases *audit trail*, *tamper evidence*, *multi-tenant isolation*, and *compression manifest* don't mean anything to your use case and never will.

If you're in the first list, MNEMOS is designed specifically for you. If you're in the second list, something lighter will serve you better — use Mem0, Zep, or in-process summary buffers. They exist because those use cases are real. MNEMOS is the answer to a different question.

---

## Why use this

The field of agent memory systems is crowded and getting more so. Here is the honest case for MNEMOS.

**Most memory systems answer one question badly:** "What did this agent say before?"

That is conversation history. It is useful, but it is not memory infrastructure. Conversation history dies when the session ends, scales to one agent, and tells you nothing about whether the information it contains is still accurate, still complete, or safe to rely on.

MNEMOS answers a different set of questions:

- *When I compressed that memory to fit in a context window, what did I throw away — and was it safe to throw away?*
- *If three of my LLM providers go down, does my reasoning layer fail or degrade gracefully?*
- *Can multiple agents share one memory pool without rebuilding context from scratch each session?*

If none of those questions matter for your use case, a simpler tool is probably the right choice. If they do matter, read on.

### The specific gaps in the alternatives

| System | What it does | What it cannot tell you |
|--------|-------------|------------------------|
| **MemGPT / Letta** | Hierarchical paging within a single agent session | What was lost in compression; what happens when the LLM provider fails |
| **Mem0** | Store and retrieve memories via API | Compression quality; reasoning consensus |
| **Zep** | Conversation history + entity extraction | Compression manifests; multi-provider reasoning |
| **LangChain / LlamaIndex memory** | In-process buffer or summary | Anything after the process exits |
| **MemPalace** | Desktop-library long-horizon memory with spatial retrieval and AAAK compression; single-user, in-process | Multi-process deployment; multi-user isolation; network-service semantics |
| **CrewAI / AutoGen memory** | Per-crew or per-agent embedded memory | Cross-session persistence; compression quality |

### What MNEMOS does that none of them do

**Quality contracts on compression.** Every time MNEMOS compresses a memory — on write, on rehydration, or before a GRAEAE consultation — it produces a manifest: what was removed, what was preserved, the quality rating, and which use cases the compressed version is and is not safe for. No other memory system treats compression as something that requires a receipt.

**A reasoning layer that degrades gracefully.** GRAEAE distributes queries across multiple LLM providers simultaneously, scores responses on relevance, coherence, completeness, and toxicity, and returns the best result. Per-provider circuit breakers prevent a failing provider from degrading the pool. A semantic cache means identical questions skip inference entirely. This is not a load balancer — it is a quality-gated reasoning bus.

**A knowledge graph alongside free-text memory.** MNEMOS stores structured triples (subject → predicate → object) with temporal validity windows alongside unstructured memories, and exposes a timeline API per subject. Most memory systems are text-only.

---

### MemPalace and MNEMOS: different problems, not competitors

MemPalace, created by Mila Jovanovic, has pushed long-horizon agent memory into the ecosystem in a way few other projects have. The LongMemEval benchmark attention, the AAAK abbreviation research, and the Palace spatial-memory metaphor are real contributions to a problem — keeping agent memory useful across long time horizons without context explosion — that is genuinely unsolved and genuinely hard. It's work worth taking seriously, and MNEMOS has been influenced by several of its ideas.

In particular, MNEMOS shares MemPalace's bets that:

- Memory deserves first-class treatment as a data structure, not as a side-effect of conversation history.
- Compression is a design axis, not an afterthought: if you keep everything raw you lose the context-window fight, and if you compress naively you lose fidelity.
- Long-horizon memory needs structure. Whether you call it a "palace", a DAG, or a temporal knowledge graph, the point is that flat vector similarity runs out of answers fast.

**MNEMOS is not trying to replace MemPalace.** The two projects are solving adjacent problems with different shapes, for different users:

| | MemPalace | MNEMOS |
|---|---|---|
| **Form factor** | Desktop library, embedded in-process | Network service (FastAPI on port 5002), runs as a daemon |
| **Deployment** | `pip install`, runs inside your agent | Deployed alongside your stack the way you'd run PostgreSQL or Redis; many agents and processes connect over REST |
| **Storage** | ChromaDB (SQLite-backed vector store) | PostgreSQL + pgvector with ACID transactions |
| **Primary user** | Individual developer on a single machine | Teams / platforms operating shared infrastructure |
| **Concurrency model** | Single-process, single-user | Multi-tenant with per-owner isolation, multi-process clients |
| **Audit surface** | Local logs | SHA-256 hash-chained audit chain, tamper-evident and externally reviewable |
| **Reasoning** | Storage + retrieval | GRAEAE multi-LLM consensus with quality scoring and provider failover |

If you are one developer building a personal agent that runs on your laptop and you want it to work offline with no infrastructure overhead, MemPalace is designed exactly for that and is a legitimate, well-constructed choice.

If you are a team or a platform deploying shared agent memory that multiple processes need to access concurrently, with an audit trail that stands up to external review, a DB backend that survives crashes and schema migrations, and a reasoning layer you can point a regulator or auditor at, MNEMOS is designed for that.

The shared premise — that agent memory deserves first-class treatment — is the same. The deployment target is not. Please don't read this section as a takedown; it's a map.


## What works now

This is the current state of v3.0.0. Features described here are implemented and running in production. Features listed in the Roadmap section that are "scheduled for v3.0.0" are under active development for this release.

The API surface is namespaced under `/v1/*`.

### Memory API (v1)

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/memories` | Store a memory with category, subcategory, content, and optional provenance |
| `GET /v1/memories` | List memories, filterable by category and subcategory |
| `GET /v1/memories/{id}` | Retrieve a single memory |
| `POST /v1/memories/search` | Full-text or semantic search with category/score filters |
| `POST /v1/memories/bulk` | Bulk create memories |
| `PATCH /v1/memories/{id}` | Update memory content or metadata |
| `DELETE /v1/memories/{id}` | Delete a memory |
| `POST /v1/memories/rehydrate` | Token-budgeted compressed context load for prompt injection |
| `POST /ingest/session` | Ingest a session transcript |
| `GET /v1/memories/{id}/log` | DAG commit history for a memory |
| `POST /v1/memories/{id}/branch` | Create a branch from a specific commit |
| `POST /v1/memories/{id}/merge` | Merge a branch back to main |
| `GET /v1/memories/{id}/versions` | Version history |
| `GET /health` | Health check (not namespaced) |
| `GET /stats` | Memory counts by category, compression statistics |

### Multi-user and provenance (v1, shipped)

Each memory carries full ownership and LLM provenance:

- `owner_id` — which user owns this memory
- `group_id` — optional group for shared access
- `namespace` — logical partition (e.g. `myapp/analyst`)
- `permission_mode` — UNIX-style octal (600 = owner only, 640 = group readable, 644 = world readable)
- `source_model` — the LLM model that produced this memory
- `source_provider` — the provider (openai, groq, ollama, etc.)
- `source_session` — session ID at time of creation
- `source_agent` — agent name or identifier

**Row Level Security** is defined in PostgreSQL but inactive for personal installs. Team/enterprise installs activate it via `install.py`, which enforces per-row access at the database layer, not application middleware.

**Deployment profiles** — selected at install time via `python install.py`:

| Profile | Auth | RLS | Use case |
|---------|------|-----|---------|
| Personal | off | off | Single developer, localhost |
| Team | API key | on | 2–20 users, shared PostgreSQL |
| Enterprise | API key | on | 20+ users, full namespace isolation |

### Admin API (v1)

| Endpoint | What it does |
|----------|-------------|
| `POST /admin/users` | Create a user |
| `GET /admin/users` | List all users |
| `POST /admin/users/{id}/apikeys` | Generate an API key (raw key returned once) |
| `GET /admin/users/{id}/apikeys` | List API keys for a user |
| `DELETE /admin/apikeys/{id}` | Revoke an API key (soft-delete) |

All admin endpoints require root role. On personal installs (no auth), they are accessible without a key.

### Knowledge graph

| Endpoint | What it does |
|----------|-------------|
| `POST /kg/triples` | Create a subject → predicate → object triple |
| `GET /kg/triples` | List triples with filters |
| `GET /kg/timeline/{subject}` | All triples for a subject in temporal order |
| `PATCH /kg/triples/{id}` | Update a triple |
| `DELETE /kg/triples/{id}` | Delete a triple |

### Consultations — reasoning domain (v3, shipped)

Multi-LLM consensus reasoning with cited memory artifacts and cryptographic audit chain.

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/consultations` | Create a consultation (prompt + task_type) |
| `GET /v1/consultations/{id}` | Retrieve a consultation record |
| `GET /v1/consultations/{id}/artifacts` | Cited memories used to answer |
| `GET /v1/consultations/audit` | Hash-chained audit log |
| `GET /v1/consultations/audit/verify` | Verify audit chain integrity |

### Providers — model routing domain (v3, shipped)

Unified provider catalog with health tracking and task-aware recommendation.

| Endpoint | What it does |
|----------|-------------|
| `GET /v1/providers` | List all configured providers with metadata |
| `GET /v1/providers/health` | Per-provider availability + circuit-breaker state |
| `GET /v1/providers/recommend` | Recommend a model for a task-type + budget |

### OpenAI-compatible gateway (v3, shipped)

Drop-in replacement for the OpenAI Chat Completions API — so any SDK that speaks OpenAI can speak to MNEMOS.

| Endpoint | What it does |
|----------|-------------|
| `GET /v1/models` | List available models across all configured providers |
| `GET /v1/models/{model_id}` | Model details |
| `POST /v1/chat/completions` | Chat completion; routes to the appropriate provider; optional memory injection |

### Stateful sessions (v3, shipped)

Multi-turn conversation state with memory injection at turn boundaries. Sessions carry accumulated context across requests.

| Endpoint | What it does |
|----------|-------------|
| `POST /sessions` | Start a new session |
| `GET /sessions/{id}` | Retrieve session state |
| `POST /sessions/{id}/messages` | Post a turn; memory injection at turn boundary |
| `GET /sessions/{id}/history` | Full message history |
| `DELETE /sessions/{id}` | End a session |

### Webhooks (v3, shipped)

Outbound notifications when events happen. Receivers verify an HMAC-SHA256 signature to trust the payload.

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/webhooks` | Subscribe; secret returned once |
| `GET /v1/webhooks` | List the caller's subscriptions |
| `GET /v1/webhooks/{id}` | Retrieve a subscription |
| `DELETE /v1/webhooks/{id}` | Revoke (soft-delete) |
| `GET /v1/webhooks/{id}/deliveries` | Recent delivery attempts |

Events: `memory.created`, `memory.updated`, `memory.deleted`, `consultation.completed`. Delivery is durable: every attempt is logged to `webhook_deliveries`, retried 4 times with exponential backoff (1m / 5m / 30m / 2h), and replayed from disk on restart by the recovery worker.

Signature header: `X-MNEMOS-Signature: sha256=<hex>`. Verify with `hmac.new(secret, body, sha256).hexdigest()`.

### OAuth / OIDC authentication (v3, shipped)

Browser-based login via external identity providers. Coexists with API-key auth — the same user can have both a key and an OIDC identity.

| Endpoint | What it does |
|----------|-------------|
| `GET /auth/oauth/providers` | List enabled providers (public, no secrets) |
| `GET /auth/oauth/{provider}/login` | Start authorization-code + PKCE flow |
| `GET /auth/oauth/{provider}/callback` | Handle provider redirect; sets `mnemos_session` cookie |
| `POST /auth/oauth/logout` | Revoke session (optionally `?all_devices=true`) |
| `GET /auth/oauth/me` | Who am I (works with either auth method) |

Admin side (`/admin/oauth/*` — root only):

| Endpoint | What it does |
|----------|-------------|
| `POST /admin/oauth/providers` | Register a provider (Google, GitHub, Azure AD, or generic OIDC) |
| `GET /admin/oauth/providers` | List configured providers (client_secret redacted) |
| `PATCH /admin/oauth/providers/{name}` | Update provider config |
| `DELETE /admin/oauth/providers/{name}` | Remove a provider |
| `GET /admin/oauth/identities` | List all OAuth identities (optionally filter by user) |

Sessions are DB-backed, revocable, and expire after 30 days by default. User provisioning: same external-id reuses the user; matching email links to an existing user; otherwise a fresh user is created.

### Federation — cross-instance memory sync (v3, shipped)

Pull-based one-way federation between MNEMOS instances. Remote peer exposes `/v1/federation/feed`; local instance pulls on a configurable interval, storing remote memories with ids of the form `fed:{peer_name}:{remote_id}` and `federation_source = peer_name`. Federated memories are read-only by application convention.

| Endpoint | What it does |
|----------|-------------|
| `POST /v1/federation/peers` | Register a remote peer (root only) |
| `GET /v1/federation/peers` | List registered peers |
| `GET /v1/federation/peers/{id}` | Peer detail |
| `PATCH /v1/federation/peers/{id}` | Update (enable/disable, filters, interval) |
| `DELETE /v1/federation/peers/{id}` | Unregister |
| `POST /v1/federation/peers/{id}/sync` | Manual sync trigger (blocks on completion) |
| `GET /v1/federation/peers/{id}/log` | Sync history for a peer |
| `GET /v1/federation/status` | Aggregate status across all peers |
| `GET /v1/federation/feed` | Serve memories to remote peers (role=`federation` or `root`) |

**Trust model:** mutual — each side registers the other. Side A issues Side B a Bearer token by creating a MNEMOS user with `role='federation'` and minting an API key via the admin API. Side B stores that token in its own `federation_peers.auth_token`. Side A's feed endpoint validates the token and `role IN ('federation', 'root')`.

**Dedup:** re-pulls are safe. Local id `fed:{peer}:{remote_id}` is stable; only rows with a newer `federation_remote_updated` overwrite existing ones.

**Filters:** `namespace_filter` and `category_filter` (both arrays) restrict what gets pulled from a peer; NULL = pull everything the peer will serve.

**Loop prevention:** the feed endpoint excludes memories where `federation_source IS NOT NULL`, so federated memories don't propagate hop-by-hop through a chain of peers.

### GRAEAE engine internals (all operational)

The reasoning engine behind `/v1/consultations` provides:

- **Circuit breaker** — per-provider CLOSED/OPEN/HALF_OPEN state machine, 5-minute cooldown
- **Consensus response cache** — per-process LRU keyed on `sha256(task_type + normalized_prompt)`, 1-hour TTL, 500-entry cap. Exact-match dedup, not embedding similarity (embedding round-trip would negate the win for the less-common near-duplicate case).
- **Quality scorer** — success / failure / latency tracking per provider, combined with Arena.ai Elo weights from the model registry (see next section) for dynamic consensus weighting
- **Rate limiter** — single-level request rate limit with graceful backoff
- **Audit chain** — SHA-256 hash-chained prompt/response log for compliance

### Model registry and dynamic provider weighting (self-maintaining)

Most multi-LLM routers hardcode a provider list. MNEMOS ships a **self-populating PostgreSQL-backed model registry** that keeps itself current.

**What's in the registry.** Every known model from every configured provider — OpenAI, Groq, xAI, Together, Nvidia, Gemini, Anthropic — with per-model metadata: provider + model_id, display name, family (grok-4, gpt-5, gemini-3, …), capabilities (`chat`, `vision`, `code`, `reasoning`, `web_search`), context window, max output tokens, input / output / cache pricing (USD per million tokens), availability, deprecation flag, **Arena.ai Elo score**, **Arena.ai rank**, and the normalized `graeae_weight` (0.50–1.00) actually used by the consensus scorer.

**How it populates.**

- **Daily provider sync** — `graeae.provider_sync` hits each provider's `/v1/models` endpoint (Gemini uses `/v1beta/models`; Anthropic uses a static list because Anthropic does not expose a public `/models` surface) and upserts into `model_registry`. New models appear automatically; deprecated ones get flagged. No manual curation required.
- **Quarterly Elo sync** — `scripts/refresh_elo_weights.py` (systemd: `graeae-elo-sync.timer`) fetches the Arena.ai leaderboard, maps ranks back to providers + models, and writes `arena_score`, `arena_rank`, and `graeae_weight` into the registry.
- **Online quality signals** — the GRAEAE engine tracks per-provider success / failure / latency in memory; those signals combine with the registry's Elo-derived `graeae_weight` to pick the winning response on each consultation.

**What it's used for.**

- **`/v1/providers/recommend?task_type=...&budget=...`** — returns the cheapest available model that meets the task's capability + quality floor. Uses `graeae_weight` as the quality signal and `input_cost_per_mtok + output_cost_per_mtok` as the cost signal.
- **GRAEAE consensus scoring** — provider responses are weighted by `graeae_weight` before the consensus pick. A provider that drops on Arena.ai also drops in MNEMOS's internal routing on the next timer tick, without any human touching a config file.
- **OpenAI-compatible gateway model routing** — when a caller passes `model="auto"`, `model="best-coding"`, `model="best-reasoning"`, `model="fastest"`, `model="cheapest"`, the gateway resolves against the registry rather than a hardcoded alias table.

**Fresh-install behavior.** If the registry is empty (first boot, no sync run yet), `/v1/providers/recommend` falls back to the static GRAEAE provider config so new deployments don't 404. The first `provider_sync` run typically populates 30–50 models depending on which provider API keys you've configured.

### What runs under the hood (infrastructure you don't have to think about)

A lot of the v3.0.0 surface is held up by background work that doesn't show up in the route table but does show up in the failure modes it prevents. For anyone who wants to know what's there:

- **Webhook delivery recovery worker** — on startup, walks `webhook_deliveries` and re-drains any rows stuck in `pending` or `retrying` with a `scheduled_at` in the past. Handles the crash-mid-retry case; subscribers get the delivery eventually rather than never.
- **Distillation worker supervision** — the compression worker runs under an exponential-backoff supervisor (1s → 2s → 4s → … capped at 5 min). A crash is logged and retried; the worker does not silently die and leave memories un-compressed for the rest of the process lifetime.
- **OAuth session garbage collector** — hourly sweep of expired and long-revoked sessions. Bounds the `oauth_sessions` table so a long-running install doesn't accumulate dead rows forever.
- **Federation sync worker** — iterates enabled peers on their individual sync intervals, pulls batches, reconciles local + remote timestamps before overwriting, logs per-sync results to `federation_sync_log`.
- **Advisory-lock-serialized audit chain writer** — the hash chain writer takes `pg_advisory_xact_lock` before reading the chain tip, so concurrent consultations cannot compute against the same stale previous hash. Closes a TOCTOU window in tamper-evident logging that most implementations leave open.
- **Advisory-lock-serialized DAG merges** — merges take a per-`(memory_id, target_branch)` advisory lock, so concurrent merges on the same branch cannot produce orphan commits or duplicate version numbers.
- **ASGI body-size middleware** — native ASGI (not `BaseHTTPMiddleware`), so it rejects chunked uploads whose running byte count exceeds `MAX_BODY_BYTES` *as they arrive*, before the full body lands in memory. Content-Length–declared uploads are rejected before the app is even invoked.
- **SSRF-hardened webhook dispatch** — URLs are re-validated at send time (not just at subscription time); DNS resolves asynchronously so a slow resolver can't freeze the event loop; cloud metadata hostnames (AWS IMDS, Google `metadata.google.internal`, Tencent, Alibaba, IPv6 variants) are on a deny list alongside the RFC1918 / loopback / link-local filter.
- **Rate limiter with X-Forwarded-For trust** — default keys on direct socket peer (safe behind no proxy); set `RATE_LIMIT_TRUST_PROXY=true` only when you run behind a proxy you control. Prevents clients from blowing out the global limit via spoofed headers.
- **pgvector query sanitization** — embedding vectors returned by the embedder are `float()`-cast before being stringified into the query. A poisoned embedder cannot inject SQL via a non-numeric vector "component".
- **Full-text search operator filtering** — `/v1/memories/search` uses `plainto_tsquery` rather than `to_tsquery`, so `|`, `&`, `!` and friends get treated as literal text instead of tsquery operators. User input cannot construct adversarial FTS queries.
- **Federation size caps** — an abusive peer cannot fill your disk: pulled content capped at 1 MB per memory, metadata at 64 KB, name fields at 256 chars.
- **Rate-limited audit endpoints** — `/v1/consultations/audit/verify` walks the entire chain from genesis; capped at 5/min so an authenticated caller cannot force O(N) scans on a large log. `/audit` list is capped at 30/min.
- **Quality manifest on every compression** — LETHE, ALETHEIA, and ANAMNESIS each write a receipt: `{what_was_removed, what_was_preserved, quality_rating, risk_factors, safe_for, not_safe_for}`. Compression-as-data, not compression-as-side-effect.

### Referential integrity (the -ism, spelled out)

Every cross-table reference in the schema is a real PostgreSQL foreign key with an explicit `ON DELETE` semantic — not a loose string column you have to trust the application layer to honour. Twenty-two FK edges across the system, and every one carries a deliberate decision about what happens when the thing it points at goes away. The schema has opinions.

Two patterns, picked per edge:

**`ON DELETE CASCADE`** — when lifecycle is genuinely owned:

- `api_keys.user_id → users(id)` — delete a user, their keys go with them.
- `sessions.user_id → users(id)`, `session_messages.session_id → sessions(id)` — close a session, its messages go.
- `user_groups.user_id → users(id)`, `user_groups.group_id → groups(id)` — membership is owned by both endpoints.
- `webhook_subscriptions.owner_id → users(id)`, `webhook_deliveries.subscription_id → webhook_subscriptions(id)` — subscriber deletion collapses the whole delivery subtree (soft-delete via `revoked=true` is the normal path; CASCADE only matters on hard deletes).
- `oauth_identities.user_id → users(id)`, `oauth_identities.provider → oauth_providers(name)`, `oauth_sessions.user_id → users(id)` — OAuth bindings follow their owner.
- `federation_sync_log.peer_id → federation_peers(id)` — unregister a peer, its sync history goes.
- `graeae_audit_log.consultation_id → graeae_consultations(id)`, `consultation_memory_refs.consultation_id → graeae_consultations(id)` — audit rows are owned by the consultation they describe. Chain integrity comes from the SHA-256 hash chain, not from the FK, so deleting a consultation does not break the chain's verifiability.

**`ON DELETE SET NULL`** — when audit history has to *survive* the referenced row's deletion:

- `memory_versions.parent_version_id → memory_versions(id)` — admin-path deletion of a mid-history commit leaves the DAG with a gap rather than cascading through every descendant.
- `memory_branches.head_version_id → memory_versions(id)` — same reasoning; branches get re-pointed, not destroyed.
- `session_memory_injections.memory_id → memories(id)` — if a memory is deleted later, the *record that we once injected it into a session* stays. The audit outlives the artifact.
- `compression_quality_log.memory_id → memories(id)` — the quality manifest survives the thing it was a manifest for. Compliance cares that the transformation happened, not that the output still exists.
- `consultation_memory_refs.memory_id → memories(id)` — a consultation's cited memory may be deleted; the *record of the citation* is an audit artifact and must not vanish.
- `oauth_sessions.identity_id → oauth_identities(id)` — rotating an identity doesn't invalidate a session row that was already in flight.

This is the part most projects that call themselves "memory" skip, because if the whole point is "store a blob, retrieve a blob", the relationships *between* blobs are out of scope. MNEMOS's design asserts the opposite: memories relate to consultations relate to audit entries relate to sessions relate to users, and the system has strong opinions about which of those relationships is load-bearing and which is historical.

The constraints are enforced at the database level. Application bugs cannot violate them. Migration bugs cannot silently create orphan rows. The constraint travels with the row.

### Compression — the MOIRAI tiers

Three-tier compression pipeline, each tier named after a Greek figure of memory.

- **LETHE** (Tier 1, CPU, always on) — fast local compression with two modes: `token` (importance-weighted token filtering, ~0.5ms, ~57% reduction — the algorithm formerly called *extractive token filter*) and `sentence` (structure-preserving extraction, ~2–5ms, ~50% reduction — formerly *SENTENCE*). `auto` mode picks per content shape. Zero external calls.
- **ALETHEIA** (Tier 2, optional GPU) — token-level importance scoring via a local LLM on a configured GPU host (`GPU_PROVIDER_HOST`); ~200-500ms, ~70% reduction. Runs offline via distillation worker; not on the live path. Falls back to LETHE when the GPU host is unreachable.
- **ANAMNESIS** (Tier 3, optional GPU) — atomic-fact extraction for archival memories (>30 days old); semantic-level compression via LLM. Fallback: skip extraction if the GPU host is unreachable (non-critical).
- **ExternalInferenceProvider** — LLM-assisted compression via llama.cpp / Ollama / any OpenAI-compatible endpoint; highest quality; used as fallback when heuristics dip below the quality threshold.
- Quality manifest on every compression: what was removed, what was preserved, risk factors, safe/unsafe use cases
- Original content always retained; compressed and original stored independently
- Configurable quality thresholds per task type (security review: 95%, architecture: 90%, general: 80%)

### Memory tiers (4-tier system)

| Tier | Description | Compression ratio |
|------|-------------|------------------|
| 1 | Recent / active | 20% |
| 2 | Short-term | 35% |
| 3 | Medium-term | 50% |
| 4 | Long-term / archive | task-type dependent |

### Versioning and audit

- Memory version history (`memory_versions` table) — every mutation auto-snapshots previous state
- Diff and revert API: `GET /v1/memories/{id}/versions`, `GET /v1/memories/{id}/versions/{n}`, `GET /v1/memories/{id}/diff`, `POST /v1/memories/{id}/revert/{n}`
- DAG (git-like) versioning: `GET /v1/memories/{id}/log`, `POST /v1/memories/{id}/branch`, `POST /v1/memories/{id}/merge`, `GET /v1/memories/{id}/commits/{commit}`
- SHA-256 hash-chained audit log for consultations: `GET /v1/consultations/audit`, `GET /v1/consultations/audit/verify`

---

## Roadmap

### Scheduled for v3.0.0 (active development)

Committed to ship with this release:

- ✅ **Webhook subscriptions** — outbound notifications on memory write, consultation completion. HMAC-signed delivery, retry with exponential backoff. **Shipped.**
- ✅ **OAuth/OIDC authentication** — browser-based login via Google, GitHub, Azure AD, or custom OIDC providers. Coexists with existing API-key auth. **Shipped.**
- ✅ **Cross-instance memory federation** — pull-based peer sync with Bearer-authenticated peers. Federated memories stored locally with `federation_source` metadata, `fed:{peer}:{remote_id}` id prefix, and a background worker that respects per-peer sync intervals. **Shipped.**

### Beyond v3.0.0

Future work — not yet scoped:

- Distributed consensus for multi-writer federation
- Plugin model for external compression / ranking algorithms
- Server-push streaming API for long-lived subscriptions

---

## Architecture

```
Agents (any language, any framework)
        │
        │  REST API (port 5002)
        ▼
┌─────────────────────────────────┐
│           MNEMOS API            │
│  auth · namespaces · search     │
│  compression · ingest · admin   │
│  knowledge graph · rehydrate    │
└──────────────┬──────────────────┘
               │
    ┌──────────┴──────────┐
    │                     │
    ▼                     ▼
PostgreSQL           GRAEAE (embedded)
memories             multi-LLM consensus
users / groups       circuit breaker
api_keys             semantic cache
compression_log      quality scorer
knowledge_graph
```

---

## Quick start

### Personal install (Docker Compose)

```bash
git clone <your-repo-url>
cd mnemos
docker compose up
# MNEMOS: http://localhost:5002
# GRAEAE is embedded in the MNEMOS API (port 5002) — no separate server
```

### Manual install

```bash
git clone <your-repo-url>
cd mnemos
pip install -r requirements.txt
python install.py
# Prompts for: deployment profile, database connection, provider API keys
# Writes config.toml, runs migrations, creates root API key (team/enterprise)
```

### Manual database setup (if not using install.py)

```bash
psql -U postgres -c "CREATE USER mnemos WITH PASSWORD 'yourpassword';"
psql -U postgres -c "CREATE DATABASE mnemos OWNER mnemos;"
psql -U mnemos -d mnemos -f db/migrations.sql
psql -U mnemos -d mnemos -f db/migrations_v1_multiuser.sql
psql -U mnemos -d mnemos -f db/migrations_v3_graeae_unified.sql
psql -U mnemos -d mnemos -f db/migrations_v3_webhooks.sql
psql -U mnemos -d mnemos -f db/migrations_v3_oauth.sql
psql -U mnemos -d mnemos -f db/migrations_v3_federation.sql
```

### Start

```bash
python api_server.py        # MNEMOS on port 5002
# GRAEAE is embedded in the MNEMOS API (port 5002) — no separate server needed

curl http://localhost:5002/health
```

---

## API reference

### Store a memory

```bash
# Basic
curl -X POST http://localhost:5002/v1/memories \
  -H 'Content-Type: application/json' \
  -d '{"content": "...", "category": "decisions", "subcategory": "architecture"}'

# With provenance
curl -X POST http://localhost:5002/v1/memories \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "...",
    "category": "decisions",
    "namespace": "myagent/analyst",
    "source_model": "gemma4-consult",
    "source_agent": "background-enricher"
  }'

# Team/enterprise: include API key
curl -X POST http://localhost:5002/v1/memories \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <your-api-key>' \
  -d '{"content": "...", "category": "decisions"}'
```

### Search

```bash
# Full-text search
curl -X POST http://localhost:5002/v1/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "topic keywords", "limit": 10}'

# Filtered by category
curl -X POST http://localhost:5002/v1/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "keywords", "category": "solutions", "limit": 5}'

# Semantic (vector) search
curl -X POST http://localhost:5002/v1/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "keywords", "semantic": true, "limit": 10}'
```

### Admin: create user and API key

```bash
# Create a user
curl -X POST http://localhost:5002/admin/users \
  -H 'Content-Type: application/json' \
  -d '{"id": "alice", "display_name": "Alice", "role": "user"}'

# Generate API key — raw_key shown once only
curl -X POST http://localhost:5002/admin/users/alice/apikeys \
  -H 'Content-Type: application/json' \
  -d '{"label": "cli-key"}'

# Revoke a key
curl -X DELETE http://localhost:5002/admin/apikeys/<key-id>
```

### GRAEAE reasoning

```bash
curl -X POST http://localhost:5002/v1/consultations \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Your question", "task_type": "architecture_design"}'

# Extract best result by score
curl -X POST http://localhost:5002/v1/consultations \
  -d '{"prompt": "...", "task_type": "reasoning"}' | \
  jq '.all_responses | to_entries | sort_by(-.[1].final_score)[0]'
```

### Memory categories

| Category | Use for |
|----------|---------|
| `infrastructure` | Configs, endpoints, system state |
| `solutions` | Workarounds, resolved problems |
| `patterns` | Reusable approaches |
| `decisions` | Rationale and tradeoffs |
| `projects` | Per-project context |
| `standards` | Quality gates, conventions |

### GRAEAE task types

| Task type | Notes |
|-----------|-------|
| `architecture_design` | Full consensus |
| `reasoning` | Full consensus |
| `code_generation` | Speed-optimized provider subset |
| `web_search` | Real-time capable providers |

---

## Compression quality manifest

```json
{
  "compression_id": "uuid",
  "quality_rating": 92,
  "what_was_removed": ["2 introductory sentences", "3 supporting examples"],
  "what_was_preserved": ["Complete reasoning chain", "All main conclusions"],
  "risk_factors": ["Missing examples may reduce convincingness"],
  "safe_for": ["Initial consultation", "Quick decision making"],
  "not_safe_for": ["Security-critical decisions", "Detailed technical review"]
}
```

---

## FAQ

### Why is it called MNEMOS, and why are you using all these mythological names — who do you think you are, a fantasy novelist?

Fair question, and we get it more than you'd think. Short answer: the names aren't set dressing. Each one is a functional tag that happens to line up with a real Greek concept, because memory is one of the domains where Greek already had the vocabulary we needed.

- **MNEMOS** — short for Mnemosyne, the Titan goddess of memory and mother of the Muses. The system stores and retrieves memory. "MemoryService" felt like underselling it.
- **GRAEAE** — the three sisters in the Perseus myth who shared one eye and one tooth, passing them back and forth to see and speak. GRAEAE is the multi-LLM consensus layer: several providers sharing one prompt and converging on one consolidated answer. The metaphor was already sitting there.
- **THE MOIRAI** — the three Fates, who spin, measure, and cut the thread of life. The compression stack is collectively THE MOIRAI because each of its three tiers decides what part of a memory's thread survives:
  - **LETHE** (river of forgetfulness) — Tier 1, CPU, fast, aggressive: throws away what you won't miss.
  - **ALETHEIA** (literally *a-lethe*, "unforgetting" — truth, disclosure) — Tier 2, GPU, deeper: preserves what the first pass would have lost.
  - **ANAMNESIS** (recollection) — Tier 3, archival, slowest: distills long-term facts so you can recover them years later.

No, we are not fantasy novelists. The naming scheme is what happens when the domain you're working in is literally the thing a pre-Socratic culture wrote whole theogonies about, and you decide to use their vocabulary instead of inventing a worse one. Every name is aligned to what the component does, not chosen for atmosphere.

If you strongly prefer `MemoryService` / `LLMRouter` / `CompressorTier1`, the code does exactly the same thing regardless of the label. They're just tags. We like ours.

### Do I need GPU hardware?

No. CPU-only installs run fine — LETHE (Tier 1 compression) runs on CPU, and the API server itself never needs a GPU. ALETHEIA (Tier 2 GPU compression) and the optional local GPU inference backends only kick in when `GPU_PROVIDER_HOST` is configured. For most deployments, CPU plus one external LLM provider is enough.

### Does it work with [OpenAI / Anthropic / Groq / Together / local Ollama]?

Yes. GRAEAE routes across any configured provider. Together AI and Groq are the default free-tier providers (no paid account required to get started). OpenAI, Anthropic, and Perplexity are supported as fallback providers. Local Ollama is first-class — MNEMOS can run fully offline with Ollama plus `nomic-embed-text` for embeddings.

### Is there a hosted version?

Not today. Self-hosted only. If you want a managed deployment, the proprietary commercial license covers custom arrangements — see `LICENSE-PROPRIETARY.md`.

### How is this different from Mem0 / Zep / MemPalace / LangChain memory?

See the *MemPalace and MNEMOS: different problems, not competitors* section above, plus the comparison table. Short version: those are in-process libraries or conversation-history stores designed for single-user / single-agent deployment. MNEMOS is a network service with multi-tenant isolation, a cryptographic audit chain, and a DAG-versioned memory model. Different form factor, different primary user.

### Does it phone home or collect telemetry?

No. There is no outbound telemetry of any kind. The only outbound traffic is the LLM provider calls you configure yourself, the webhook deliveries you register, and the federation syncs you set up. The code is all here; grep `httpx` if you want to confirm.

### Can I use it in production?

Yes — we have been since December 2025. v3.0.0 is the first public release, not a greenfield experiment; the codebase has been operated continuously for roughly four months before being cut for open source. The honest caveat: it has been single-operator-tested, not yet battle-tested across many independent deployments. File issues against the live install and we'll track them.

### What's the migration story from [Mem0 / Zep / raw PostgreSQL]?

Currently manual — write a one-shot script that hits `POST /v1/memories/bulk` with your source data. Direct-import adapters for major competitors are on the roadmap but not yet shipped.

### Apache 2.0 vs the proprietary commercial license — which one do I use?

- **Apache 2.0** (`LICENSE`) — the OSS distribution. Free to use, modify, redistribute, fork, run commercially, etc. Standard permissive OSS license. **This is what you use.**
- **Proprietary commercial license** (`LICENSE-PROPRIETARY.md`) — only matters if you need terms beyond Apache 2.0 (custom indemnification, specific SLA language, managed-service arrangements, vendor-of-record requirements). Most users don't need this. Contact the maintainer if you do.

If you're not sure, it's Apache 2.0.

### Why port 5002 and not something normal like 8080?

Historical. Earlier versions split MNEMOS (5000) and GRAEAE (5001) across two services; v3 unified them on 5002 to signal "this is the combined single service". Override with `MNEMOS_PORT` if 5002 is taken.

### Does it run in Docker / Kubernetes?

Yes. `Dockerfile` and `docker-compose.yml` ship in the repo; `docker compose up -d` gets you a working MNEMOS + PostgreSQL instance for local evaluation. For Kubernetes, the Docker image is the starting point — no Helm chart yet, but the service is stateless on its own (Postgres is the only state), so a standard Deployment + Service + ConfigMap pattern works.

### How do I secure it in production?

- Set `MNEMOS_API_KEY` and require Bearer auth on all requests.
- Enable `RATE_LIMIT_ENABLED=true` (it's on by default).
- Set `MNEMOS_SESSION_SECRET` to a stable value so OAuth flows survive restarts.
- Set `OAUTH_TRUST_PROXY=true` + `RATE_LIMIT_TRUST_PROXY=true` only when you're behind a reverse proxy you control.
- Keep `WEBHOOK_ALLOW_PRIVATE_HOSTS=false` (the default). SSRF defense is on by default.
- Run behind a TLS-terminating reverse proxy. Don't expose the Uvicorn socket directly.
- Review `SECURITY.md` for the full checklist.

---

## License

MNEMOS is dual-licensed:

- **Apache License 2.0** for the open-source distribution in this repository — see [`LICENSE`](./LICENSE).
- **Proprietary commercial license** available by agreement for organizations that need alternative commercial terms — see [`LICENSE-PROPRIETARY.md`](./LICENSE-PROPRIETARY.md).

Possession of this repository does not automatically grant the proprietary commercial license; contact the maintainer for those terms.
