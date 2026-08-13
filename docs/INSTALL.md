# MNEMOS Install Guide

MNEMOS ships as a small memory kernel (`mnemos-core`) plus a set of **separate,
optional subsystem distributions** that all contribute to the same `mnemos.*`
PEP 420 namespace and are **runtime-gated** — if a subsystem isn't installed,
its routes/MCP tools are simply not mounted (and return HTTP 503 with the exact
install command, never an import crash).

There are two ways to deploy: **pre-built container images** (turnkey) or
**`pip install`** (compose your own). Pick one.

---

## 1. Container images (recommended)

Published to `ghcr.io/ncz-os`. Composition is OCI-layered: each image is built
`FROM` the one above it, so the heavy base is pulled once and shared.

| Image | Contains | Arch | Pull |
|---|---|---|---|
| `ghcr.io/ncz-os/mnemos-core` | Kernel: EPIMONE persistence (SQLite default), API, MCP, in-process embedder | `amd64` + `arm64` | `docker pull ghcr.io/ncz-os/mnemos-core` |
| `ghcr.io/ncz-os/mnemos` | **Everything**: core + GRAEAE + PANTHEON + KNEMON + CHARON, one API process | `amd64` + `arm64` | `docker pull ghcr.io/ncz-os/mnemos` |
| `ghcr.io/ncz-os/mnemos-enterprise` | Everything + Oracle / Db2 / MySQL drivers | `amd64` only | `docker pull ghcr.io/ncz-os/mnemos-enterprise` |
| `ghcr.io/ncz-os/mnemos-stiphos` | STIPHOS hive service (separate process) | `amd64` + `arm64` | `docker pull ghcr.io/ncz-os/mnemos-stiphos` |

> **`mnemos` is the canonical "everything" image** and the same artifact the
> reference PYTHIA quadlet deploys.

> **Enterprise is amd64-only by policy.** The Oracle thin driver would run on
> arm64, but `ibm_db` (Db2) has no reliable arm64 wheel and the enterprise
> audience runs x86. Other arches (Power/`ppc64le`, LinuxONE/`s390x`,
> Ampere/`arm64`) are available on request with sponsor-provided build + CI
> resources, because the native drivers need validation on that hardware.

### Run the everything image (SQLite, zero config)

```bash
docker run --rm -p 5002:5002 -v mnemos-data:/data ghcr.io/ncz-os/mnemos:latest
# → http://localhost:5002/health
```

### Run against an external database (runtime backend selection)

The backend is chosen by `MNEMOS_DATABASE_DSN` at runtime — no rebuild:

```bash
# PostgreSQL + pgvector
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='postgres://user:pass@host:5432/mnemos' \
  ghcr.io/ncz-os/mnemos:latest

# Oracle Database 26ai (thin mode — works on the everything image, no enterprise image needed)
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='oracle://MNEMOS:pass@host:1521/ORCLPDB1' \
  ghcr.io/ncz-os/mnemos:latest

# IBM Db2 — needs the enterprise image (ibm_db driver)
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='db2://MNEMOS:pass@host:50000/MNEMOS' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# MariaDB 11.7+ — native vector search in the FREE Community edition
# (VEC_DISTANCE_COSINE + HNSW VECTOR INDEX; no extension needed). Uses the
# aiomysql driver (enterprise image). The default open-source / self-hosted
# vector backend for the MySQL family.
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='mariadb://mnemos:pass@host:3306/mnemos' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# MySQL 9.0+ — VECTOR_DISTANCE ships only in Enterprise/HeatWave (NOT
# Community; verified absent through 9.3). For open-source MySQL-family
# vector search, use MariaDB above.
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='mysql://mnemos:pass@host:3306/mnemos' \
  ghcr.io/ncz-os/mnemos-enterprise:latest
```

> **Choosing a backend.** Every backend self-provisions its full schema on
> first connect (DSN + `MNEMOS_EMBEDDING_DIM`, no installer step). For
> vector/semantic workloads the **recommended default is PostgreSQL +
> pgvector** — the most mature and predictable vector store with the broadest
> managed-service support. **MariaDB** is the best *open-source MySQL-family*
> choice (built-in vectors, no extension, free Community edition — unlike MySQL
> Community which has no `VECTOR_DISTANCE`), but its vector engine is new
> (11.4/11.7+) and less battle-tested at scale than pgvector. SQLite + sqlite-vec
> is the zero-dependency edge/dev default; Oracle 26ai and IBM Db2 12.1.5 target
> enterprise/big-iron deployments.

### Run the STIPHOS hive service (separate container)

```bash
docker run -p 8080:8080 -v stiphos-data:/data ghcr.io/ncz-os/mnemos-stiphos:latest
# → http://localhost:8080/health
```

---

## 2. pip install (compose your own)

The PyPI/installable package is **`mnemos-core`** (there is no `mnemos-os`
dist — `mnemos` is the *image* name, not a pip package). Subsystems are
separate distributions pulled in via extras:

```bash
# kernel only (SQLite)
pip install 'mnemos-core[sqlite]'

# kernel + reasoning
pip install 'mnemos-core[graeae]'

# everything (matches the `mnemos` image) — installs the four add-on dists
pip install 'mnemos-core[server]'

# everything + enterprise drivers
pip install 'mnemos-core[full,enterprise]'
```

> **Do not use `[full]` on arm64 hosts** — `full` no longer pulls the
> Intel-only `openvino` accelerator. Use `[server]` (which is
> `nats,persephone,pantheon,knemon,graeae,charon`) for an arch-neutral
> everything install, and add accelerators per host (`[openvino]`/`[cuda]`/`[amd]`)
> only where supported.

### Subsystem → distribution map

| Extra | Pulls distribution | Subsystem |
|---|---|---|
| `pantheon` | `mnemos-pantheon` | PANTHEON model catalog/facade |
| `knemon` | `mnemos-knemon` | KNEMON cost/model routing + ledger |
| `graeae` | `mnemos-graeae` | GRAEAE multi-muse reasoning bus |
| `charon` | `mnemos-charon` | CHARON portability (MPF import/export, migrate-in, Docling) |
| `oracle` | `oracledb` | Oracle Database 26ai backend (thin) |
| `db2` | `ibm_db` | IBM Db2 12.1.5 backend |
| `mysql` | `aiomysql` | MySQL 9.0+ backend (Enterprise/HeatWave for vectors) |
| `mysql` | `aiomysql` | **MariaDB 11.7+** backend too — same `aiomysql` driver; use a `mariadb://` DSN. Vector search is in the free Community edition (no extra driver). |

STIPHOS (the hive) is the standalone **`mnemos-stiphos`** distribution — it is
NOT an extra of core and is NOT in the everything image; install/run it
separately:

```bash
pip install 'mnemos-stiphos[mcp]'
uvicorn mnemos.hive_mind.service:app --host 0.0.0.0 --port 8080
```

### Bundles

| Bundle | Expands to |
|---|---|
| `edge` | `aiosqlite`, `sqlite-vec` |
| `server` | `nats`, `persephone`, `pantheon`, `knemon`, `graeae`, `charon` |
| `ml` | `morpheus`, `kronos`, `apollo`, `artemis`, `hot` |
| `full` | `server` + ML + `edge` + all four add-on dists |
| `enterprise` | `oracle`, `db2`, `mysql` |

### Adding a subsystem later

```bash
pip install 'mnemos-core[charon]'   # or any extra
systemctl restart mnemos
mnemos doctor                        # reports installed extras/bundles
```

---

## Backend selection precedence

Highest precedence first:

1. `MNEMOS_DATABASE_DSN` (preferred — single DSN, scheme picks the backend)
2. Per-backend env vars: `ORACLE_DSN`, `DB2_DSN`, `PG_HOST`/`PG_DATABASE`/…
3. `MNEMOS_PERSISTENCE_BACKEND` / `PG_BACKEND` (`postgres`|`sqlite`|`oracle`|`db2`|`mysql`|`mariadb`)
4. `MNEMOS_PROFILE` defaults (`server` → postgres, `edge`/`dev` → sqlite)

DSN scheme detection:

| Scheme | Backend |
|---|---|
| `postgres://…` / `postgresql://…` | PostgreSQL + pgvector |
| `oracle://…` / `oracle+oracledb://…` | Oracle Database 26ai |
| `db2://…` / `ibm_db://…` | IBM Db2 12.1.5 |
| `mysql://…` | MySQL 9.0+ |
| `sqlite:///…` | SQLite + sqlite-vec |

---

## Enterprise backends (Oracle Database 26ai + IBM Db2 12.1.5)

Both implement the same `PersistenceBackend` ABC (EPIMONE,
`mnemos/persistence/base.py`) and are exercised by `tests/test_persistence_parity.py`.

### Oracle Database 26ai

The `oracledb` driver is **thin mode by default** — no Oracle Instant Client
required, so Oracle works on the plain `mnemos` everything image (and on arm64).
Vector column is `VECTOR(768, FLOAT32)`; index is HNSW INMEMORY NEIGHBOR GRAPH
(requires Database In-Memory). Migration set: `db/migrations_oracle/`.

Pool tuning env (Oracle eng review, 2026-05-21):

- `MNEMOS_ORACLE_POOL_MIN` (default `2`), `MNEMOS_ORACLE_POOL_MAX` (default `10`),
  `MNEMOS_ORACLE_POOL_INCREMENT` (default `1`)
- `MNEMOS_ORACLE_STMT_CACHE_SIZE` (default `20`)
- `MNEMOS_ORACLE_POOL_ACQUIRE_TIMEOUT` (default `60`)
- `MNEMOS_ORACLE_PDB` — issues `ALTER SESSION SET CONTAINER=<pdb>` when DSN points at CDB$ROOT
- `MNEMOS_ORACLE_DRCP=YES` — Database Resident Connection Pooling (`cclass='MNEMOS'`, `purity=SELF`)
- `MNEMOS_ORACLE_THICK=YES` — `oracledb.init_oracle_client()` before pool creation (needs Instant Client)
- `MNEMOS_VECTOR_DIM_MAX` (default `4096`) — embedding-dim cap on the vector search path

### IBM Db2 12.1.5

Needs the `mnemos-enterprise` image (or `pip install 'mnemos-core[db2]'` on a
host with the Db2 CLI driver). `Db2Backend` runs on Db2 Oracle Compatibility
Mode and overrides `semantic_search` with native Db2 SQL so the DiskANN index
engages. Vector column `VECTOR(768, FLOAT32)`; `VECTOR_DISTANCE(..., EUCLIDEAN)`
+ `FETCH APPROX FIRST K ROWS ONLY`.

- `db2set DB2_VECTOR_INDEXING=YES` (then `db2stop && db2start`) is **required**
  before the DiskANN index can be created/engaged. `mnemos doctor` surfaces this
  via `Db2Backend.is_vector_indexing_enabled`.
- `MNEMOS_DB2_VECTOR_INDEX`: `approx` (default, engages DiskANN) | `exact` (parity/debug)

See [docs/db2-eap-recipe-2026-05-20.md](db2-eap-recipe-2026-05-20.md) and
[docs/db2-oracle-ee-test-plan.md](db2-oracle-ee-test-plan.md).

---

## Migrating between backends (CHARON)

CHARON's MPF v0.1 export is backend-agnostic and lossless for native rows:

```bash
# 1. Export from the current backend
mnemos export --format mpf --out /tmp/mnemos-backup.mpf.json
# 2. Point at the new DSN (driver must be installed — enterprise image for Db2)
export MNEMOS_DATABASE_DSN='oracle://MNEMOS:pass@host:1521/service_name'
# 3. Apply migrations on the new backend
mnemos install --profile server
# 4. Import
mnemos import --from mpf /tmp/mnemos-backup.mpf.json
```

See [docs/MEMORY_EXPORT_FORMAT.md](MEMORY_EXPORT_FORMAT.md).

---

## For agents

Machine-readable install instructions (which dist/image to choose for a
requested module set, with copy-paste commands) live in
[`AGENTS.md`](../AGENTS.md) at the repo root.
