# Choosing a backend (all free, all official containers)

mnemos is backend-agnostic: the same image + the same MIF memory model run on any of four
**free** databases with a native vector type. Pick one with a compose profile
(`docker compose --profile <name> up -d`). mnemos self-provisions its schema (vector column +
index) on first connect — no manual DDL.

| Profile | Engine | Free tier | Vector | Notes |
|---|---|---|---|---|
| `db2` | IBM Db2 Community Edition | free (`LICENSE=accept`) | native `VECTOR` + DiskANN | **Recommended lead** — fewest CE limits |
| `oracle` | Oracle Database 23ai **Free** | free | AI Vector Search (HNSW/IVF) | see EE note below |
| `postgres` | PostgreSQL + pgvector | open source | pgvector HNSW | most familiar |
| `mariadb` | MariaDB 11.7+ | open source | community `VECTOR` | pure OSS path |

All four use the shared CPU **bge-m3** embedder (1024-dim) — so every backend runs
`MNEMOS_EMBEDDING_DIM=1024`. (Swap the embedder in `docker-compose.yml`; match the dim.)

---

## Db2 Community Edition — the lead
Native `VECTOR` type + `VECTOR_DISTANCE` + DiskANN index, exposed via Oracle-compat mode
(`DB2_COMPATIBILITY_VECTOR=ORA`, `DB2_VECTOR_INDEXING=YES`; applied by
`scripts/init-db2-vectors.sh`). Db2 CE has the **fewest capability limits of the "enterprise"
free tiers** (generous size/feature envelope for evaluation), which is why we lead with it.
DSN: `db2://db2inst1:<pw>@db2:50000/MNEMOS`. DB must be **UTF-8 / 32K pagesize**.

## Oracle Database 23ai Free
The published free container is **Oracle 23ai Free** (`container-registry.oracle.com/database/free`),
which **does** include **AI Vector Search** — enough for mnemos semantic recall. DSN:
`oracle://system:<pw>@oracle:1521/FREEPDB1`.

> **On Oracle Enterprise Edition:** Oracle's **OTN Developer License** permits **non-production
> evaluation** of EE features for development/testing — but there is **no published EE
> container image**; you would install EE yourself under that license. For a turnkey free
> container, **23ai Free is the supported path here** and covers vector search. We do not ship
> or imply an EE image.

## PostgreSQL + pgvector
`pgvector/pgvector:pg17` — pure open source, HNSW vector index, the most widely-understood
option. DSN: `postgres://mnemos:<pw>@postgres:5432/mnemos`. Great default if you already run
Postgres.

## MariaDB
`mariadb:11.8` — MariaDB 11.7+ ships a native `VECTOR` type + vector index, fully open source.
The **pure-OSS backend** for users who want no vendor tiers at all. DSN:
`mariadb://mnemos:<pw>@mariadb:3306/mnemos`.

---

### The one config rule that bites everyone
Whatever backend + embedder you choose, **`MNEMOS_EMBEDDING_DIM` must equal the embedder's
output dimension** (bge-m3 = 1024, nomic-embed-text = 768). The env var is
`MNEMOS_EMBEDDING_DIM` (aliases `MNEMOS_EMBEDDING_DIM` / `PG_EMBEDDING_DIM`).
`MNEMOS_DATABASE_EMBEDDING_DIM` is **not** a recognized key — using it silently defaults to
768 and every store fails with a vector-dimension cast error. This applies to all four
backends.
