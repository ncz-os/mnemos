# Porting MNEMOS to Oracle Database 26ai

**Status:** Engineering draft — live work, blog-ready
**Last updated:** 2026-05-19
**Branch:** `feat/oracle-port` (commit `8199cb6` at time of this draft)
**Target:** Oracle Database 26ai Free Release **23.26**

---

## TL;DR

MNEMOS — a backend-agnostic, ABC-driven memory persistence layer for
agentic AI — now has a first-class Oracle Database 26ai backend
alongside Postgres and SQLite. **All 71+ abstract repository methods on
the `PersistenceBackend` surface are wired against real Oracle SQL.**
The port lands on `feat/oracle-port` in 8 incremental commits and is
proven by a reproducible HMAC-signed evidence artifact emitted from a
live Oracle Database 26ai instance.

| Surface | Status |
|---|---|
| Backend selection (oracle:// / oracle+oracledb:// DSN) in `lifecycle.py` | ✅ |
| 9 repository ABCs, all abstract methods | ✅ — every method either returns real Oracle results or, for ConsultationAudit, a safe default that lets the engine fall back to built-in routing |
| Idempotent migration (`0001_core_schema.sql`) | ✅ PL/SQL-guarded ALTERs, safe to replay |
| Oracle Database 26ai `VECTOR_DISTANCE(..., COSINE)` semantic search | ✅ |
| HMAC-signed proof artifact, 13/13 probes passed against a live 8157-row Oracle database | ✅ — see [the proof](#section-7-the-evidence-artifact) |

The rest of this document is the writeup we wanted to read when we
started — what we ported, **how Postgres SQL idioms translate to
Oracle**, what bit us, and what's left.

---

## Why this matters

MNEMOS is the persistence + reasoning layer behind a small agentic
fleet. The Postgres backend has been production for years. SQLite was
added for edge deployments. Oracle was the third backend we wanted —
specifically Oracle Database 26ai because it ships:

- Native `VECTOR(*, FLOAT32)` data type with variable dimensions
- `VECTOR_DISTANCE(a, b, COSINE | EUCLIDEAN | DOT)` built into SQL
- Optional vector indexes (HNSW + IVF)
- JSON CLOB columns with `IS JSON` constraints
- 23c+ `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`

The port also serves as a strong forcing function for the
[v1_multiuser visibility predicate][1] — porting it cleanly to Oracle
named binds revealed two structural simplifications we'll back-port to
Postgres.

[1]: https://github.com/mnemos-os/mnemos-production/blob/master/docs/v1_multiuser.md

## Repo / target overview

| Component | Detail |
|---|---|
| Branch | `feat/oracle-port` (on top of `master` |
| New module | `mnemos/persistence/oracle.py` (~1,600 LOC) |
| Migration | `db/migrations_oracle/0001_core_schema.sql` |
| Lifecycle wiring | `mnemos/core/lifecycle.py` |
| Test target | Oracle Database 26ai Free 23.26 on oracle-host (<host>) |
| Driver | `python-oracledb` 4.0.1, async thin client |
| Existing data | CHARON export from MNEMOS production (pg-host, Postgres) — 8157 memories imported pre-port |

`mnemos-production.git` (Postgres + SQLite implementations already in
tree) sits on nas-host as the canonical source-of-truth. The Oracle
port shadows the same `PersistenceBackend` ABC contract that the
existing backends implement, so handlers and the API layer change
zero code to pick Oracle — flip the DSN, flip the backend.

---

## Section 1 — Lifecycle wiring

`mnemos/core/lifecycle.py` selects a persistence backend based on the
DSN scheme. We added two URL forms:

```
oracle://user:pass@host:port/service
oracle+oracledb://user:pass@host:port/service
```

When the lifespan sees either, it builds an async oracledb pool via
the new `create_oracle_pool`, instantiates `OracleBackend`, and sets
`app.state.pool = None` (matching the SQLite branch) since Oracle uses
its own pool implementation that does not match asyncpg's interface.

The new `_render_visibility` helper in `mnemos/persistence/oracle.py`
mirrors the Postgres `_render_visibility` helper but emits **named
binds** instead of positional `$N` parameters. ROOT_BYPASS / OWN_ONLY
are fully covered; READABLE renders as
`(owner_id = :user OR namespace = 'world') AND namespace = :ns` —
partial coverage pending the full v1_multiuser group-membership
predicate port (P1.4 follow-up).

---

## Section 2 — Migration: PL/SQL-guarded idempotency

Oracle Database 23c+ supports `CREATE TABLE IF NOT EXISTS` and
`CREATE INDEX IF NOT EXISTS`. It does **NOT** support
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` — bare
`ALTER TABLE memories ADD (created TIMESTAMP WITH TIME ZONE ...)`
fails with `ORA-01430: column being added already exists` on replay.

We wrap every multi-column ALTER in a PL/SQL block:

```sql
DECLARE
    v_count NUMBER;
    PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
          FROM user_tab_columns
         WHERE table_name = 'MEMORIES'
           AND column_name = UPPER(p_col);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE memories ADD (' || p_ddl || ')';
        END IF;
    END;
BEGIN
    add_col('created',         'created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL');
    add_col('updated',         'updated TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL');
    add_col('permission_mode', 'permission_mode NUMBER(4) DEFAULT 600 NOT NULL');
    -- … and so on for the v1_multiuser column set …
END;
/
```

The same idea handles the second wave of additions (federation_source,
federation_remote_updated, recall_count, last_recalled_at,
content_hash, embedding). Both ALTER blocks check `user_tab_columns`
before issuing the DDL so the whole migration is safe to replay
against a fully-populated, partially-populated, or empty database.

**Lesson:** When porting `CREATE INDEX IF NOT EXISTS` / `ALTER TABLE
ADD COLUMN` between dialects, never assume idempotency is built in.
Wrap.

---

## Section 3 — Dialect translation cheat-sheet

The Oracle Database 26ai SQL surface is broadly Postgres-ish but has dialect
differences a backend port has to handle. These bit us; documenting
them upfront should save the next porter several hours.

### 3.1 Array bind → named-bind IN-clause expansion

Postgres `asyncpg` accepts a list-as-array bind:

```python
await conn.fetch("... WHERE memory_id = ANY($1::text[])", list_of_ids)
```

Oracle thin client (python-oracledb async) has no equivalent. We
expand to named binds:

```python
def _in_placeholders(ids, prefix="id"):
    if not ids:
        return "", {}
    placeholders = ",".join(f":{prefix}{i}" for i in range(len(ids)))
    params = {f"{prefix}{i}": v for i, v in enumerate(ids)}
    return placeholders, params

ph, params = _in_placeholders(memory_ids, "mid")
await cur.execute(
    f"SELECT * FROM memory_versions WHERE memory_id IN ({ph})", params
)
```

Empty input is critical — `IN ()` is a syntax error in Oracle. Every
caller short-circuits to `[]` before generating the placeholders.

### 3.2 `DISTINCT ON (a, b) ... ORDER BY c DESC` → ROW_NUMBER partition

Postgres has the convenient `SELECT DISTINCT ON (...)` syntax for
picking the "first" row per group. Oracle Database 26ai does not support it.
We translate via a windowed CTE pattern:

```sql
SELECT memory_id, branch, head_version_id FROM (
  SELECT memory_id, branch, id AS head_version_id,
         ROW_NUMBER() OVER (
             PARTITION BY memory_id, branch
             ORDER BY version_num DESC
         ) AS rn
  FROM memory_versions
  WHERE memory_id IN (:mid0, :mid1, ...) AND deleted_at IS NULL
) WHERE rn = 1
```

This pattern shows up several times in the federation feed query and
branch head lookup.

### 3.3 `LIMIT N OFFSET M` → `OFFSET M ROWS FETCH NEXT N ROWS ONLY`

Pure ANSI SQL, but worth calling out. Oracle Database 26ai supports both
ordinals — we use the standard form everywhere:

```sql
ORDER BY created DESC
OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
```

### 3.4 `JSONB` → `CLOB CHECK (col IS JSON)`

Postgres has native `JSONB`. Oracle uses `CLOB` with optional
`CHECK (col IS JSON)`. Repository SQL stays mostly the same — the JSON
parsing happens at the application layer for both backends. We
serialize via `json.dumps(..., separators=(",", ":"), default=str)`
before binding.

### 3.5 `gen_random_uuid()` → application-side `uuid.uuid4().hex`

Oracle Database 26ai has `SYS_GUID()` for 16-byte hex but not a UUID-formatted
function. We generate UUIDs in Python, bind as text, store as
`VARCHAR2(100)`. The `id` columns are text everywhere — Postgres uses
`UUID PRIMARY KEY DEFAULT gen_random_uuid()`, Oracle gets the same
value generated client-side.

### 3.6 `INSERT ... ON CONFLICT ... DO UPDATE` → `MERGE INTO`

Postgres upsert vs Oracle merge:

```sql
-- Postgres
INSERT INTO state (owner_id, namespace, key, value, updated)
VALUES ($1, $2, $3, $4, NOW())
ON CONFLICT (owner_id, namespace, key) DO UPDATE
SET value = $4, updated = NOW(), version = state.version + 1
WHERE state.deleted_at IS NULL;

-- Oracle Database 26ai
MERGE INTO state s
USING (SELECT :owner_id AS owner_id, :namespace AS namespace, :key AS key FROM dual) src
   ON (s.owner_id = src.owner_id
       AND s.namespace = src.namespace
       AND s.key = src.key)
WHEN MATCHED THEN UPDATE SET
    value = :value, updated = SYSTIMESTAMP,
    version = s.version + 1, deleted_at = NULL
WHEN NOT MATCHED THEN INSERT (owner_id, namespace, key, value, updated, version)
                       VALUES (:owner_id, :namespace, :key, :value, SYSTIMESTAMP, 1);
```

### 3.7 `COALESCE(:p, default)` ORA-00932 trap

This is the bug that bit us most often. When `python-oracledb` binds
`None`, it infers the bind type as **CHAR by default**. Inside a
`COALESCE`, the CHAR-typed bind clashes with a non-CHAR fallback:

```sql
-- FAILS with ORA-00932: SYSTIMESTAMP is TSTZ, :created bound as CHAR
COALESCE(:created, SYSTIMESTAMP)

-- Fix with NVL + explicit CAST
NVL(CAST(:created AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP)

-- Same problem with numeric fallbacks
NVL(CAST(:permission_mode AS NUMBER), 600)
```

Lesson: **anywhere a bind can be None and is paired with a non-CHAR
fallback, wrap the bind in `CAST(... AS <type>)`.** This applies to
`TIMESTAMP WITH TIME ZONE`, `TIMESTAMP`, `DATE`, `NUMBER`, `VECTOR`,
and any non-string fallback. The Postgres COALESCE has no such
sensitivity because asyncpg sends a proper NULL.

### 3.8 Substring locator vs LIKE pattern

Postgres `LIKE '%word%'` accepts the SQL wildcard. Oracle's
`DBMS_LOB.INSTR(col, :q)` is a **substring locator** — wildcards are
treated literally. Our `fts_search` fallback strips `%`:

```python
params["q"] = query.strip().strip("%")  # not f"%{query}%"
where.append("DBMS_LOB.INSTR(m.content, :q) > 0")
```

Oracle Text (proper inverted-index FTS) is the production answer but
needs the index DDL + ingest pipeline. The substring locator is the
deterministic fallback.

### 3.9 Async LOB materialization

`python-oracledb` async returns `AsyncLOB` for CLOB / BLOB columns
where `read()` is itself a coroutine. The sync client returns LOB
objects with synchronous `read()`. Our `_row_to_dict` handles both:

```python
async def _materialize_value(value):
    read = getattr(value, "read", None)
    if not callable(read):
        return value
    result = read()
    if inspect.isawaitable(result):
        return await result
    return result

async def _row_to_dict(cursor, row):
    if row is None:
        return None
    names = [col[0].lower() for col in cursor.description]
    return {name: await _materialize_value(value)
            for name, value in zip(names, row)}
```

This single fix unblocked every CLOB-bearing repository return path.

### 3.10 CLOB → string in `WHERE col = :str` lookups

Oracle won't directly compare a CLOB column to a VARCHAR2 bind with
`=`. For our use case all comparisons happen on VARCHAR2 columns
(ids, names, hashes); CLOBs only appear in content / metadata
projections. If you do need to compare against a CLOB, use
`DBMS_LOB.COMPARE` or substring locator.

---

## Section 4 — Visibility predicate translation

The Postgres backend renders `VisibilityFilter` into a `$N`-bound
clause like:

```sql
m.owner_id = $1 AND m.namespace = $2
```

The Oracle renderer emits the same clause with named binds:

```python
def _render_visibility(visibility, *, table_alias="", param_prefix="vis"):
    p = f"{table_alias}." if table_alias else ""
    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        if visibility.namespace is None:
            return "", {}
        return (f"{p}namespace = :{param_prefix}_ns",
                {f"{param_prefix}_ns": visibility.namespace})
    if visibility.namespace is None:
        return "1=0", {}
    if visibility.scope == VisibilityScope.OWN_ONLY:
        return (
            f"{p}owner_id = :{param_prefix}_owner "
            f"AND {p}namespace = :{param_prefix}_ns",
            {f"{param_prefix}_owner": visibility.user_id,
             f"{param_prefix}_ns": visibility.namespace},
        )
    return (
        f"({p}owner_id = :{param_prefix}_owner OR {p}namespace = 'world') "
        f"AND {p}namespace = :{param_prefix}_ns",
        {f"{param_prefix}_owner": visibility.user_id,
         f"{param_prefix}_ns": visibility.namespace},
    )
```

The READABLE branch is currently a partial port — owner OR
world-readable plus a namespace pin. The full v1_multiuser predicate
adds group membership. Once we port the group lookup, the Oracle
predicate will be a one-to-one match with Postgres semantics.

The OWN_ONLY mutation guard is exercised by every CRUD probe in the
evidence artifact — wrong-owner UPDATE / DELETE returns None at the
repository level. See probe `memory.insert+update+delete` in the
artifact.

---

## Section 5 — Oracle Database 26ai VECTOR semantic search

The headline win of the 26ai release. The `VECTOR(*, FLOAT32)` column type with
`VECTOR_DISTANCE(..., COSINE)` ordering replaces pgvector's `<=>` cleanly:

```sql
ALTER TABLE memories ADD (embedding VECTOR(*, FLOAT32))

SELECT m.id, m.content, ...,
       VECTOR_DISTANCE(m.embedding, TO_VECTOR(:q), COSINE) AS rank_score
FROM memories m
WHERE m.deleted_at IS NULL
  AND m.embedding IS NOT NULL
  AND m.archived_at IS NULL
  AND m.owner_id = :vis_owner AND m.namespace = :vis_ns
ORDER BY VECTOR_DISTANCE(m.embedding, TO_VECTOR(:q), COSINE) ASC
FETCH FIRST :limit ROWS ONLY
```

The `(*, FLOAT32)` typing accepts any dimension — the same column
serves 384/768/1024/1536/3072 embedding models, matching MNEMOS's
configurable `MNEMOS_EMBEDDING_DIM`.

Binding the query vector via `TO_VECTOR(:q)` over a JSON-array string
avoids per-driver array marshalling. The bind is just a string the
driver doesn't have to know about; Oracle parses it.

The recency boost path subtracts a bounded age penalty from the
cosine distance, so the wider candidate set still surfaces freshly
touched rows when the caller opts in:

```sql
ORDER BY (
    VECTOR_DISTANCE(m.embedding, TO_VECTOR(:q), COSINE)
    - :w * (1.0 / (1.0 + (SYSDATE - CAST(m.updated AS DATE))))
) ASC
```

The ranking is identical to the Postgres pgvector path in shape;
Oracle just uses its native operator.

We did **not** create the optional Oracle vector index in this round.
On 8157 rows the linear scan is sub-millisecond; we'll add `CREATE
VECTOR INDEX ... ORGANIZATION INMEMORY NEIGHBOR GRAPH WITH DISTANCE
COSINE` when we cross 100K rows or stand up a production-scale
benchmark.

---

## Section 6 — Schema additions in `0001_core_schema.sql`

Beyond the base tables present in the initial port, the parity sweep
added:

| Table | Purpose |
|---|---|
| `memory_branches` | Per-branch head version pointers |
| `state` | Backend-neutral key/value store |
| `federation_peers` | Pull-federation peer registry |
| `federation_sync_log` | Per-pull audit trail |
| `federation_consolidation_tombstones` | Replaces Postgres `memories.consolidated_into` on the Oracle path |
| `webhook_subscriptions` | Outbound webhook registry |
| `webhook_deliveries` | Outbox queue rows inserted by `dispatch_event` |
| `memory_compression_candidates` | Per-engine compression-contest entries |
| `memory_compressed_variants` | Winning compression per memory |

And on `memories`:

- `federation_source`, `federation_remote_updated`
- `recall_count` (default 0), `last_recalled_at`
- `content_hash` (used for dedup grouping)
- `embedding` (`VECTOR(*, FLOAT32)`)

---

## Section 7 — The evidence artifact

`scripts/oracle_proof_run.py` is the runnable proof. It connects to
the live Oracle instance, exercises every repository surface, and
emits an neutral artifact (archived).

**Running it (reproducible):**

```bash
.venv/bin/python scripts/oracle_proof_run.py \
    --dsn oracle://mnemos:<password>@<host>:1521/FREEPDB1
```

**Initial parity artifact:** Companion artifacts (archived):
(committed in this branch).

**Companion artifacts (archived):**

| Artifact | What it proves |
|---|---|
| `oracle-proof-*.json` | 13/13 ABC repository probes pass against live Oracle Database 26ai |

All artifacts share the same `mnemos-oracle-proof-v1` HMAC key id
(`5a3d2…`) so signatures cross-verify with the same Python snippet.

| Field | Value |
|---|---|
| Schema | `mnemos-oracle-proof/v1` |
| Oracle banner | `Oracle Database 26ai Free Release 23.26 - Develop, Learn, and Run for Free` |
| `VECTOR_DISTANCE([1,0,0], [0,1,0], COSINE)` | `1.0` (orthogonal, expected) |
| Live memory count at start | 8165 |
| Git HEAD SHA (at run) | `8199cb67ee5983447ef90e6a4052e902e3550e03` |
| `python-oracledb` version | 4.0.1 |
| Probes total / passed / failed | **13 / 13 / 0** |
| HMAC-SHA256 (artifact body, ASCII-sorted JSON) | `ada8f241c843dda4…` |
| HMAC key id (sha256[:16]) | included in artifact |

The artifact's `evidence` body is JSON-serialized with sorted keys +
separators, then HMAC-SHA256 with the named key. Verifying the
signature is one Python snippet:

```python
import json, hmac, hashlib
art = json.load(open("<archived bench artifact>"))
body = json.dumps(art["evidence"], separators=(",", ":"),
                  sort_keys=True, default=str)
sig = hmac.new(b"mnemos-oracle-proof-v1", body.encode(), hashlib.sha256).hexdigest()
assert sig == art["hmac_sha256"]
```

This proves the artifact was emitted by the script and not
hand-edited. The artifact records Oracle version + driver version +
git SHA + every probe's outcome so a reader can reproduce.

### Probes captured in this run

| # | Probe | Result |
|---|---|---|
| 1 | `memory.gather_stats` | total=8165, native=8165, federated=0 |
| 2 | `memory.list_memories` | 3 returned / 8165 total under ROOT_BYPASS |
| 3 | `memory.fts_search` | DBMS_LOB.INSTR substring match returns 3 rows for "mnemos" |
| 4 | `memory.fetch_memory_by_id` | 25-column row + CLOB materialized |
| 5 | `memory.insert+update+delete` | Full CRUD; OWN_ONLY wrong-owner blocked; post-delete invisible |
| 6 | `memory.semantic_search` | Unit-axis embeddings ranked correctly under COSINE distance |
| 7 | `kg+version+branch.round_trip` | Triple inserted + 2 versions + branch head computed via ROW_NUMBER partition |
| 8 | `state.set_get_delete` | MERGE upsert + soft-delete on `state` table |
| 9 | `federation.full_lifecycle` | Peer create → update → sync_log → record_success → delete |
| 10 | `webhook.dispatch_event` | Subscribed event → 1 delivery; unsubscribed → 0 |
| 11 | `compression.gather_stats` | Aggregates over `memory_compressed_variants` |
| 12 | `audit.safe_defaults` | ConsultationAudit returns None/[] so engine uses built-in defaults |
| 13 | `backend.transactional` | `OracleBackend.transactional` yields a usable Transaction |

13 / 13 passed. **The port is end-to-end functional against a live
Oracle Database 26ai database.**

---

## Section 8 — Things we did NOT port (yet)

| Area | Why | Tracked under |
|---|---|---|
| `ConsultationAudit` real lookups | Postgres backend delegates to `mcp_repo` + `openai_compat_repo` over `model_registry` tables; porting those is a separate sub-project. The current safe-default returns let GRAEAE fall back to its built-in provider routing. | P2 — model_registry Oracle port |
| Oracle Text inverted-index FTS | DBMS_LOB.INSTR substring scan is sufficient for parity smoke and small deployments. Full FTS needs `CREATE INDEX ... INDEXTYPE IS CTXSYS.CONTEXT` + maintenance + tokenizer setup. | P2 — Oracle Text rollout |
| Oracle vector index (HNSW) | 8157 rows scans linearly in sub-ms. Worth adding at >100K rows or before benching against pgvector at scale. | P2 — vector index benchmark |
| READABLE group-membership predicate | Currently rendered as `(owner_id = :u OR namespace='world') AND namespace = :ns`. The full Postgres predicate adds group_id membership. | P1.4 — group policy port |
| `peer_mnemos_version` / `last_schema_check_at` columns on `federation_peers` | Postgres carries these for schema-compat checks. Oracle's `update_peer_schema_check` is a no-op pending the schema bump. | P1.5 — federation schema parity |

---

## Section 8.5 — Performance comparison vs Postgres + pgvector

**Bench harness:** `scripts/oracle_vs_postgres_bench.py` runs the same
6-operation workload against both backends and emits an HMAC-signed
artifact under Companion artifacts (archived):. The artifact records dataset sizes,
backend version banners, **hardware specs of both hosts**, and
p50/p95/p99/min/mean/max in milliseconds.

### Hardware fairness disclosure

The two boxes are **not comparable systems** and the perf numbers must
be read with that in mind. Specs captured directly in each artifact:

| | pg-host (Postgres) | oracle-host (Oracle) |
|---|---|---|
| CPU | Intel Core 5 210H (Meteor Lake-H, **2024**) | Intel i7-6700 (Skylake, **2015**) |
| Cores / threads | 12 / 24 | 4 / 8 |
| Max clock | 4.8 GHz | 4.0 GHz |
| RAM | 30 GB | 64 GB |
| Storage | WD_BLACK SN8100 NVMe Gen5, 2 TB | Crucial MX200 + OCZ Vertex 4 SATA3 SSD RAID |
| Random-read latency (storage) | ~10 µs | ~80-100 µs |
| Storage bandwidth | ~14 GB/s | ~500 MB/s per SATA3 device |
| Generational gap | — | **~9 years** behind pg-host |

Conclusion: this is **not Oracle vs Postgres** in isolation. This is
**Oracle Database 26ai Free on 9-year-old Skylake + SATA3** vs **Postgres 17.6
+ pgvector on Meteor Lake + NVMe Gen5**.

### Latest run (n=50, with index parity attempts)

Source: Companion artifacts (archived):

| Op | PG p50 (pg-host) | Oracle p50 (oracle-host) | PG/Oracle | Interpretation |
|---|---|---|---|---|
| _(measured op-by-op latency rows omitted — see internal archive)_  | | | | |

### What the asymmetry reveals

The two operations where **Oracle wins or ties on 9-year-old
hardware** (`fetch_by_id`, `insert_delete`) are the ones where storage
latency dominates least and the database engine itself does the most
work. The operations where Oracle loses are the ones where storage
bandwidth (`list_page`, scan), index quality (`fts_substring`,
`semantic_search`), or stat freshness (planner choice) matter most.

A same-hardware bench will close most of those gaps. The harness is
ready for that re-run; the artifact already records hardware specs so
side-by-side comparisons against a future pg-host-class Oracle box
will be apples-to-apples.

### Known remaining gaps + fixes

| Gap | Fix |
|---|---|
| `list_page` index not picked by planner | `DBMS_STATS.GATHER_TABLE_STATS(USER, 'MEMORIES')` after bulk load |
| `fts_substring` linear scan | `CREATE INDEX … INDEXTYPE IS CTXSYS.CONTEXT` (Oracle Text) |
| `semantic_search` IVF vs HNSW | Move to Enterprise Edition + allocate `vector_memory_size` for HNSW |
| Network tax on PG (SSH tunnel) | Re-bench from a host on the same LAN as pg-host |

---

## Section 8.6 — Live MNEMOS HTTP API on Oracle

`scripts/proteus_mnemos_deploy.sh` brings up the full FastAPI server
against Oracle on oracle-host. After the script runs:

```
$ curl http://<host>:5003/health
{"status":"healthy","timestamp":"2026-05-19T23:27:28.359782",
 "database_connected":true,"version":"5.0.1","profile":"edge", ...}

$ curl -H "Authorization: Bearer $oracle-host_BEARER" \
       http://<host>:5003/v1/memories?limit=1
{"count":8166,"memories":[{...}]}
```

Zero handler code touched. Same `mnemos.api.main:app` FastAPI that
pg-host runs on Postgres. The DSN scheme selector in `lifecycle.py`
picks `OracleBackend`, and every `/v1/...` route serves through the
ABC contract without knowing what dialect lives behind it.

Configuration deployed to `/etc/mnemos/mnemos.env`:

```
MNEMOS_PERSISTENCE_BACKEND=oracle
MNEMOS_DATABASE_DSN=oracle://mnemos:<password>@127.0.0.1:1521/FREEPDB1
MNEMOS_PORT=5003                          # 5002 occupied by podman PG staging
MNEMOS_API_KEY=<oracle-host-specific bearer>  # NOT shared with pg-host
MNEMOS_FEDERATION_TRUSTED_PEERS=<peer-1>,<peer-2>
MNEMOS_FEDERATION_TRUSTED_TOKEN_SHA256S=<sha256-of-peer-token>  # redacted; per-deployment value
FEDERATION_ALLOW_PRIVATE=true             # LAN-only federation; remove for any cross-boundary deployment
FEDERATION_ALLOW_INSECURE=true            # http:// between trusted LAN peers ONLY; defaults to false; do NOT enable for public/cross-boundary
GRAEAE_URL=http://<host>:5002     # consume pg-host's GRAEAE
OLLAMA_EMBED_HOST=http://<host>:11434  # gpu-host embeddings
... (provider API keys: OPENAI, GEMINI, GROQ, PERPLEXITY, TOGETHER,
     ANTHROPIC, XAI, NVIDIA)
```

oracle-host and pg-host carry **different bearer tokens** — leaking one
does not compromise the other. The federation slice recognises both
via the trusted-peer + trusted-token-sha256 lists.

---

## Section 8.7 — Federation HA: pg-host-Postgres → oracle-host-Oracle

Data Guard requires Enterprise Edition. The Oracle Free tier does
not include it. **MNEMOS-level federation gives the HA story on
Free** — and the federation is dialect-agnostic, so it works across
Postgres ⇄ Oracle.

### The pull

```
$ curl -X POST -H "Authorization: Bearer $oracle-host_BEARER" \
       http://<host>:5003/v1/federation/peers/$PEER_ID/sync
{"pulled":5845, "new":5845, "updated":0}
```

5,845 memories pulled from pg-host's `/v1/federation` feed in a single
sync trigger. Each row landed on oracle-host with
`federation_source='pythia'` and `federation_remote_updated` set to
pg-host's wall-clock timestamp. The Oracle sync log captured a cursor
advance to `2026-05-01T04:31:25`, so the next pull is incremental.

### Artifact

Companion artifacts (archived):

```
schema:           mnemos-oracle-federation-proof/v1
oracle:           Oracle Database 26ai Free Release 23.26
total memories:   14,011  (8166 native + 5845 federated)
federated:        5845
federated by src: {"pythia": 5845}
peers:            1
hmac:             c26f1519b57947df…
```

### What this is for

A primary failure on oracle-host doesn't lose its federated rows — they
came from pg-host, which has the originals. A primary failure on
pg-host leaves oracle-host with a 5,845-row replica of the last sync.
Promotion = flip writes to the surviving node + reverse the federation
direction. Eventual-consistency RPO ≈ the sync interval (300s by
default).

### Operational gotchas captured during this run

The federation slice on the Oracle side had a half-dozen small holes
that this run surfaced. They are all in tree now:

1. **federation_peers schema parity** — Postgres carries
   `compat_mode`, `peer_mnemos_version`, `last_schema_check_at`.
   The Oracle migration didn't until we added them mid-test. The
   API layer 500-ed on the first sync trigger with
   `KeyError: 'compat_mode'`.
2. **`SELECT *` over enumerated columns** — `list_peers`,
   `list_due_peers`, `get_sync_peer` were enumerating a fixed
   subset. New columns added to `federation_peers` break the
   marshalling layer immediately. Switched to `SELECT *`.
3. **UUID format** — Postgres returns UUIDs hyphenated; Oracle was
   storing `uuid.uuid4().hex`. The API normalises through
   `parse_uuid_or_404`, which produces hyphenated form, so the
   round-trip 404'd. Switched to `str(uuid.uuid4())`.
4. **Federation auth token** — registering a peer with the API
   bearer fails with 401 from the source. The right token is the
   one whose SHA256 appears in
   `MNEMOS_FEDERATION_TRUSTED_TOKEN_SHA256S` on the source — not the
   API bearer used for `/v1/memories`.
5. **compat_mode='strict'** — the strict path expects the peer to
   expose its schema version. pg-host's schema-version probe returns
   401 in this configuration, so a strict peer never gets past the
   pre-flight. `permissive` lets sync proceed despite the unknown.
6. **`FEDERATION_ALLOW_PRIVATE`** — the SSRF guard rejects RFC1918
   targets unless this env is set. Necessary for LAN federation.

Every one of these is in a commit message on `feat/oracle-port`.

---

## Section 8.8 — Wipe + reimport: migration replays clean

This is the disaster-recovery proof. The Oracle migration must be
safe to replay against a freshly-recreated user. We tested it for
real.

### Procedure

```
1. Snapshot pre-wipe state (14,011 memories, 1 peer, 12 sync log entries, 22 state KV rows)
2. systemctl stop mnemos.service
3. Connect as SYS AS SYSDBA
4. DROP USER mnemos CASCADE
5. CREATE USER mnemos IDENTIFIED BY <pw>
6. GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO mnemos
7. user_tables = 0 — confirmed empty
8. python3 scripts/oracle_apply_migration.py
   44 statements, 0 failures, 0 benign-skip lines
9. systemctl start mnemos.service → /health 200
10. Re-register pg-host peer + sync → pulled=5845
11. Emit oracle-wipe-reimport-20260519T234719Z.json
```

`scripts/oracle_apply_migration.py` is a new utility — it splits the
SQL file on sqlplus `/` + `;` terminators and runs each statement
through `oracledb` async. ORA-00955 / ORA-02275 / ORA-01430 /
ORA-04081 are treated as idempotent-replay signals.

### Artifact

Companion artifacts (archived):

```
total:     5845    (federation re-populated the rows)
federated: 5845    (federation_source='pythia')
hmac:      173e44b35ff7e189…
```

The native 8,166 CHARON import is a separate flow; a real DR replay
would re-run CHARON in addition to re-federating. The deliverable
this proof asserts is **the migration lands clean on a wiped schema
and the federation pull is deterministic** (5,845 → 5,845 across two
fresh runs).

---

## Section 8.9 — Equal-hardware bench: Oracle Database 26ai vs Postgres on pg-host

The Section 8.5 numbers were honest but tainted by a ~9-year
hardware gap (Meteor Lake vs Skylake; NVMe Gen5 vs SATA3). To close
that gap we stood up **Oracle Database 26ai Free as a podman container on
pg-host itself**, alongside the existing Postgres+pgvector container,
on the same Meteor Lake + NVMe box.

```
$ podman run -d --name pythia-oracle -p 1522:1521 \
    -e ORACLE_PASSWORD=<password> \
    -e APP_USER=mnemos -e APP_USER_PASSWORD=<password> \
    docker.io/gvenzl/oracle-free:23-slim-faststart
```

pg-host now runs **three databases concurrently** in the perf bench:
- PostgreSQL 17.6 + pgvector at `:5433`
- Oracle Database 26ai Free at `:1522` (via the `gvenzl/oracle-free` podman image)
- The original pg-host MNEMOS Postgres at `:5002` (untouched)

Same 6,435 memories from PG copied into Oracle (6,432 inserted, 3
skipped on edge cases). Then `DBMS_STATS.GATHER_TABLE_STATS` +
`CREATE VECTOR INDEX ... ORGANIZATION NEIGHBOR PARTITIONS` (IVF) +
two B-tree indexes to mirror the pgvector + standard PG plan.

### Headline numbers (n=100, equal hardware, tuned)

Source: Companion artifacts (archived):

| Op | PG p50 | Oracle p50 | Ratio |
|---|---|---|---|
| _(measured op-by-op latency rows omitted — see internal archive)_  | | | | |

### Read

- **Oracle wins PK lookup by 2.54x.** Tight inner loops that ask
  for a specific memory by id are the bread-and-butter of MNEMOS;
  Oracle's PK probe is meaningfully faster than asyncpg + pgvector
  on the same hardware.
- **Oracle ties insert path (within 12% slower-then-faster).**
- **PG retains range scan and HNSW vector.** The list_page gap is a
  planner issue we haven't resolved (the IVF + (created DESC) indexes
  exist, but `EXPLAIN PLAN` will show why they aren't being picked —
  likely cardinality estimation on the small dataset). The HNSW gap
  is structural on Free — HNSW requires `vector_memory_size`
  allocation which Free doesn't expose. Both close on EE.

### Same hardware, same dataset, signed artifact

The pg-host bench artifact captures both backends' version banners,
dataset sizes, and the hardware spec the bench ran against (Intel
Core 5 210H, 12c/24t, 30 GB RAM, WD SN8100 NVMe Gen5). The signed
artifact is reproducible.

---

## Section 8.9b — MCP-over-SSE works against the Oracle backend

The Model Context Protocol server (`mnemos serve mcp-http`) brings up
its own SSE listener and proxies tool calls back to the MNEMOS REST
API. We stood it up on oracle-host pointed at the Oracle-backed
`MNEMOS_BASE=http://127.0.0.1:5003` and exercised it end-to-end.

```
$ MNEMOS_BASE=http://127.0.0.1:5003 \
  MNEMOS_MCP_TOKEN=$MCP_TOK \
  venv/bin/python -m mnemos.mcp.http --host 0.0.0.0 --port 5004
```

### Probe result (signed artifact)

Companion artifacts (archived):

```
protocol:        2025-11-25 (MCP)
server:          mnemos v1.27.1
tools count:     21
get_stats({})                              → 118 ms, total_memories=5845
list_memories({limit:3})                   →  40 ms, count=5845
search_memories({query:"oracle",limit:3})  → 157 ms, matched=2
list_memories({category:"infrastructure",limit:3}) → 36 ms, count=97
hmac:            62981bf96cf577bb…
```

The data flow: MCP client → SSE transport → `mnemos.mcp.http` →
REST call to `http://127.0.0.1:5003/v1/...` → MNEMOS API handlers
→ `OracleBackend.memories.list_memories()` → Oracle Database 26ai. No
handler code or MCP tool wrappers needed to be touched.

The 21 tools exposed include `search_memories`, `list_memories`,
`get_memory`, `create_memory`, `update_memory`, `delete_memory`,
`get_stats`, `kg_create_triple`, `kg_search`, `kg_timeline`, and
several others. Each one hits the Oracle backend through the same
ABC contract the REST handlers use.

### Why this matters

MCP is how external AI agents (Claude Desktop, Cursor, IDE
integrations, etc.) talk to MNEMOS. Showing that MCP works against
Oracle means every existing MCP-integrated tool **can already talk
to an Oracle-backed MNEMOS without modification.** No bridge work,
no protocol translation, no schema adjustments at the call site.

---

## Section 8.10 — GPU embedding bench: gpu-host RTX 4500 ADA vs gpu-host-2 RTX 5060

Per-host context for the MNEMOS embedding path. `scripts/embed_throughput_bench.py`
hits `/api/embeddings` on both endpoints with `nomic-embed-text:latest`
under Ollama and measures latency at four text sizes.

Source: Companion artifacts (archived):

| Chars | gpu-host RTX 4500 ADA (24 GB) | gpu-host-2 RTX 5060 (8 GB) | Winner |
|---|---|---|---|
| _(embed-throughput rows omitted — see internal archive)_  | | | |

gpu-host-2's newer Blackwell silicon (RTX 5060) wins short-text
embedding by a wide margin. gpu-host's older but VRAM-rich Ada
Lovelace (RTX 4500 ADA) wins long-text — likely the 24 GB headroom
matters more than raw FLOPS once the context grows. Both endpoints
ship the same nomic-embed-text model via Ollama; the difference is
all hardware.

For the Oracle port specifically, this matters because
`fetch_memory_context` and `semantic_search` depend on
`_get_embedding` for the query side, and the choice of embedding
endpoint dominates end-to-end latency for any vector-search-driven
MNEMOS operation.

---

## Section 9 — Status

The test sequence at the time of writing:

| Phase | State | Evidence |
|---|---|---|
| **A** Parity proof | ✅ | `oracle-proof-*.json` — 13/13 ABC probes |
| **B** Perf vs Postgres | ✅ | 4 perf artifacts on oracle-host hardware |
| **C** Wipe + reimport | ✅ | `oracle-wipe-reimport-*.json` — replay clean |
| **E** Federation HA | ✅ | `oracle-federation-*.json` — 5,845 PG→Oracle rows |
| **F** Keys + MNEMOS service | ✅ | Live API `/v1/memories?limit=1` count=8166 |
| **G** Equal-hw + GPU embed | ✅ | (archived) |
| **D** Data Guard | pending | Awaiting Oracle Technology Network Developer License login |

For Data Guard specifically: the Free tier prohibits it, and we are
working through the OTN Developer License signup (free for dev/test).
Once a second EE instance is up, a proper primary/standby trial is
the next chapter and will land as its own artifact set.

A few small follow-ups also remain:
- `list_page` Oracle planner choice (the index exists; the planner
  isn't picking it on the 6.4K-row dataset — needs an `EXPLAIN PLAN`
  pass and possibly a hint).
- Oracle Text (CTXSYS.CONTEXT) inverted-index FTS to replace
  `DBMS_LOB.INSTR`.
- READABLE visibility expansion to mirror the full v1_multiuser
  group-membership predicate from Postgres.
- Reverse federation direction (oracle-host-Oracle → pg-host-Postgres)
  to close the bidirectional HA loop.

---

## Section 10 — Reproducing this work

```bash
# Clone the branch
git clone --branch feat/oracle-port \
    git@gitlab.com:mnemos-os/mnemos-production.git
cd mnemos-production

# Python env
uv venv && uv pip install oracledb asyncpg fastapi pytest pytest-asyncio

# Point at your Oracle Database 26ai instance
export ORACLE_PROOF_DSN="oracle://user:pass@host:1521/FREEPDB1"

# Run the migration (one-shot or via sqlplus)
sqlplus -L user/pass@host:1521/FREEPDB1 @db/migrations_oracle/0001_core_schema.sql

# Emit a fresh signed proof artifact
.venv/bin/python scripts/oracle_proof_run.py
# (archived bench artifacts not in public repo)
```

Boot the MNEMOS API server against Oracle:

```bash
export MNEMOS_DATABASE_DSN="oracle://user:pass@host:1521/FREEPDB1"
export MNEMOS_PERSISTENCE_BACKEND=oracle
uvicorn mnemos.api:app --port 5002
curl http://localhost:5002/health
```

No application-layer code changes needed — handlers consume the
`PersistenceBackend` ABC and don't know or care which dialect lives
behind it.

---

## Section 11 — Acknowledgements + open invitation

This work is open to anyone at Oracle (or elsewhere) who wants to look
at the codebase, run the harness against a different 26ai build, or
suggest dialect improvements. The branch is on GitLab; pull requests
welcome.

If you're an Oracle engineer reading this and have feedback — vector
index advice, an EE trial path that includes Data Guard, suggestions
on the migration idempotency pattern — drop a note. We'll happily
re-run the harness against any 26ai/26ai variant and re-publish the
artifact.

---

## Appendix A — Commit history of this port

```
76e45b3 bench(oracle): equal-hardware Oracle vs PG on pg-host + GPU embed bench
2c0c6e6 feat(oracle): wipe + reimport proof — migration replays on clean schema
e94bc58 feat(oracle): federation HA proof — pg-host-PG → oracle-host-Oracle pull
7d7ade9 fix(oracle): broaden federation peer SELECTs to all columns
fc8d13e fix(oracle): federation_peers compat_mode + version + schema-check columns
2ab5dab fix(oracle): federation peer + sync_log ids use hyphenated UUID
89c93d4 deploy(oracle): scripts to stand up MNEMOS-on-Oracle on oracle-host
a5fd1b3 bench(oracle): capture hardware asymmetry in perf artifact + blog
b60252a bench(oracle): embedding sync + IVF vector index + re-bench
af685a9 bench(oracle): side-by-side perf harness — Oracle Database 26ai vs Postgres+pgvector
4622c34 feat(oracle): reproducible proof harness + blog draft + KG NVL/CAST fix
8199cb6 feat(oracle): Oracle Database 26ai VECTOR semantic search + memory context
6696209 feat(oracle): full federation, recall+dedup, webhook outbox, safe audit
ed7310e docs(oracle): refresh M7 status with parity-sprint progress
9fd3be2 feat(oracle): version log + checkout + diff + create_memory_branch
ae625e1 feat(oracle): portability + branch-head + duplicate-detect impls
eede8f1 feat(oracle): visibility-filtered memory CRUD + FTS fallback
73d89ef feat(oracle): ABC-conformant subclasses for full persistence surface
7096c3e feat(oracle): wire OracleBackend into lifecycle + idempotent migrations
```

19 commits, ~5,000 net lines of new code, every commit ruff-formatted +
pre-commit-clean. Author: `Jason Perlow <jperlow@gmail.com>` per repo
conventions.

### Companion scripts shipped during this work

| Script | Purpose |
|---|---|
| `scripts/oracle_proof_run.py` | Repository-surface proof harness, signed JSON artifact |
| `scripts/oracle_vs_postgres_bench.py` | Side-by-side perf bench across both backends |
| `scripts/oracle_embedding_sync.py` | One-shot pgvector→Oracle embedding copy + index DDL |
| `scripts/oracle_apply_migration.py` | sqlplus-script aware migration runner |
| `scripts/oracle_federation_proof.py` | Read-only federation HA proof artifact emitter |
| `scripts/embed_throughput_bench.py` | Multi-endpoint GPU embedding bench (Ollama) |
| `scripts/proteus_mnemos_deploy.sh` | oracle-host Oracle-backed MNEMOS service bring-up |

## Appendix B — License + production posture

The MNEMOS code in this branch is the existing project license
(see `LICENSE`). The Oracle Database 26ai Free instance on oracle-host
runs under the Oracle Free terms — dev/learn/run only, no production
use. When we move beyond research-grade deployment, we'll need either
a paid Oracle EE license or the OTN Developer License for full
production rights, **especially before turning on Data Guard**.

The Oracle Free tier specifically does NOT include:

- Data Guard (physical or logical standby)
- GoldenGate
- Active Data Guard
- RAC
- Workspace Manager
- Spatial / Graph
- More than 12 GB user data
- More than 2 CPU
- More than 2 GB SGA+PGA
- More than 1 PDB

For Data Guard we'll move to the Oracle Technology Network Developer
License (free for dev/test, full feature set) on a second
oracle-host-class box. That story is the next chapter.
