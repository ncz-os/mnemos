> ## 📍 Canonical source: GitLab
> The authoritative source for this project lives on GitLab — always: **https://gitlab.com/ncz-os/mnemos**
>
> This GitHub repository is retained **only** to host container images on ghcr.io (`mnemos-core`, `mnemos`, `mnemos-enterprise`). Development, issues, and merge requests happen on GitLab.

---

<p align="center">
  <img src="docs/images/logo.png" alt="MNEMOS" width="400" />
</p>

# MNEMOS + GRAEAE

**MNEMOS v6.1.2 is the memory operating system for
serious agentic work: a packaged FastAPI runtime, **EPIMONE** — the six-backend
persistence layer (SQLite + sqlite-vec by default, PostgreSQL + pgvector,
Oracle Database 26ai HNSW INMEMORY NEIGHBOR GRAPH, IBM Db2 12.1.5 (EAP) DiskANN
vector, MySQL 9.0 Enterprise/HeatWave `VECTOR_DISTANCE`, and MariaDB 11.7+
native `VEC_DISTANCE_COSINE` + HNSW — every backend self-provisions its schema
on first connect), GRAEAE reasoning bus,
operator-audited compression stack, divergent dream-state pipeline (REPLAY ->
CLUSTER -> CONSOLIDATE -> SYNTHESISE -> EXTRACT), GDPR right-to-be-forgotten
worker, PERSEPHONE archival subsystem, PANTHEON unified LLM facade, KRONOS
recall observability, and CLI-first deployment surface.**

MNEMOS is not just a place to put bytes. It is a runtime of named subsystems that
manage the full lifecycle of agent memory across providers, agents, and time
horizons: **write, embed, search, compress, version, reason-over, audit,
federate, export, import, and operate**.

> **v6.0.0 — the split-distribution release.** MNEMOS is now a small core
> (`mnemos-core`) plus separately installable `mnemos.*` namespace subsystems
> (GRAEAE, PANTHEON, KNEMON, CHARON) and the standalone STIPHOS hive service. It
> adds Oracle Database 26ai and IBM Db2 12.1.5 (EAP) as first-class persistence
> backends alongside PostgreSQL and SQLite. Turnkey container images are
> published to `ghcr.io/ncz-os` (`mnemos-core`, `mnemos`, `mnemos-enterprise`,
> `mnemos-stiphos`); see [docs/INSTALL.md](docs/INSTALL.md) and
> [AGENTS.md](AGENTS.md). Enterprise backend
> driver, DSN, and migration steps are in
> [docs/INSTALL.md](docs/INSTALL.md#enterprise-backends-oracle-database-26ai--ibm-db2-1215).
> Development history continues
> on the `feat/oracle-port` branch.


## Quick Start

> **🚀 Fastest path — the [free-backend Quickstart](quickstart/README.md).**
> Two commands to durable, MCP-accessible agent memory on a **free** database.
> It's built around **IBM Db2 12.1.5** — the reference deployment for the
> *"mnemos on Db2"* IBM TechXchange write-up — but the exact same image and
> steps run unchanged on **Oracle Database 23ai Free**, **PostgreSQL + pgvector**,
> or **MariaDB 11.7+**. Pick a backend in
> [`quickstart/docs/BACKENDS.md`](quickstart/docs/BACKENDS.md); we lead with Db2,
> it works with all of them.

Memory and reasoning runtime for AI agents: persistent search, versioned storage, webhook fanout, and a unified LLM routing bus - all behind a single MCP interface.

---

### 1. Agent-driven install

Paste into Claude Code, Cursor, or Codex. The agent runs the install; you
confirm. Agents should read [AGENTS.md](AGENTS.md) — it has the machine-readable
module registry and a deterministic procedure for installing exactly the
requested modules on the operator's arch + backend.

The pip package is **`mnemos-core`** (the subsystems are separate dists pulled
via extras). `mnemos` is the published **image** name, not a pip package.

**Turnkey (container, any arch):**

```
docker run -p 5002:5002 -v mnemos-data:/data ghcr.io/ncz-os/mnemos:latest
# everything image: core + graeae + pantheon + knemon + charon. SQLite by default.
# Point at a real DB with -e MNEMOS_DATABASE_DSN='postgres://…' (or oracle://… thin).
```

**pip (compose your own):**

```
Install MNEMOS on this machine.

Steps:
1. pip install 'mnemos-core[server]'   # everything; arch-neutral (no openvino)
2. mnemos init                         # scaffold config + token
3. mnemos serve                        # start API on :5002
4. mnemos doctor                       # verify subsystems
5. Set MNEMOS_BASE=http://localhost:5002 and MNEMOS_API_KEY=<token from step 2>
   in shell env and any agent config that needs to reach it.

Edge device (SQLite kernel only): pip install 'mnemos-core[edge]'
Single subsystem, e.g. reasoning: pip install 'mnemos-core[graeae]'
Hive (STIPHOS) is a SEPARATE service: pip install 'mnemos-stiphos[mcp]' (port 8080)
```

**Enterprise backends (Oracle Database 26ai, IBM Db2 12.1.5 EAP).**
Turnkey is the **amd64-only** `mnemos-enterprise` image (everything + Oracle/Db2/
MySQL drivers baked in). Note: Oracle uses the thin driver, so it also runs on
the plain `mnemos` image and on arm64 — only Db2 actually requires enterprise.
See [docs/INSTALL.md](docs/INSTALL.md#enterprise-backends-oracle-database-26ai--ibm-db2-1215)
for full driver, DSN, and migration steps.

```
# Turnkey (amd64):
docker run --platform linux/amd64 -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='db2://user:pass@host:50000/dbname' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# Or from source:
git clone https://github.com/ncz-os/mnemos && cd mnemos
python -m pip install -e '.[server,enterprise]'   # or '.[server,oracle]' / '.[server,db2]'
export MNEMOS_DATABASE_DSN='oracle://user:pass@host:1521/service_name'
# or:  MNEMOS_DATABASE_DSN='db2://user:pass@host:50000/dbname'
mnemos install --profile server
mnemos serve --profile server
```

---

### 2. Connect an agent via MCP

Add to `~/.claude/mcp_servers.json` (Claude Code) or equivalent:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://<host>:5002",
        "MNEMOS_API_KEY": "<token>"
      }
    }
  }
}
```

For HTTP/SSE transport (ChatGPT, remote agents): `mnemos serve mcp-http` on `:5004`.

Key MCP tools the agent gets:

| Tool | What it does |
|---|---|
| `search_memories` | Semantic + filtered search across the memory store |
| `create_memory` | Write a new memory with category, tags, and content |
| `get_memory` | Fetch a memory by ID |
| `kg_search` | Query the knowledge-graph triple store |
| `kronos_anomalies` | Surface recall anomalies and memory health signals |
| `list_deletions` | List soft-deleted memories pending hard deletion |

---

### 3. Webhooks + integrations

| Integration | What connects | How |
|---|---|---|
| **Claude Code** | Hooks fire on session-start, prompt-submit, stop - auto-log to MNEMOS | `integrations/claude-code/` - copy hooks + set `MNEMOS_BASE` |
| **ZeroClaw** | Zeroclaw agent reads/writes memories via MCP | `integrations/zeroclaw/` + `mnemos serve mcp-stdio` in zeroclaw config |
| **OpenClaw** | OpenClaw gateway routes memory ops through MCP | `integrations/openclaw/` + MCP server entry in `openclaw.json` |
| **Hermes** | Optional memory skill mounts MNEMOS as a tool provider | `integrations/hermes/optional-skills/memory/mnemos/` |
| **Webhooks (any)** | Push `memory.created`, `memory.updated`, `memory.deleted`, `consultation.completed` events to any HTTPS endpoint | `POST /api/webhooks/register` with `{"url": "...", "events": [...]}` |
| **Cursor / Cline / Continue.dev / Zed / Aider** | Any MCP-capable IDE connects via stdio or HTTP transport | See `docs/connectors/` |

---

Full documentation: [docs/](docs/)

## Architecture

MNEMOS is a packaged FastAPI service with a single `mnemos` CLI for installation, serving, MCP transport, and operational checks. Agents connect through MCP stdio, MCP HTTP/SSE, REST, or OpenAI-compatible SDKs, while the runtime routes memory, reasoning, session, webhook, federation, portability, and observability work through the `mnemos/` package. Persistence is selected by profile and DSN: **SQLite + sqlite-vec** for edge and development installs, **PostgreSQL + pgvector** for server deployments, **Oracle Database 26ai** (`23.26.1-ee`, HNSW INMEMORY NEIGHBOR GRAPH, JSON Duality, TDE) for enterprise installs, and **IBM Db2 12.1.5** (native `VECTOR(768, FLOAT32)` + DiskANN vector index; runs through **Db2 Oracle Compatibility Mode** with cursor-level Oracle→Db2 token translation — a native Db2 dialect port is on the v6.x roadmap, see [docs/v6.1-roadmap.md](docs/v6.1-roadmap.md). `Db2MemoryRepository.semantic_search` emits native Db2 SQL — `VECTOR_DISTANCE(..., EUCLIDEAN)` + `FETCH APPROX FIRST` — engaging the DiskANN index on the user-facing query path) for enterprise installs, **MySQL 9.0+** (native `VECTOR` + `VECTOR_DISTANCE` — note these functions ship only in MySQL **Enterprise/HeatWave**, not Community) for the managed-cloud MySQL audience (RDS/Aurora MySQL, HeatWave), and **MariaDB 11.7+** (native `VECTOR` columns, `VEC_DISTANCE_COSINE`/`VEC_FromText`, HNSW `VECTOR INDEX` — all in the **free Community** edition, embeddings stored in a `memory_embeddings` join table) as the default open-source/self-hosted vector backend for the MySQL family. All six backends implement the same `PersistenceBackend` ABC (`mnemos/persistence/base.py`), self-provision their schema idempotently on `backend.open()` (DSN-aware, dimension from `MNEMOS_EMBEDDING_DIM`), and share `tests/test_persistence_parity.py`. **Recommended default for vector/semantic workloads: PostgreSQL + pgvector** — the most mature, predictable, and well-scaled vector store (HNSW, broad managed-service support); MariaDB is the strongest *MySQL-family* option but its vector engine is newer/less battle-tested. GRAEAE handles multi-provider reasoning and model routing; MOIRAI handles operator-audited compression through APOLLO and ARTEMIS.

## Documentation

| Topic | File |
|---|---|
| Installation | [docs/INSTALL.md](docs/INSTALL.md) |
| Specification | [docs/SPECIFICATION.md](docs/SPECIFICATION.md) |
| System requirements | [docs/SYSTEM_REQUIREMENTS.md](docs/SYSTEM_REQUIREMENTS.md) |
| Memory architecture | [docs/MEMORY_ARCHITECTURE.md](docs/MEMORY_ARCHITECTURE.md) |
| Compression | [docs/COMPRESSION.md](docs/COMPRESSION.md) |
| GRAEAE reasoning | [docs/GRAEAE_FEATURES.md](docs/GRAEAE_FEATURES.md) |
| PANTHEON provider facade | [docs/PANTHEON.md](docs/PANTHEON.md) |
| KRONOS observability | [docs/KRONOS.md](docs/KRONOS.md) |
| Portability format (MIF 1.0; MPF legacy) | [docs/MEMORY_EXPORT_FORMAT.md](docs/MEMORY_EXPORT_FORMAT.md) |
| Scaling | [docs/SCALING.md](docs/SCALING.md) |
| Single-binary builds | [docs/SINGLE_BINARY.md](docs/SINGLE_BINARY.md) |
| Operations | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Benchmark harness | [scripts/bench_v4.py](scripts/bench_v4.py) — cross-backend vector-search harness (PG / Oracle / Db2 / SQLite). Results published post-GA. |

## License

MNEMOS is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.


## Build infrastructure & partners

Continuous integration and package distribution for this project are generously
supported by our open-source infrastructure partners:

- **[GitLab](https://gitlab.com/)** — canonical source hosting and CI pipelines
  (format / lint / test gates), via the
  [GitLab for Open Source](https://about.gitlab.com/solutions/open-source/) program.
- **[Buildkite](https://buildkite.com/)** — CI/CD orchestration with hosted macOS
  and Linux agents, and our APT package registry host
  (`packages.buildkite.com/ncz-os/ncz`), via the
  [Buildkite Open Source](https://buildkite.com/pricing) program.

Thank you to both for backing open-source software.
