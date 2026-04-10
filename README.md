# MNEMOS + GRAEAE

**Memory infrastructure for the agentic ecosystem.**

Deploy MNEMOS once. Every agent in your stack — across tools, sessions, and providers — shares the same quality-tracked memory pool. With namespace isolation and per-owner provenance, each agent's memories are sovereign but accessible across the ecosystem when permitted.

This is not an embedded library. It is a network service with a REST API, a PostgreSQL backend, and a multi-LLM reasoning engine. It runs alongside your agents the way Redis runs alongside your application — always available, always consistent, outliving any individual process.

---

## Why use this

The field of agent memory systems is crowded and getting more so. Here is the honest case for MNEMOS.

**Most memory systems answer one question badly:** "What did this agent say before?"

That is conversation history. It is useful, but it is not memory infrastructure. Conversation history dies when the session ends, scales to one agent, and tells you nothing about whether the information it contains is still accurate, still complete, or safe to rely on.

MNEMOS answers a different set of questions:

- *What do all my agents collectively know, and who told them?*
- *When I compressed that memory to fit in a context window, what did I throw away — and was it safe to throw away?*
- *Which LLM actually produced this synthesis, and can I verify it?*
- *If three of my providers go down, does my reasoning layer fail or degrade gracefully?*

If none of those questions matter for your use case, a simpler tool is probably the right choice. If they do matter, MNEMOS is the only system that answers all four.

### The specific gaps in the alternatives

| System | What it does | What it cannot tell you |
|--------|-------------|------------------------|
| **MemGPT / Letta** | Hierarchical paging within a single agent session | What was lost in compression; what happens when the LLM provider fails |
| **Mem0** | Store and retrieve memories via API | Compression quality; multi-agent provenance; reasoning consensus |
| **Zep** | Conversation history + entity extraction | Compression manifests; multi-provider reasoning; cross-agent sharing |
| **LangChain / LlamaIndex memory** | In-process buffer or summary | Anything after the process exits |
| **MemPalace** | Spatial hierarchy with verbatim local storage | What was compressed and whether it was safe; multi-agent access; scale beyond one machine |
| **CrewAI / AutoGen memory** | Per-crew or per-agent embedded memory | Cross-agent sharing; provenance; compression quality |

### What MNEMOS does that none of them do

**Quality contracts on compression.** Every time MNEMOS compresses a memory — on write, on rehydration, before a GRAEAE consultation — it produces a manifest: what was removed, what was preserved, the quality rating, and which use cases the compressed version is and is not safe for. You can query this manifest. You can flag compressions below a quality threshold for review. You can retrieve the original. No other memory system treats compression as something that requires a receipt.

**Shared ecosystem memory with provenance.** Memories in MNEMOS are tagged with the user, group, namespace, source model, source provider, and source agent that created them. An InvestorClaw run writes analyst memories tagged `namespace=investorclaw, source_agent=background_enricher, source_model=gemma4-consult`. A separate agent reads across namespaces with appropriate permissions. The memory pool is shared infrastructure, not a per-agent silo.

**A reasoning layer that degrades gracefully.** GRAEAE, the reasoning engine built into MNEMOS, distributes queries across up to six LLM providers simultaneously, scores responses on relevance, coherence, completeness, and toxicity, and returns the best result. Per-provider circuit breakers prevent a failing provider from degrading the pool. A semantic cache means identical questions skip inference entirely. An append-only, SHA-256 hash-chained audit log means every response is tamper-evident. This is not a load balancer — it is a quality-gated reasoning bus.

---

## What it is

### MNEMOS — Quality-tracked shared memory

MNEMOS stores, compresses, and retrieves agent memories with full audit trails across a multi-agent ecosystem.

- **Three compression pathways**: WRITE (on storage), READ (on rehydration), GRAEAE (before consultation)
- **Two compression algorithms**: token-filter² (semantic, GRAEAE-assisted) and SENTENCE (heuristic, always available offline)
- **4-tier memory system**: Recent → Compressed → Archived → Permanent, with task-aware tier selection
- **Quality manifests**: every compression records what was preserved, what was dropped, risk factors, and safe use cases
- **Full provenance**: owner, group, namespace, source model, source provider, source session, source agent on every record
- **UNIX-style access control**: permission modes (owner/group/world read-write) enforced at the PostgreSQL RLS layer
- **Reversal support**: original always retained; retrieve pre-compression version at any time

### GRAEAE — Multi-LLM consensus reasoning

GRAEAE distributes reasoning queries across multiple providers simultaneously and returns a quality-scored consensus result.

- **Six providers**: Perplexity, Together/DeepSeek-R1, Groq, OpenAI, xAI, Ollama
- **Persistent queue**: all requests written to SQLite before dispatch — crash-safe and resumable
- **Circuit breaker**: per-provider CLOSED/OPEN/HALF_OPEN state machine, 5-minute cooldown, automatic recovery
- **Semantic cache**: embedding-similarity deduplication at 0.85 threshold, 24-hour TTL
- **Rate limiting with backpressure**: four levels, request queuing before rejection
- **Cryptographic audit log**: SHA-256 hash-chained, append-only, tamper-evident

---

## Deployment profiles

MNEMOS ships as a single codebase. The installer asks which profile fits your use case. You can grow from personal to enterprise without data migration.

```
? Select deployment profile:
  ▸ Personal    — single user, localhost, no auth, Docker Compose setup
    Team        — 2–20 users, API key auth, shared PostgreSQL
    Enterprise  — 20+ users, OAuth/OIDC, RLS enforced, namespace isolation, compliance audit
```

### Personal

One developer, one machine. MNEMOS + PostgreSQL + Ollama in a single `docker compose up`. No auth required — the API listens on localhost only. Full compression, full GRAEAE, all features available.

### Team

Shared PostgreSQL instance. API key authentication per user. Memories are owned and namespaced. UNIX-style permission modes enforced. GRAEAE pools provider API keys across the team.

### Enterprise

Everything in Team, plus: OAuth/OIDC authentication, PostgreSQL Row Level Security enforced at the database layer (not just application middleware), full namespace isolation per agent/project, compliance-grade audit log.

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
│  compression · manifests · RLS  │
└──────────────┬──────────────────┘
               │
    ┌──────────┴──────────┐
    │                     │
    ▼                     ▼
PostgreSQL           GRAEAE (port 5001)
memories             multi-LLM consensus
compression_log      circuit breaker
graeae_consults      semantic cache
memory_versions      audit log
users / groups
```

MNEMOS and GRAEAE are separate services that can be deployed on the same machine or across nodes. Agents communicate only with the MNEMOS API; GRAEAE is an internal implementation detail unless you want to query it directly.

---

## Quick start

### Prerequisites

- Docker and Docker Compose (personal install)
- Python 3.11+ and PostgreSQL 14+ (manual install)
- Ollama (optional — for local embedding and offline SENTENCE compression)

### Personal install (Docker Compose)

```bash
git clone <your-repo-url>
cd mnemos-production
docker compose up
# MNEMOS: http://localhost:5002
# GRAEAE: http://localhost:5001
```

### Manual install

```bash
pip install -r requirements.txt
psql -U postgres -c "CREATE USER mnemos WITH PASSWORD 'yourpassword';"
psql -U postgres -c "CREATE DATABASE mnemos OWNER mnemos;"
psql -U mnemos -d mnemos -f db/migrations.sql
python api_server.py
```

### Installer (interactive)

```bash
python install.py
# Prompts for: deployment profile, database connection, provider API keys, Ollama endpoint
# Writes config.toml and starts the service
```

---

## API reference

### Memory operations

```bash
# Store a memory
curl -X POST http://localhost:5002/memories \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "...",
    "category": "decisions",
    "namespace": "investorclaw/analyst",
    "tags": ["architecture"]
  }'

# Semantic search
curl -X POST http://localhost:5002/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "topic keywords", "limit": 10, "min_score": 0.3}'

# Search within a namespace
curl -X POST http://localhost:5002/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "keywords", "namespace": "investorclaw/analyst", "limit": 5}'

# Quality check a stored memory
curl http://localhost:5002/memories/<id>/quality-check

# Retrieve the original pre-compression version
curl http://localhost:5002/memories/<id>/original
```

### GRAEAE reasoning

```bash
# Multi-LLM consensus
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
| `architecture_design` | Full consensus, highest quality floor |
| `reasoning` | Full consensus |
| `code_generation` | Speed-optimized provider subset |
| `web_search` | Real-time capable providers |

---

## Compression quality

Every compression produces a manifest:

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

Quality thresholds by task type (configurable in `config.toml`):

| Task type | Minimum quality |
|-----------|----------------|
| Security review | 95% |
| Architecture design | 90% |
| Code generation | 88% |
| Reasoning | 85% |
| General | 80% |

---

## GRAEAE circuit breaker

```
CLOSED --(5 failures)--> OPEN --(5 min cooldown)--> HALF_OPEN --(success)--> CLOSED
                                                           |
                                                      (failure)--> OPEN
```

```bash
curl http://localhost:5001/graeae/health
```

---

## License

Apache 2.0 — see `LICENSE`.
