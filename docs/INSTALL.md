# MNEMOS Install Guide

MNEMOS keeps the memory kernel small by default. Subsystems install through
pip extras, and common deployment shapes are available as named bundles so an
operator picks a deployment shape instead of hand-selecting every subsystem.

## Quick Matrix

| Install | Command | Use it for |
|---|---|---|
| Core | `pip install mnemos-os==6.0.0rc1` | Memory CRUD, search, version DAG, federation, auth/RLS, GRAEAE, MCP, webhooks |
| Edge | `pip install 'mnemos-os[edge]==6.0.0rc1'` | SQLite-only edge devices with `aiosqlite` and `sqlite-vec` |
| Server | `pip install 'mnemos-os[server]==6.0.0rc1'` | Production Postgres deployments with NATS, PERSEPHONE, and PANTHEON |
| ML | `pip install 'mnemos-os[ml]==6.0.0rc1'` | Compression-heavy and dream-state-active deployments |
| Interop | `pip install 'mnemos-os[interop]==6.0.0rc1'` | Cross-platform agent fleets using the KNOSSOS/MemPalace shim |
| Full | `pip install 'mnemos-os[full]==6.0.0rc1'` | All optional MNEMOS subsystems |
| Oracle (enterprise) | `pip install 'mnemos-os[oracle]'` (or source on `feat/oracle-port`) | Oracle Database 26ai backend (HNSW INMEMORY NEIGHBOR GRAPH, JSON Duality, TDE) |
| Db2 (enterprise) | `pip install 'mnemos-os[db2]'` (or source on `feat/oracle-port`) | IBM Db2 12.1.5 backend (DiskANN VECTOR(768, FLOAT32)) |
| Enterprise (both) | `pip install 'mnemos-os[server,enterprise]'` | server bundle + Oracle + Db2 drivers |

For source installs, use the same extras against the editable package:

```bash
python -m pip install -e '.[dev,server,ml]'
# Enterprise backends (Oracle / Db2) from the feat/oracle-port branch:
python -m pip install -e '.[dev,server,enterprise]'
```

## Mix And Match

Extras compose normally:

```bash
pip install 'mnemos-os[server,ml]==6.0.0rc1'
pip install 'mnemos-os[edge,interop]==6.0.0rc1'
```

`server,ml` is the usual production-plus-dream-state shape: Postgres + NATS +
PERSEPHONE/PANTHEON plus MORPHEUS/KRONOS/APOLLO/ARTEMIS/hot-path acceleration.

## A La Carte Extras

| Extra | Subsystem | Adds |
|---|---|---|
| `build` | PyInstaller build support | `pyinstaller>=6.0`, `sqlite-vec` |
| `docling` | Document parsing/import support | `docling>=2.5.0`, `docling-core>=2.0.0`, `pillow>=10.0.0` |
| `tracing` | OpenTelemetry tracing | `opentelemetry-api>=1.27.0`, `opentelemetry-sdk>=1.27.0`, `opentelemetry-exporter-otlp-proto-http>=1.27.0` |
| `structlog` | Structured JSON logging | `structlog>=25.0.0` |
| `sqlite` | SQLite persistence support | `aiosqlite>=0.20.0`, `sqlite-vec>=0.1.6` |
| `morpheus` | `mnemos/domain/morpheus`, MORPHEUS routes and workers | `numpy>=1.24` |
| `persephone` | PERSEPHONE archival routes and worker | `zstandard>=0.25` |
| `pantheon` | PANTHEON facade routes and IRIS MCP tools | no additional dependency |
| `kronos` | KRONOS admin routes and MCP tools | `numpy>=1.24` |
| `kronos-gpu` | KRONOS GPU acceleration | `cupy>=12` |
| `knossos` | KNOSSOS phase-1 stdio/MemPalace shim | no additional dependency |
| `apollo` | APOLLO compression engine | no additional dependency |
| `artemis` | ARTEMIS compression engine | `networkx>=3.3`, `scipy>=1.11` |
| `nats` | NATS substrate and routing-audit consumer | `nats-py>=2.14.0` |
| `hot` | Optional Rust hot-path wheel | `mnemos-hot>=0.2.0` |
| `edge` | Edge deployment bundle | `aiosqlite>=0.20.0`, `sqlite-vec>=0.1.6` |
| `server` | Server deployment bundle | `mnemos-os[nats,persephone,pantheon]` |
| `ml` | ML deployment bundle | `mnemos-os[morpheus,kronos,apollo,artemis,hot]` |
| `interop` | Interop deployment bundle | `mnemos-os[knossos]` |
| `full` | Full deployment bundle | `mnemos-os[morpheus,persephone,pantheon,kronos,knossos,apollo,artemis,nats,hot,edge]` |
| `semantic` | CPU semantic scoring | `fastembed>=0.3.0` |
| `gpu` | NVIDIA CUDA semantic scoring | `fastembed-gpu>=0.3.0` |
| `phi` | Intel iGPU semantic scoring | `openvino-genai>=2024.4.0`, `fastembed>=0.3.0` |
| `dev` | Development/test tooling | `import-linter>=2.0.0`, `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`, `pytest-cov>=5.0.0`, `ruff>=0.5.0` |
| `oracle` | Oracle Database 26ai backend driver | `oracledb>=2.0` |
| `db2` | IBM Db2 12.1.5 backend driver | `ibm_db>=3.2` |
| `enterprise` | Enterprise backend bundle | `mnemos-os[oracle,db2]` |

## Bundle Contents

| Bundle | Expands to |
|---|---|
| `edge` | `aiosqlite`, `sqlite-vec` |
| `server` | `nats`, `persephone`, `pantheon` |
| `ml` | `morpheus`, `kronos`, `apollo`, `artemis`, `hot` |
| `interop` | `knossos` |
| `full` | `morpheus`, `persephone`, `pantheon`, `kronos`, `knossos`, `apollo`, `artemis`, `nats`, `hot`, `edge` |

## Adding An Extra Later

Upgrade the existing environment with the extra and restart MNEMOS:

```bash
pip install 'mnemos-os[persephone]==6.0.0rc1'
systemctl restart mnemos
```

For editable installs:

```bash
python -m pip install -e '.[persephone]'
```

After restart, `mnemos doctor` reports which extras and bundles are installed.
MCP tools for unavailable optional subsystems are not advertised in `tools/list`.

## Enterprise Backends (Oracle Database 26ai + IBM Db2 12.1.5 EAP)

The `feat/oracle-port` branch adds Oracle Database 26ai and IBM Db2 12.1.5 as
first-class persistence backends alongside PostgreSQL and SQLite. Both
implement the same `PersistenceBackend` ABC
(`mnemos/persistence/base.py`) and are exercised by the same
`tests/test_persistence_parity.py` suite (with backend arms gated by
env vars; see below).

### Backend selection precedence

The runtime selects a backend from (highest precedence first):

1. `MNEMOS_DATABASE_DSN` (preferred — single DSN selector, scheme picks backend)
2. Per-backend env vars: `ORACLE_DSN`, `DB2_DSN`, `PG_HOST`/`PG_DATABASE`/...
3. `MNEMOS_PERSISTENCE_BACKEND` or `PG_BACKEND` (`postgres` | `sqlite` | `oracle` | `db2`)
4. `MNEMOS_PROFILE` defaults (`server` → postgres, `edge`/`dev` → sqlite)

DSN scheme detection:

| Scheme | Backend |
|---|---|
| `postgres://…` / `postgresql://…` | PostgreSQL + pgvector |
| `oracle://…` / `oracle+oracledb://…` | Oracle Database 26ai (`OracleBackend`) |
| `db2://…` / `ibm_db://…` | IBM Db2 12.1.5 (`Db2Backend`) |
| `sqlite:///…` | SQLite + sqlite-vec |

### Oracle Database 26ai

Requires Oracle Database 26ai with the VECTOR type + HNSW INMEMORY NEIGHBOR
GRAPH feature. Verified on Free Edition (Free 26ai container image)
and Enterprise Edition 23.26.1; SE2 carries the VECTOR type but
HNSW INMEMORY availability is edition / `VECTOR_MEMORY_SIZE`
dependent — confirm via `SELECT * FROM v$vector_memory_pool_summary`
before relying on the index. See
[docs/db2-oracle-ee-test-plan.md](db2-oracle-ee-test-plan.md) for
container-based EE topology and
[docs/oracle-port-status.md](oracle-port-status.md) for the current
repository-surface coverage.

```bash
# 1. Install the driver
pip install 'mnemos-os[oracle]'         # when published
python -m pip install -e '.[oracle]'    # from source on feat/oracle-port

# 2. Point MNEMOS at the database
export MNEMOS_DATABASE_DSN='oracle://MNEMOS:<password>@127.0.0.1:1521/ORCLPDB1'

# 3. Apply the Oracle migration set (backend is selected by MNEMOS_DATABASE_DSN above)
mnemos install --profile server

# 4. Verify
mnemos doctor
mnemos serve --profile server
```

Notes:

- The `oracledb` driver is **thin mode by default** — no Oracle Instant Client required for
  most operations. Some Oracle features require **thick mode**; install Oracle
  Instant Client and set `MNEMOS_ORACLE_THICK=YES` if needed (the
  driver init runs at pool-construction time, so a missing Instant
  Client fails loud at startup rather than silently falling back).
- Vector column type is `VECTOR(768, FLOAT32)`; index type is HNSW INMEMORY
  NEIGHBOR GRAPH (Oracle Database 26ai requirement: enable Database In-Memory).
- Migration files: `db/migrations_oracle/` (Oracle migration set, applied in order
  starting from `0001_core_schema.sql`).
- **Pool tuning env vars (Oracle eng review, 2026-05-21):**
  - `MNEMOS_ORACLE_POOL_MIN` (default `2`) — minimum pooled sessions.
  - `MNEMOS_ORACLE_POOL_MAX` (default `10`) — maximum pooled sessions.
  - `MNEMOS_ORACLE_POOL_INCREMENT` (default `1`) — pool growth step.
  - `MNEMOS_ORACLE_STMT_CACHE_SIZE` (default `20`) — cursor cache per
    session; raise for hot statement sites.
  - `MNEMOS_ORACLE_POOL_ACQUIRE_TIMEOUT` (default `60`) — seconds
    before `acquire()` gives up; oracledb's built-in wait mode would
    otherwise block indefinitely on a saturated pool.
  - `MNEMOS_ORACLE_PDB` — when set + DSN points at CDB$ROOT, the
    per-session callback issues `ALTER SESSION SET CONTAINER = <pdb>`.
    Benign failures (e.g. ORA-65049 "already in target PDB") are
    logged at DEBUG.
  - `MNEMOS_ORACLE_DRCP=YES` — enable Database Resident Connection
    Pooling. Pool advertises `cclass='MNEMOS'` + `purity=SELF`;
    requires `EXECUTE DBMS_CONNECTION_POOL.START_POOL` on the server
    side.
  - `MNEMOS_ORACLE_THICK=YES` — call `oracledb.init_oracle_client()`
    before pool creation. Requires Oracle Instant Client on the host.
- **Vector input validation env var:**
  - `MNEMOS_VECTOR_DIM_MAX` (default `4096`) — cap on embedding
    dimensionality accepted by the Oracle / Db2 `semantic_search`
    path. NaN / Inf elements are always rejected. Set this only if
    you genuinely need wider embeddings.

### IBM Db2 12.1.5

Requires Db2 12.1.5 (EAP or GA). See
[docs/db2-eap-recipe-2026-05-20.md](db2-eap-recipe-2026-05-20.md) for the
EAP container recipe and the same file for the GA-equivalent repackage
path on June 6, 2026 GA.

> **Performance posture.** The Db2 backend ships on top of Db2's
> **Oracle Compatibility Mode** (`DB CFG ORA_COMPATIBILITY ON` plus
> `ENABLE_ORACLE_COMPATIBILITY=true` in the container env).
> `Db2Backend` subclasses `OracleBackend` and inherits the Oracle SQL
> surface verbatim; the cursor layer rewrites Oracle tokens
> (`SYSTIMESTAMP`→`CURRENT TIMESTAMP`, `:name` binds → `?` positional,
> etc.) at query time. This carries per-query parse-time overhead and
> the Db2 optimizer does not see native dialect tokens directly. The
> exception is `Db2MemoryRepository.semantic_search`, which is
> overridden with native Db2 SQL so the DiskANN index actually engages.
> A full native Db2 dialect port (drop Oracle subclassing + compat
> mode dependency) is tracked on the v6.x roadmap
> ([docs/v6.1-roadmap.md](v6.1-roadmap.md) items #44-46).

```bash
# 1. Install the driver
pip install 'mnemos-os[db2]'            # when published
python -m pip install -e '.[db2]'       # from source on feat/oracle-port

# 2. Enable native vector indexing in the Db2 instance
db2set DB2_VECTOR_INDEXING=YES
db2 force application all && db2stop && db2start

# 3. Point MNEMOS at the database
export MNEMOS_DATABASE_DSN='db2://MNEMOS:<password>@127.0.0.1:50000/MNEMOS'

# 4. Apply the Db2 migration set (backend is selected by MNEMOS_DATABASE_DSN above)
mnemos install --profile server

# 5. Verify
mnemos doctor
mnemos serve --profile server
```

Notes:

- The `ibm_db` driver requires the DB2 CLI (`clidriver` / `libdb2`) at install
  time on the build host. Pre-built wheels exist for common Linux + macOS
  platforms.
- Vector column type is `VECTOR(768, FLOAT32)`; index uses DiskANN with
  the `VECTOR_DISTANCE` function. Runtime `semantic_search` is overridden
  in `Db2MemoryRepository` to emit
  `VECTOR_DISTANCE(..., EUCLIDEAN)` + `FETCH APPROX FIRST K ROWS ONLY`
  so the app path engages the DiskANN index (not just the bench
  harness in `scripts/bench_v4.py`). For L2-normalized embeddings
  (MNEMOS default) the EUCLIDEAN top-K ordering is identical to COSINE.
  See `mnemos/persistence/db2.py` for the Oracle → Db2 SQL-translation
  layer + the native `semantic_search` override.
- `MNEMOS_DB2_VECTOR_INDEX` env var toggles vector-index engagement:
  - `approx` (default) — `FETCH APPROX FIRST` + EUCLIDEAN; engages the
    DiskANN index. Recall@10 vs exact scan is typically ≥ 0.95 for
    normalized embeddings.
  - `exact` — `FETCH FIRST` + EUCLIDEAN; exact scan, no index
    engagement. Use for parity / debugging.
- `DB2_VECTOR_INDEXING=YES` registry variable (set via `db2set`) is
  REQUIRED before the migration can create the DiskANN index and
  before `FETCH APPROX FIRST` can engage it. The `Db2Backend.open`
  startup probe logs a clear WARNING when this variable is missing
  or not set to `YES`; the warning includes the operator action
  (`db2set DB2_VECTOR_INDEXING=YES && db2stop && db2start`).
  `mnemos doctor` surfaces the same state via
  `Db2Backend.is_vector_indexing_enabled`.

### Backend-gated tests

`tests/test_persistence_parity.py` enumerates backend arms based on
which env vars are set:

| Env var | Backend arm enabled |
|---|---|
| `MNEMOS_TEST_DB` (PG URL) | `postgres` |
| `ORACLE_DSN` | `oracle` |
| `DB2_DSN` | `db2` |
| (always) | `sqlite` |

`tests/test_oracle_live.py` and `tests/test_db2_live.py` run a minimal
live parity probe per backend and skip cleanly when the matching env
var is absent.

## Migration From Earlier v5 Installs

Before subsystem modularization, `pip install mnemos-os==5.0.0` behaved like an
all-bundled install. After this change it is core-only.

If you were on v5.0.0 and want the old all-bundled behavior:

```bash
pip install 'mnemos-os[full]==6.0.0rc1'
```

If you only need production server features:

```bash
pip install 'mnemos-os[server]==6.0.0rc1'
```

### Migrating to Oracle Database 26ai or Db2 12.1.5

To migrate an existing PostgreSQL or SQLite install to an enterprise
backend without losing data:

```bash
# 1. Export from the current backend via CHARON
mnemos export --format mpf --out /tmp/mnemos-backup.mpf.json

# 2. Install the new driver
pip install 'mnemos-os[oracle]'    # or [db2]

# 3. Point MNEMOS at the new DSN
export MNEMOS_DATABASE_DSN='oracle://MNEMOS:<password>@host:1521/service_name'
# or:  MNEMOS_DATABASE_DSN='db2://MNEMOS:<password>@host:50000/dbname'

# 4. Apply migrations on the new backend (backend selected by MNEMOS_DATABASE_DSN above)
mnemos install --profile server

# 5. Import the snapshot
mnemos import --from mpf /tmp/mnemos-backup.mpf.json
```

The CHARON export format (MPF v0.1) is backend-agnostic and lossless
for native rows; see [docs/MEMORY_EXPORT_FORMAT.md](MEMORY_EXPORT_FORMAT.md)
and the round-trip tests in `tests/test_persistence_parity.py`.

Missing subsystem routes return HTTP 503 with the exact install command. Missing
MCP tools are filtered out of `tools/list`, and optional workers no-op cleanly
when their extra is unavailable.
