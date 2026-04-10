# MNEMOS + GRAEAE

**Persistent memory and multi-LLM consensus reasoning for production agentic systems.**

MNEMOS is a memory store built for AI agents that need to remember across sessions, compress context intelligently, and retrieve the right information for the right task. GRAEAE is the reasoning layer on top — a multi-provider consensus engine that routes queries to the best available models, tracks quality, and fails gracefully when providers go down.

Together they form a substrate for agentic systems that need to reason reliably over time.

---

## Why We Built This

Every production agentic system hits the same wall: context windows are finite, LLM providers fail, and nothing guarantees the answer you got was actually good.

Existing approaches each solve part of the problem:

| Tool | What it does | What it misses |
|------|-------------|----------------|
| **MemGPT / Letta** | Hierarchical memory with paging | Single-provider, no quality tracking, no compression manifests |
| **Mem0** | Simple persistent memory layer | No compression strategy, no multi-LLM consensus, no audit trail |
| **LangChain Memory** | In-chain buffer/summary memory | Session-scoped only, no cross-session persistence, no quality scoring |
| **RAG pipelines** | Vector retrieval from documents | Retrieval is not reasoning; no compression quality; no circuit breaking |
| **LlamaIndex** | Document indexing and query | Document-centric, not agent-memory-centric; no compression manifests |

None of them answer: *how much information was lost when you compressed that memory, and was it safe to lose?*

None of them handle: *what happens when three of your six LLM providers go down simultaneously?*

MNEMOS + GRAEAE were built to answer both questions in production.

---

## What It Is

### MNEMOS — Quality-Tracked Persistent Memory

MNEMOS stores, compresses, and retrieves agent memories with full audit trails. Every compression generates a **quality manifest** — a structured record of what was removed, what was preserved, the quality rating, and which use cases the compressed version is safe for.

Key capabilities:

- **Three compression pathways**: WRITE (on storage), READ (on rehydration), GRAEAE (on consultation)
- **Two compression algorithms**: token-filter (semantic, GRAEAE-assisted) and SENTENCE (heuristic, always available)
- **4-tier memory system**: Recent → Compressed → Archived → Permanent, with task-aware tier selection
- **Quality manifests**: Every compression logs what was preserved, what was dropped, risk factors, and safe use cases
- **Reversal support**: Original always retained; downgrade to original if quality falls below threshold
- **PostgreSQL backend**: ACID-compliant, concurrent access, production-scale

### GRAEAE — Multi-LLM Consensus Reasoning

GRAEAE distributes reasoning queries across multiple LLM providers simultaneously, scores the responses, and returns a consensus result. It is not a load balancer — it is a quality-gated reasoning bus.

Six providers in the default configuration: Perplexity, Together/DeepSeek-R1, Groq, OpenAI, xAI, Ollama.

Key capabilities:

- **Consensus mode**: Queries all available providers, scores responses on relevance/coherence/completeness/toxicity, returns best result
- **Persistent queue**: All requests written to SQLite before dispatch — crash-safe, resumable
- **Circuit breaker**: Per-provider CLOSED/OPEN/HALF_OPEN state machine; 5-minute cooldown; automatic recovery
- **Semantic cache**: Embedding-similarity deduplication (0.85 threshold, 24hr TTL) — identical questions skip inference entirely
- **Rate limiting with backpressure**: Four levels; request queuing before rejection
- **Cryptographic audit log**: SHA-256 hash-chained append-only log; every response is tamper-evident

---

## Target User

**AI engineers and researchers building production agentic systems** that need:

- Sessions that remember what happened before — not just in the current context window
- Reasoning that does not collapse when a provider is rate-limited or down
- Compression that tells you what it threw away
- An audit trail that proves the system did not hallucinate its own history

This is not a tool for weekend experiments. It is designed for systems where a bad answer has a cost and a crashed provider cannot silently fail.

---

## Architectural Decisions

### PostgreSQL over SQLite for the memory store

SQLite is fine for single-writer, single-reader scenarios. Agentic systems that run background enrichers, foreground queries, and scheduled distillation jobs simultaneously need ACID isolation and concurrent write support. PostgreSQL also gives us partial indexes, materialized views for compression stats, and native JSONB for manifests — none of which SQLite handles well at scale.

### token-filter + SENTENCE compression, not naive summarization

Summarization with an LLM is expensive, slow, and produces a result with no quality contract. token-filter (Hybrid Compression Squared) uses GRAEAE to intelligently compress memories when the reasoning engine is available, falling back to SENTENCE (Semantic Adaptive Compression) — a heuristic that runs entirely locally with no external calls. The result always includes a manifest. Compression ratios are tuned per task type: code generation compresses more aggressively (0.30) than architecture design (0.50) because implementation details matter differently.

### Multi-provider consensus over single-model routing

Single-provider routing is fragile. When one provider rate-limits or goes down, single-provider systems stall. GRAEAE runs all available providers in parallel and returns the best-scored response. The circuit breaker ensures a flapping provider does not degrade the whole pool. Quality scoring means the system learns which providers perform best for which task types over time.

### Quality manifests as a first-class concept

Most compression pipelines discard information and move on. MNEMOS treats the compression manifest as a first-class artifact: stored in the database, queryable via API, reviewable by humans or agents. The manifest answers: *was it safe to compress this?* If the quality rating falls below a configurable threshold (default: 80%), the system flags the compression for review. Critical task types (security review: 95%, architecture design: 90%) have tighter floors.

### 4-tier memory with task-aware selection

Not all memories are equal. Recent memories (tier 1) are stored at 20% compression — mostly intact. Long-term memories (tier 4) are stored at full compression or archived. The tier selector chooses which tiers to load based on task type and complexity, so a quick code generation task does not pull the entire memory corpus.

### REST API for language-agnostic integration

MNEMOS and GRAEAE are API-first. Claude Code, custom Python scripts, and any HTTP client can use them without importing a Python package. The API is the interface; the implementation can evolve independently.

---

## How It Differs

### From MemGPT/Letta

MemGPT introduced hierarchical memory paging — the right idea, but implemented as a single-agent framework tied to specific model APIs. MNEMOS is a standalone service: any agent, any framework. MemGPT has no compression quality tracking. MNEMOS manifests every compression with what was lost.

### From Mem0

Mem0 is a simple CRUD memory layer. It stores and retrieves. It does not compress, does not score quality, does not chain audit entries cryptographically, and does not route through a multi-provider consensus engine. MNEMOS is more opinionated about what it means for a memory to be *reliably* stored.

### From LangChain/LlamaIndex memory modules

These are in-process, session-scoped memory implementations. They do not persist across process restarts. They have no compression quality contracts. They are designed for single-session demos, not multi-day agentic runs.

### From plain RAG

RAG retrieves from documents. MNEMOS stores *agent-generated* memories — synthesis, decisions, observations — and compresses them over time. Retrieval is semantic but the data source is the agent's own history, not a document corpus. The two complement each other; they are not the same problem.

---

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 14+
- Ollama (optional, for local embedding and SENTENCE compression)

### Installation

```bash
git clone <your-repo-url>
cd mnemos-production
pip install -r requirements.txt
```

### Database setup

```bash
psql -U postgres -c "CREATE USER mnemos WITH PASSWORD 'yourpassword';"
psql -U postgres -c "CREATE DATABASE mnemos OWNER mnemos;"
psql -U mnemos -d mnemos -f db/migrations.sql
```

### Configuration

Set environment variables or edit `config.toml`:

```bash
export MNEMOS_KEYS_PATH=~/.config/mnemos/api_keys.json
export OLLAMA_EMBED_HOST=http://localhost:11434
export GRAEAE_URL=http://localhost:5001
```

### Start the API

```bash
python api_server.py
# Verify: curl http://localhost:5002/health
```

### Start GRAEAE

```bash
python graeae/server.py
# Verify: curl http://localhost:5001/health
```

---

## API Reference

### Memory Operations

```bash
# Store a memory
curl -X POST http://localhost:5002/memories \
  -H 'Content-Type: application/json' \
  -d '{"content": "...", "category": "decisions", "tags": ["architecture"]}'

# Semantic search
curl -X POST http://localhost:5002/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "topic keywords", "limit": 10, "min_score": 0.3}'

# Filtered search by category
curl -X POST http://localhost:5002/memories/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "keywords", "category": "solutions", "limit": 5}'

# Quality check on a stored memory
curl http://localhost:5002/memories/<id>/quality-check

# Retrieve original (pre-compression)
curl http://localhost:5002/memories/<id>/original
```

### GRAEAE Reasoning

```bash
# Multi-LLM consensus
curl -X POST http://localhost:5001/graeae/consult \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Your question", "task_type": "architecture_design", "mode": "consensus"}'

# Extract best result by score
curl -X POST http://localhost:5001/graeae/consult \
  -d '{"prompt": "...", "task_type": "reasoning"}' | \
  jq '.all_responses | to_entries | sort_by(-.[1].final_score)[0]'
```

### Memory Categories

| Category | Use for |
|----------|---------|
| `infrastructure` | Configs, endpoints, system state |
| `solutions` | Workarounds, resolved problems |
| `patterns` | Reusable approaches |
| `decisions` | Rationale and tradeoffs |
| `projects` | Per-project context |
| `standards` | Quality gates, conventions |

### GRAEAE Task Types

| Task type | Notes |
|-----------|-------|
| `architecture_design` | Full consensus, highest quality floor |
| `reasoning` | Full consensus |
| `code_generation` | Speed-optimized provider subset |
| `web_search` | Real-time capable providers |

---

## Compression Quality Reference

Every compression returns a manifest:

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

Quality thresholds (configurable in `config.toml`):

| Task type | Minimum quality |
|-----------|----------------|
| Security review | 95% |
| Architecture design | 90% |
| Code generation | 88% |
| Reasoning | 85% |
| General | 80% |

---

## GRAEAE Circuit Breaker

Providers move through states automatically:

```
CLOSED --(5 failures)--> OPEN --(5 min cooldown)--> HALF_OPEN --(success)--> CLOSED
                                                          |
                                                     (failure)--> OPEN
```

Health status:

```bash
curl http://localhost:5001/graeae/health
```

---

## License

Apache 2.0 — see `LICENSE`.
