# MNEMOS + GRAEAE

**Memory infrastructure for the agentic ecosystem.**

Deploy MNEMOS once. Every agent in your stack — across tools, sessions, and processes — shares the same quality-tracked memory pool. It runs alongside your agents the way Redis runs alongside your application — always available, always consistent, outliving any individual process.

This is not an embedded library. It is a network service with a REST API and a PostgreSQL backend, backed by a multi-LLM reasoning engine (GRAEAE) that runs separately on the same host or across your network.

---

## Origin

MNEMOS started in December 2025 as a frustration with a very specific problem: every new AI agent session started from scratch.

We were running a homelab spanning several machines — a dedicated GPU inference server for local models, a reasoning host, a compute node for batch workloads, NAS storage, and developer workstations. On top of that infrastructure we were building a stack of agentic tools: a multi-stage data pipeline, a domain-specific analysis system, a financial portfolio skill, and an agentic platform that coordinated them. Each project had its own infrastructure knowledge, its own architectural decisions, its own solved problems. And every time a new agent session started, all of that had to be rebuilt from scratch — re-explained, re-loaded, re-reasoned through — at the cost of context window and API spend.

The first approach was the obvious one: a large context file injected as a system prompt. It worked, briefly. Then it hit 40K tokens and became the entire context budget. You could not selectively load the parts that mattered for the task at hand. Everything came in or nothing did. Compressing it manually was lossy and opaque — you never knew what you had thrown away.

The second problem arrived simultaneously: reasoning costs. A single frontier model query for architecture decisions ran $0.075 per thousand tokens. We were making those decisions constantly — pipeline stage design, multi-tier enrichment strategies, agent routing models — and the costs added up fast. And expensive model answers were no better than a well-run consensus of cheaper models. We knew this because we had run the comparison.

GRAEAE was the answer to the second problem: a multi-LLM consensus engine that distributed reasoning queries across multiple providers simultaneously, scored the responses, and returned the best result for a fraction of the cost — with better coverage of the solution space because no single model's blind spots dominated.

MNEMOS was the answer to the first problem: a PostgreSQL-backed memory service that stored what the agents learned, compressed it intelligently over time, and retrieved the right subset for the task at hand. The compression had to come with a receipt — a manifest saying what was removed, what was kept, and whether it was safe to use the compressed version for this particular task type.

The first production commit landed on February 18, 2026. By April 2026 the system had stored **6,793 memories** and performed **3,077 compressions**, each with a quality manifest. It runs v2.3.0 in production today, backing multiple active agentic tools simultaneously. The tools it was built alongside are still running on it.

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
| **MemPalace** | Spatial hierarchy with verbatim local storage | What was compressed and whether it was safe; scale beyond one machine |
| **CrewAI / AutoGen memory** | Per-crew or per-agent embedded memory | Cross-session persistence; compression quality |

### What MNEMOS does that none of them do

**Quality contracts on compression.** Every time MNEMOS compresses a memory — on write, on rehydration, or before a GRAEAE consultation — it produces a manifest: what was removed, what was preserved, the quality rating, and which use cases the compressed version is and is not safe for. No other memory system treats compression as something that requires a receipt.

**A reasoning layer that degrades gracefully.** GRAEAE distributes queries across multiple LLM providers simultaneously, scores responses on relevance, coherence, completeness, and toxicity, and returns the best result. Per-provider circuit breakers prevent a failing provider from degrading the pool. A semantic cache means identical questions skip inference entirely. This is not a load balancer — it is a quality-gated reasoning bus.

**A knowledge graph alongside free-text memory.** MNEMOS stores structured triples (subject → predicate → object) with temporal validity windows alongside unstructured memories, and exposes a timeline API per subject. Most memory systems are text-only.

---

## What works now

This is the current state of v2.3.0 + v1 multi-user. Features described here are implemented and running in production. Features in the roadmap section are not yet implemented.

### MNEMOS API

| Endpoint | What it does |
|----------|-------------|
| `POST /memories` | Store a memory with category, subcategory, content, and optional provenance |
| `GET /memories` | List memories, filterable by category and subcategory |
| `GET /memories/{id}` | Retrieve a single memory |
| `POST /memories/search` | Semantic search with score threshold and category filter |
| `POST /memories/bulk` | Bulk create memories |
| `PATCH /memories/{id}` | Update memory content or metadata |
| `DELETE /memories/{id}` | Delete a memory |
| `POST /memories/rehydrate` | Load a compressed set of memories for context injection |
| `POST /ingest/session` | Ingest a session transcript |
| `GET /health` | Health check |
| `GET /stats` | Memory counts by category, compression statistics |

### Multi-user and provenance (v1, shipped)

Each memory carries full ownership and LLM provenance:

- `owner_id` — which user owns this memory
- `group_id` — optional group for shared access
- `namespace` — logical partition (e.g. `investorclaw/analyst`)
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
| `POST /triples` | Create a subject → predicate → object triple |
| `GET /triples` | List triples with filters |
| `GET /timeline/{subject}` | All triples for a subject in temporal order |
| `PATCH /triples/{id}` | Update a triple |
| `DELETE /triples/{id}` | Delete a triple |

### GRAEAE reasoning engine

| Endpoint | What it does |
|----------|-------------|
| `POST /graeae/consult` | Multi-LLM consensus query |
| `GET /graeae/health` | Provider availability and circuit breaker status |

**GRAEAE core modules (all operational):**
- **Circuit breaker** — per-provider CLOSED/OPEN/HALF_OPEN state machine, 5-minute cooldown
- **Persistent queue** — SQLite-backed, crash-safe, resumable
- **Semantic cache** — embedding-similarity deduplication, 24-hour TTL
- **Quality scorer** — relevance, coherence, toxicity, completeness scoring per provider
- **Rate limiter** — four backpressure levels, request queuing before rejection

### Compression

- **token-filter²** (Hybrid Compression Squared) — GRAEAE-assisted semantic compression
- **SENTENCE** (Semantic Adaptive Compression) — heuristic, runs offline with no external calls
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

---

## Roadmap

These features are designed and scoped but not yet implemented.

**v2 — Versioning and audit**
- Memory version history (`memory_versions` table) — every mutation auto-snapshots previous state
- Diff and revert API
- SHA-256 hash-chained audit log for GRAEAE responses

**v3 — Scale and federation**
- OAuth/OIDC for enterprise authentication
- Cross-instance memory federation
- Webhook subscriptions — notify agents on memory write

---

## Architecture

```
Agents (any language, any framework)
        │
        │  REST API (port 5000)
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
PostgreSQL           GRAEAE (port 5001)
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
cd mnemos-production
docker compose up
# MNEMOS: http://localhost:5000
# GRAEAE: http://localhost:5001 (if available)
```

### Manual install

```bash
git clone <your-repo-url>
cd mnemos-production
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
```

### Start

```bash
python api_server.py        # MNEMOS on port 5000
python graeae/server.py     # GRAEAE on port 5001 (optional)

curl http://localhost:5000/health
```

---

## API reference

### Store a memory

```bash
# Basic
curl -X POST http://localhost:5000/memories \
  -H 'Content-Type: application/json' \
  -d '{"content": "...", "category": "decisions", "subcategory": "architecture"}'

# With provenance
curl -X POST http://localhost:5000/memories \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "...",
    "category": "decisions",
    "namespace": "myagent/analyst",
    "source_model": "gemma4-consult",
    "source_agent": "background-enricher"
  }'

# Team/enterprise: include API key
curl -X POST http://localhost:5000/memories \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <your-api-key>' \
  -d '{"content": "...", "category": "decisions"}'
```

### Search

```bash
# Full-text search
curl -X POST http://localhost:5000/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "topic keywords", "limit": 10}'

# Filtered by category
curl -X POST http://localhost:5000/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "keywords", "category": "solutions", "limit": 5}'

# Semantic (vector) search
curl -X POST http://localhost:5000/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "keywords", "semantic": true, "limit": 10}'
```

### Admin: create user and API key

```bash
# Create a user
curl -X POST http://localhost:5000/admin/users \
  -H 'Content-Type: application/json' \
  -d '{"id": "alice", "display_name": "Alice", "role": "user"}'

# Generate API key — raw_key shown once only
curl -X POST http://localhost:5000/admin/users/alice/apikeys \
  -H 'Content-Type: application/json' \
  -d '{"label": "cli-key"}'

# Revoke a key
curl -X DELETE http://localhost:5000/admin/apikeys/<key-id>
```

### GRAEAE reasoning

```bash
curl -X POST http://localhost:5001/graeae/consult \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Your question", "task_type": "architecture_design"}'

# Extract best result by score
curl -X POST http://localhost:5001/graeae/consult \
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

## License

Apache 2.0 — see `LICENSE`.
