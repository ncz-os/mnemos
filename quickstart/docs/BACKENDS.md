# Choosing a backend (all free, all official containers)

mnemos is backend-agnostic: the same image + the same MIF memory model run on any of four
**free** databases with a native vector type. Pick one with a compose profile
(`docker compose --profile <name> up -d`). mnemos self-provisions its schema (vector column +
index) on first connect — no manual DDL.

| Profile | Engine | Free tier | Vector | Notes |
|---|---|---|---|---|
| `db2` | IBM Db2 Community Edition | free (`LICENSE=accept`) | native `VECTOR` + vector index | **Recommended lead** — fewest CE limits |
| `oracle` | Oracle Database 23ai **Free** | free | AI Vector Search (HNSW/IVF) | see EE note below |
| `postgres` | PostgreSQL + pgvector | open source | pgvector HNSW | most familiar |
| `mariadb` | MariaDB 11.7+ | open source | community `VECTOR` | pure OSS path |

All four use the shared CPU **bge-m3** embedder (1024-dim) — so every backend runs
`MNEMOS_EMBEDDING_DIM=1024`. (Swap the embedder in `docker-compose.yml`; match the dim.)

---

## Who this is for + free-tier limits

This quickstart is for standing up a **small-scale mnemos for evaluation and development at
zero enterprise-database cost** — a laptop/single-host memory store, not a production cluster.
Two of the four backends (Postgres, MariaDB) are fully open source with **no engine limits at
all**; the two proprietary databases (Db2, Oracle) ship **free editions** whose capacity caps
are listed below. When you outgrow a free tier, **mnemos speaks the same API on the paid
edition of that same engine — switch `MNEMOS_DATABASE_DSN` and migrate whenever you're ready,
with no change to mnemos and no change to your data model.**

| Engine | License / edition | Cores | Memory | Data size | Prod use | Vector |
|---|---|---|---|---|---|---|
| **Db2 Community Edition** | free (`db2dec.lic`, permanent) | ≤ 4 cores | ≤ 8 GB instance (Soft Stop) | **unlimited** | allowed (small workloads) | native `VECTOR` + vector index |
| **Oracle 23ai Free** | free (OTN) | ≤ 2 CPUs (foreground) | ≤ 2 GB (SGA+PGA) | ≤ 12 GB user data | allowed | AI Vector Search |
| **PostgreSQL + pgvector** | open source (PostgreSQL Lic.) | *no engine cap* — host-bound | host-bound | host-bound | yes | pgvector HNSW (≤ 2000 dims indexed) |
| **MariaDB** | open source (GPLv2) | *no engine cap* — host-bound | host-bound | host-bound | yes | native `VECTOR` (11.7+) |

**What the caps mean for a memory store:** a mnemos memory is small — text plus one 1024-float
bge-m3 vector, a few KB each — so even Oracle Free's 12 GB user-data cap holds on the order of
a million+ memories, ample for eval/dev; Db2 CE (8 GB RAM / 4 cores, **unlimited** data) is the
most generous proprietary free tier. **Postgres and MariaDB impose no engine limits** — bounded
only by your host — which is why they're the pure-open-source choices. bge-m3's 1024 dims sit
well inside every backend's vector limits (pgvector indexes up to 2000).

**Migrating to a paid edition later:** nothing here is tied to the free tier. Point
`MNEMOS_DATABASE_DSN` at Db2 Standard/Advanced, Oracle EE, or a managed Postgres/MariaDB; mnemos
self-provisions its schema on first connect, and existing memories move across cleanly via MIF
export/import (see [`MIF-MIGRATION.md`](MIF-MIGRATION.md)).

Sources for the free-tier figures — Db2 verified **live** against the running 12.1.5.0 CE
instance (`db2licm -l`: `db2dec`, Max memory **8 GB**, Max cores **4**, Permanent, Soft Stop —
note this is the enforced value on 12.1.5, which is *lower* than the 16 GB some third-party
guides quote); Oracle from the [Oracle Database Free FAQ](https://www.oracle.com/database/free/faq/)
(2 CPUs / 2 GB RAM / 12 GB user data).

---

## Db2 Community Edition — the lead
Native `VECTOR` type + `VECTOR_DISTANCE` + vector indexing (via `DB2_VECTOR_INDEXING`), exposed via Oracle-compat mode
(`DB2_COMPATIBILITY_VECTOR=ORA`, `DB2_VECTOR_INDEXING=YES`; applied by
`scripts/init-db2-vectors.sh`). Db2 CE has the **fewest capability limits of the "enterprise"
free tiers** (generous size/feature envelope for evaluation), which is why we lead with it.
DSN: `db2://db2inst1:<pw>@db2:50000/MNEMOS`. DB must be **UTF-8 / 32K pagesize**.

## Oracle Database 23ai Free
The published free container is **Oracle 23ai Free** (`container-registry.oracle.com/database/free`),
which **does** include **AI Vector Search** — enough for mnemos semantic recall. DSN:
`oracle://system:<pw>@oracle:1521/FREEPDB1`.

> **⚠ Not an anonymous pull.** Unlike Db2 CE (icr.io), pgvector, and MariaDB (Docker Hub) — all of
> which pull with no login — Oracle's registry requires you to `docker login
> container-registry.oracle.com` with an Oracle SSO account **and accept the image license in the
> web UI once** before `docker compose --profile oracle up` will succeed. The image is free; the
> gate is a one-time login + license click.

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

---

## Embedder tuning (semantic floor)

Semantic search returns only hits whose cosine similarity clears a floor. Two env vars on each
mnemos service control it:

- **`MNEMOS_SEMANTIC_FLOOR`** (mnemos default `0.65`) — the absolute minimum cosine similarity a
  hit must clear.
- **`MNEMOS_SEMANTIC_MARGIN_FLOOR`** — an out-of-distribution gate that *also* requires the top
  hit to stand out from the pack; set `0` to disable it and rely on the absolute floor alone.

The `0.65` default is calibrated for a **full-precision** embedder. The bundled CPU embedder is a
**quantized bge-m3 (Q8 GGUF)**, which compresses the similarity range: a strong paraphrase (no
shared words) measures a raw cosine of roughly **0.55–0.58**, not the 0.70+ a full-precision model
produces. So with the bundled embedder, a good paraphrase can sit *below* the default floor and
silently return nothing.

**Rule of thumb:** re-tune the floor whenever you change the embedder.
- Bundled quantized bge-m3 → set `MNEMOS_SEMANTIC_FLOOR=0.45` and `MNEMOS_SEMANTIC_MARGIN_FLOOR=0`.
- Full-precision / hosted embedder → the `0.65` default is appropriate.

Too high silently drops real hits; too low lets unrelated noise leak in. Measure your own
distribution: store a few memories, then compare a good-paraphrase query's top score against an
unrelated query's top score, and set the floor between them.
