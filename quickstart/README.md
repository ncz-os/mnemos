# mnemos — free-backend agent memory Quickstart

Persistent, semantically-searchable **agent memory** on a **free database** — no proprietary
vector store, no cloud lock-in. One mnemos image runs on any of four free, officially-published
databases with a native vector type; an AI coding agent (Claude, Cursor, Codex, …) reads and
writes this memory over **MCP**.

Self-contained and CPU-only: pick a backend, two commands, and you have durable cross-session
memory on a laptop. This is also the reference deployment for the *"mnemos on Db2 12.1.5"* IBM
TechXchange write-up — **we lead with Db2** (see why in [`docs/BACKENDS.md`](docs/BACKENDS.md)).

---

## Backends (pick one — all free, all official containers)

| Profile | Engine | Why |
|---|---|---|
| **`db2`** | IBM Db2 Community Edition | **Recommended** — native `VECTOR` + vector index, fewest CE limits |
| `oracle` | Oracle Database 23ai **Free** | AI Vector Search (EE-eval note in BACKENDS.md) |
| `postgres` | PostgreSQL + pgvector | pure open source, most familiar |
| `mariadb` | MariaDB 11.7+ | pure open source, no vendor tiers |

Same `ghcr.io/ncz-os/mnemos-enterprise` image for all (it carries every backend's driver);
shared CPU **bge-m3** embedder (1024-dim). No GPU.

## Prerequisites
Docker + Docker Compose · ~6 GB RAM free (Db2/Oracle first-boot is the heavy part) · no GPU.

**Embeddings run on CPU** — the bundled `embed` service is llama.cpp serving **bge-m3** (1024-dim).
On first boot it **downloads the GGUF model** (`gpustack/bge-m3-GGUF:Q8_0`, ~600 MB from Hugging
Face), so the very first store may wait ~1–2 min for the embedder to finish loading; it's cached
for subsequent runs. No GPU or embedding API key required. (To use your own OpenAI-compatible
embedder instead, repoint `MNEMOS_EMBED_HTTP_URL` / `MNEMOS_EMBEDDING_DIM` — see `.env.example`.)

## Run it

```bash
cp .env.example .env                    # REQUIRED: set DB_PASSWORD (no default is baked in)
docker compose --profile db2 up -d      # or: oracle | postgres | mariadb
./scripts/init-db2-vectors.sh           # Db2 ONLY, one-time (enable Db2 VECTOR datatype + vector indexing)
curl -s localhost:5002/health           # {"status":"healthy",...}
```

Switching backends: `docker compose --profile <old> down`, then `--profile <new> up -d`.

## Prove it (store → semantic recall) — same for every backend

```bash
curl -s localhost:5002/v1/memories -H 'content-type: application/json' \
  -d '{"content":"mnemos stores agent memory in a free database with a native vector type","category":"reference"}'

# NOTE: the field is  "semantic": true  (NOT mode:"semantic")
curl -s localhost:5002/v1/memories/search -H 'content-type: application/json' \
  -d '{"query":"where does mnemos keep memory","limit":3,"semantic":true}'
```

You get the memory back with a relevance score — served by the backend's native vector search.

---

## The one config rule (applies to every backend)

mnemos is configured entirely by environment (see `docker-compose.yml`). The rule that decides
whether vector recall works at all:

> **`MNEMOS_EMBEDDING_DIM` must equal the embedder's output dimension.** The env var is
> `MNEMOS_EMBEDDING_DIM` (aliases `MNEMOS_EMBEDDING_DIM` / `PG_EMBEDDING_DIM`). bge-m3 → `1024`,
> nomic-embed-text → `768`. **`MNEMOS_DATABASE_EMBEDDING_DIM` is NOT a recognized key** — set
> it and mnemos silently defaults to 768, the schema builds `VECTOR(768)`, and every store
> fails with a vector-dimension cast error.

Per-backend DSNs, the Db2/Oracle vector-mode specifics, and the Oracle-EE licensing note are in
[`docs/BACKENDS.md`](docs/BACKENDS.md). The search field is the boolean **`semantic: true`**
(an unknown `mode` is silently ignored → keyword results). Default relevance floor `0.65`.

Point at an **external** database (drop the DB service from compose): set
`MNEMOS_DATABASE_DSN` accordingly — mnemos self-provisions its schema on first connect.

## Next
- **Wire your AI agent** → [`docs/AGENTS.md`](docs/AGENTS.md) (Claude Code · Cursor · Codex, over MCP).
- **Import memory / documents** → [`docs/MIF-MIGRATION.md`](docs/MIF-MIGRATION.md)
  (MIF portable-memory + IBM Docling document ingestion).

## License / provenance
mnemos is open source (`gitlab.com/ncz-os/mnemos`). Db2 CE (`LICENSE=accept`), Oracle 23ai Free,
PostgreSQL, and MariaDB are all free under their respective licenses. This quickstart carries no
credentials — set your own `DB_PASSWORD`, and enable `MNEMOS_AUTH_ENABLED` + a token before any
shared/production use.
