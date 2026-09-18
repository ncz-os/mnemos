> ## 📍 Canonical source: GitLab
> The authoritative source for this project lives on GitLab — always: **https://gitlab.com/ncz-os/mnemos**
>
> This GitHub repository is retained **only** to host the `mnemos-enterprise` container image on ghcr.io. Development, issues, and merge requests happen on GitLab.

---

<p align="center">
  <img src="docs/images/logo.png" alt="MNEMOS" width="400" />
</p>

# MNEMOS + GRAEAE

**MNEMOS v7.0.0 is the memory operating system for serious agentic work.** It is
not just a place to put bytes: it is a runtime of named subsystems that manage
the full lifecycle of agent memory across providers, agents, and time horizons —
write, embed, search, compress, version, reason over, audit, federate, export,
import, and operate.

What is in the box:

- a packaged **FastAPI runtime** with a CLI-first deployment surface
- **EPIMONE**, the six-backend persistence layer — SQLite + sqlite-vec by
  default, PostgreSQL + pgvector, Oracle AI Database 26ai, IBM Db2 12.1, MySQL 9.0
  Enterprise/HeatWave, and MariaDB 11.7+. Every backend self-provisions its
  schema on first connect. See [Persistence](#persistence).
- the **GRAEAE** reasoning bus and **PANTHEON** unified LLM facade
- an operator-audited compression stack
- a divergent dream-state pipeline: REPLAY → CLUSTER → CONSOLIDATE →
  SYNTHESISE → EXTRACT
- a GDPR right-to-be-forgotten worker
- the **PERSEPHONE** archival subsystem
- first-party **CHARON portability**: MIF/MPF import and export, universal
  ingest, migrate-in adapters, and **STYX** encrypted off-fleet backups
- **KRONOS** recall observability

> **How it is packaged.** MNEMOS ships as a small core (`mnemos-core`) plus
> separately installable `mnemos.*` namespace subsystems (GRAEAE, PANTHEON,
> KNEMON) and the standalone STIPHOS hive service. CHARON's portability,
> migration, ingestion, and STYX backup code became first-party core modules
> on 2026-09-18; only Docling's heavy document-conversion dependencies remain
> optional through `mnemos-core[docling]`. The published
> container image is `ghcr.io/ncz-os/mnemos-enterprise` — a single multi-arch
> (amd64 + arm64) manifest with every backend driver (Oracle, MySQL, MariaDB)
> except Db2, which is amd64-only. Pin an exact version (`:7.0.0`) to keep a fleet
> on identical code. Install and DSN/driver setup for each backend are in
> [docs/INSTALL.md](docs/INSTALL.md); the agent-facing contract is in
> [AGENTS.md](AGENTS.md).


## Quick Start

> **🚀 Fastest path — the [free-backend Quickstart](quickstart/README.md).**
> Two commands to durable, MCP-accessible agent memory on a **free** database.
> It's built around **IBM Db2 12.1** — the reference deployment for the
> *"mnemos on Db2"* IBM TechXchange write-up — but the exact same image and
> steps run unchanged on **Oracle AI Database 26ai Free**, **PostgreSQL + pgvector**,
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
docker run -p 5002:5002 -v mnemos-data:/data ghcr.io/ncz-os/mnemos-enterprise:latest
# everything image: core (including CHARON/STYX) + graeae + pantheon + knemon. SQLite by default.
# Point at a real DB with -e MNEMOS_DATABASE_DSN='postgres://…' (or oracle://… thin).
```

**pip (compose your own):**

> **None of `mnemos-core`, `mnemos-graeae`, `mnemos-pantheon`, `mnemos-knemon`,
> or `mnemos-stiphos` are currently published to PyPI.**
> `pip install 'mnemos-core[...]'` will 404 — the `[server]`/`[full]` extras
> recurse into these names on the public index. Until they're published, install
> from source. This exact sequence is tested in a clean venv:

```
# Core (arch-neutral, no openvino):
git clone https://gitlab.com/ncz-os/mnemos && cd mnemos
python -m pip install -e .

# Add-ons, straight from GitLab — core's already-installed version satisfies
# each add-on's own mnemos-core floor, so pip resolves it locally and never
# needs to reach PyPI for mnemos-core itself:
pip install 'git+https://gitlab.com/ncz-os/graeae.git'
pip install 'git+https://gitlab.com/ncz-os/knemon.git'
pip install 'git+https://gitlab.com/ncz-os/pantheon.git'   # needs graeae + knemon installed first

mnemos init                         # scaffold config + token
mnemos serve                        # start API on :5002
mnemos doctor                       # verify subsystems
# Set MNEMOS_BASE=http://localhost:5002 and MNEMOS_API_KEY=<token from mnemos init>
# in shell env and any agent config that needs to reach it.

# Hive (STIPHOS) is a SEPARATE service, same pattern:
git clone https://gitlab.com/ncz-os/mnemos-stiphos && cd mnemos-stiphos
pip install -e '.[mcp]'   # port 8080
```

Run `pip check` afterward as a sanity check — a clean install reports "No
broken requirements found." Anything it does report is a real gap; install it.

Edge device (SQLite kernel only): after the core install above, `pip install
aiosqlite sqlite-vec` (the `[edge]` extra's own two deps — both real, published
packages, unaffected by the above).

**Enterprise backends (Oracle AI Database 26ai, IBM Db2 12.1).**
`mnemos-enterprise` is a single multi-arch (amd64 + arm64) image with every
backend driver baked in. The one asymmetry: Db2's driver has no arm64 wheel,
so the Db2 backend is amd64-only — Oracle (thin driver), MySQL, and MariaDB
all work on arm64 too. See [docs/INSTALL.md](docs/INSTALL.md) for full
driver, DSN, and migration steps.

```
# Turnkey (amd64):
docker run --platform linux/amd64 -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='db2://user:pass@host:50000/dbname' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# Or from source (see the "pip (compose your own)" section above for the
# add-on install order — enterprise adds the same set plus the driver extra
# on core itself):
git clone https://gitlab.com/ncz-os/mnemos && cd mnemos
python -m pip install -e '.[enterprise]'   # or '.[oracle]' / '.[db2]' — core's own extras, real PyPI deps
pip install 'git+https://gitlab.com/ncz-os/graeae.git'
pip install 'git+https://gitlab.com/ncz-os/knemon.git'
pip install 'git+https://gitlab.com/ncz-os/pantheon.git'
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

For HTTP/SSE transport (ChatGPT, remote agents): `mnemos serve mcp-http`, which listens on `:5003` by default.

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
| **Webhooks (any)** | Push `memory.created`, `memory.updated`, `memory.deleted`, `consultation.completed` events to any HTTPS endpoint | `POST /v1/webhooks` with `{"url": "...", "events": [...]}` |
| **Cursor / Cline / Continue.dev / Zed / Aider** | Any MCP-capable IDE connects via stdio or HTTP transport | See `docs/connectors/` |

---

Full documentation: [docs/](docs/)

## Architecture

MNEMOS is a packaged FastAPI service with a single `mnemos` CLI for
installation, serving, MCP transport, and operational checks. Agents connect
through MCP stdio, MCP HTTP/SSE, REST, or OpenAI-compatible SDKs, while the
runtime routes memory, reasoning, session, webhook, federation, portability,
and observability work through the `mnemos/` package. GRAEAE handles
multi-provider reasoning and model routing; MOIRAI handles operator-audited
compression through APOLLO and ARTEMIS.

### Persistence

The backend is chosen at runtime by DSN scheme, never by rebuild. Six are
implemented, in `mnemos/persistence/`:

| Backend | Vector support | Notes |
|---|---|---|
| **SQLite + sqlite-vec** | `vec0` virtual table | Default. Edge and development installs; no server to run. |
| **PostgreSQL + pgvector** | HNSW | **Recommended for vector and semantic workloads** — the most mature and predictable option, with broad managed-service support. |
| **Oracle AI Database 26ai** | HNSW `INMEMORY NEIGHBOR GRAPH` | Also JSON Duality and TDE. Thin driver, so it runs on the standard `mnemos` image and on arm64. |
| **IBM Db2** | DiskANN | Hot paths emit native Db2 SQL — `VECTOR_DISTANCE(..., EUCLIDEAN)` with `FETCH APPROX FIRST`, engaging the DiskANN index on the user-facing query path. The default dialect (`MNEMOS_DB2_DIALECT=compat`) still translates inherited Oracle-shaped SQL at the cursor layer; set `MNEMOS_DB2_DIALECT=native` for the pass-through backend. amd64 only. |
| **MySQL 9.0+** | `VECTOR_DISTANCE` | For the managed-cloud MySQL audience (RDS and Aurora MySQL, HeatWave). Note that the vector functions ship only in MySQL **Enterprise/HeatWave**, not Community. |
| **MariaDB 11.7+** | `VEC_DISTANCE_COSINE` + HNSW `VECTOR INDEX` | The strongest *MySQL-family* option, and available in the **free Community** edition. Embeddings live in a `memory_embeddings` join table. Its vector engine is newer than pgvector's and correspondingly less battle-tested. |

Every backend satisfies the same `PersistenceBackend` protocol set
(`mnemos/persistence/base.py`) and self-provisions its schema idempotently on
`backend.open()`, DSN-aware, with the dimension taken from
`MNEMOS_EMBEDDING_DIM`. SQLite and PostgreSQL share the cross-backend harness
in `tests/test_persistence_parity.py`; Oracle, Db2, MySQL, and MariaDB are
each covered by their own live suite.

## Documentation

| Topic | File |
|---|---|
| Installation | [docs/INSTALL.md](docs/INSTALL.md) |
| Specification | [docs/SPECIFICATION.md](docs/SPECIFICATION.md) |
| System requirements | [SYSTEM_REQUIREMENTS.md](SYSTEM_REQUIREMENTS.md) |
| Memory architecture | [docs/MEMORY_ARCHITECTURE.md](docs/MEMORY_ARCHITECTURE.md) |
| Compression | [docs/COMPRESSION.md](docs/COMPRESSION.md) |
| GRAEAE reasoning | [docs/GRAEAE_FEATURES.md](docs/GRAEAE_FEATURES.md) |
| PANTHEON provider facade | [docs/PANTHEON.md](docs/PANTHEON.md) |
| KRONOS observability | [docs/KRONOS.md](docs/KRONOS.md) |
| Audit chain | [docs/AUDIT_CHAIN.md](docs/AUDIT_CHAIN.md) |
| Portability format (MIF 1.0) | [docs/MEMORY_EXPORT_FORMAT.md](docs/MEMORY_EXPORT_FORMAT.md) |
| Scaling | [docs/SCALING.md](docs/SCALING.md) |
| Operations | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Backend parity matrix | [docs/BACKEND_PARITY.md](docs/BACKEND_PARITY.md) — generated by `scripts/generate_backend_parity_matrix.py`, CI-gated against drift by `lint:backend-parity`. Do not hand-edit. |
| Known limitations | [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) |
| SQLite / edge profile | [docs/SQLITE_PROFILE.md](docs/SQLITE_PROFILE.md) |
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
