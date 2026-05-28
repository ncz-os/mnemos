# MEDUSA edge MNEMOS replica deployment (2026-05-23 → 24)

**Status:** LIVE 2026-05-24 00:00 EDT
**Host:** MEDUSA `192.168.207.64` (MacBook Pro 16" 2019, T2, i7-9750H, AMD Radeon Pro 5500M, 16 GB RAM, 457 GB NVMe, Ubuntu T2 24.04)
**Endpoints:**

| Service | Port | Role |
|---|---|---|
| `mnemos-api` | 5002 | SQLite MNEMOS replica (8291 rows, 8291 bge-m3 embeddings) |
| `llama-embed-bge-m3` | 8090 | Vulkan bge-m3 embed (primary for MEDUSA, fallback for PYTHIA) |
| `llama-rerank-bge` | 8091 | Vulkan bge-reranker-v2-m3 (v6.2 M-2.2.3 reranker — live) |
| `redis-server` | 6379 | resilience layer (rate limit + cache) |
| `ic-engine` | 18090 / 18092 | InvestorClaw (pre-existing) |
| `zeroclaw` | — | agent runtime (pre-existing) |

---

## What this is

A second-source MNEMOS instance that mirrors PYTHIA's Oracle 23ai primary. Federation-pulls content + locally re-embeds via on-host bge-m3 Vulkan. Designed for:

- **LAN survivability** — PYTHIA down → MEDUSA still answers `/v1/memories/search`.
- **Portability** — MBP form factor + battery → take MNEMOS offsite without VPN.
- **Quality A/B** — same 1024d bge-m3 vector space; semantic search comparable.
- **Edge demo rig** — single-host stack including AI runtime, no external deps.

Not designed for: write fan-out (writes still go to PYTHIA primary), production traffic load (single-process SQLite).

---

## Bootstrap path (one-time)

Sequence used 2026-05-23 → 24, ~10 min wall clock total once image transferred:

```bash
# 0. transfer 5.6 GB image PYTHIA -> MEDUSA via gzip-ssh stream (~3 min)
ssh jasonperlow@192.168.207.67 'docker save mnemos-os:oracle | gzip' | \
  ssh jasonperlow@192.168.207.64 'gunzip | docker load'

# 1. MEDUSA dependencies
ssh jasonperlow@192.168.207.64 'sudo apt-get install -y redis-server && sudo systemctl enable --now redis-server'

# 2. launch mnemos-api on MEDUSA — SQLite + http embed via localhost MEDUSA :8090
# (see scripts/medusa_mnemos_run.sh; env knobs below)

# 3. live-patch embedder.py for HTTP backend (image was built before runtime/embedder
#    HTTP backend; new images bake it in via commit dfc56ed)
docker cp embedder.py mnemos-api:/app/mnemos/runtime/embedder.py
docker restart mnemos-api

# 4. register PYTHIA as federation peer
curl -X POST http://192.168.207.64:5002/v1/federation/peers \
  -d '{"name":"pythia","base_url":"http://192.168.207.67:5002",
       "auth_token":"<PYTHIA BEARER>",
       "namespace_filter":null,"category_filter":null,
       "enabled":true,"sync_interval_secs":300,"compat_mode":"permissive"}'

# 5. trigger initial sync (8289 rows in ~13s)
curl -X POST http://192.168.207.64:5002/v1/federation/peers/<peer-id>/sync -d '{}'
# pulls content only; embedding column is NULL per current F-1 plumbing state
# (changes once v6.1 F-1.2-1.5 lands — copy_embeddings flag carries vectors)

# 6. backfill bge-m3 embeddings via TYPHON CUDA endpoint (~2 min for 8289 rows)
docker exec -e BATCH=32 -e EMBED_URL=http://192.168.207.61:8090/v1/embeddings \
  mnemos-api python3 /tmp/backfill_sqlite_embeddings.py

# 7. mnemos SQLite reads from memory_embeddings JOIN table; migrate
docker exec mnemos-api python3 /tmp/migrate_embeddings_to_join_table.py

# 8. mnemos SQLite stores embeddings as JSON text not raw BLOB; re-encode
docker exec mnemos-api python3 /tmp/reencode_embeddings.py
```

After step 8: `curl -d '{"query":"...","semantic":true}' http://192.168.207.64:5002/v1/memories/search` returns ranked federated + local hits.

Helper scripts under `scripts/`:
- `backfill_sqlite_embeddings.py`
- `migrate_embeddings_to_join_table.py`
- `reencode_embeddings.py`

---

## Container env (production)

```
MNEMOS_PROFILE=server
MNEMOS_PERSISTENCE_BACKEND=sqlite
MNEMOS_DATABASE_DSN=sqlite:///data/mnemos.db
MNEMOS_DATABASE_SQLITE_PATH=/data/mnemos.db
MNEMOS_DATABASE_EMBEDDING_DIM=1024
MNEMOS_EMBEDDING_DIM=1024
MNEMOS_AUTH_ENABLED=false                  # LAN-only deploy
MNEMOS_EMBED_BACKEND=http
MNEMOS_EMBED_HTTP_URL=http://192.168.207.64:8090/v1/embeddings   # primary: localhost MEDUSA Vulkan
MNEMOS_EMBED_HTTP_URL_FALLBACK=http://192.168.207.61:8090/v1/embeddings  # secondary: TYPHON CUDA
MNEMOS_EMBED_HTTP_MODEL=bge-m3
MNEMOS_EMBED_HTTP_TIMEOUT=60
FEDERATION_ALLOW_PRIVATE=true
FEDERATION_ALLOW_INSECURE=true
WEBHOOK_ALLOW_PRIVATE_HOSTS=true
MNEMOS_RATE_LIMIT_PER_MINUTE=10000
RATE_LIMIT_PER_MINUTE=10000
RATE_LIMIT_DEFAULT=10000/minute
```

3-tier embed fallback (per `mem_1779334716543_f8ebd4` EXCEPTION clause):
1. MEDUSA :8090 (localhost Vulkan, ~180 ms per embed on long content)
2. TYPHON :8090 (CUDA, ~30 ms per embed, 15× faster)
3. local llamacpp + nomic-embed-text-v1.5.Q8_0.gguf (CPU, last-resort)

Switch primary to TYPHON for query latency: set `MNEMOS_EMBED_HTTP_URL` to TYPHON, keep MEDUSA in fallback.

---

## Systemd units on MEDUSA

```
/etc/systemd/system/mnemos-api.service           # oneshot docker start wrapper
/etc/systemd/system/llama-embed-bge-m3.service   # llama.cpp bge-m3 Vulkan
/etc/systemd/system/llama-rerank-bge.service     # llama.cpp bge-reranker-v2-m3 Vulkan
/etc/systemd/system/redis-server.service         # stock distro package
```

All `enable --now` and `Restart=on-failure`.

---

## Ongoing-state caveats

- **Federation pulls run every 5 min** by default. New rows arrive on MEDUSA with `embedding=NULL` (the F-1.2-1.5 plumbing that carries embeddings via federation isn't yet shipped). Until then, a background backfill_sqlite_embeddings.py cron is needed to embed delta rows — currently manual.
- **Writes to MEDUSA stay local** (don't propagate to PYTHIA). Treat MEDUSA as read-only mirror; use PYTHIA for writes.
- **Vector format gotcha** — mnemos SQLite stores `memory_embeddings.embedding` as JSON text (`json.dumps([...])`). Raw float32 BLOB is rejected by `mnemos_cosine_similarity` UDF. The `reencode_embeddings.py` helper exists for migrations from a BLOB-writing backfill.
- **Re-embed on dim change** — switching primary embed model to anything other than bge-m3 1024d requires re-embedding all rows. The Oracle schema swap pattern (`scripts/oracle_swap_to_bge_m3.sh`) has a SQLite analog left as exercise.

---

## TYPHON embed services (companion infra)

MEDUSA fallback chain depends on TYPHON :8090 (bge-m3 CUDA) + :8091 (nomic-embed-text CUDA). Both moved from `nohup`-managed (would die at OOM / SSH disconnect) to systemd 2026-05-24:

```
/etc/systemd/system/llama-embed-bge-m3.service   # TYPHON RTX 5060
/etc/systemd/system/llama-embed-nomic.service    # TYPHON RTX 5060
```

Use cases for TYPHON nomic :8091:
- Phase B PYTHIA dual-index `embedding_nomic` column backfill (one-time done; ongoing via cron-style worker TBD)
- 768d compatibility for legacy callers if/when nomic re-enters the stack

---

## Open follow-ups

1. **F-1.2-1.5 plumbing** (separate hive jobs `019e5764-6c91..6d5f`) — federation feed carries embedding bytes → MEDUSA delta rows arrive pre-embedded; backfill cron not needed.
2. **Ongoing dual-write worker** — PYTHIA + MEDUSA both populate `embedding_nomic` for new memories (not just backfill). Worker reads `WHERE embedding_nomic IS NULL`, embeds locally, UPDATEs.
3. **HA failover** — point a fleet-wide DNS / haproxy at PYTHIA primary; failover to MEDUSA on health-check failure. Currently no HA layer; clients pick host explicitly.
4. **Schema parity check** — periodic compare-row-count + sample-hash of PYTHIA vs MEDUSA to detect federation drift.

---

## Cross-references

- `docs/medusa-vulkan-embed-rerank.md` — Phase A Vulkan llama.cpp deployment
- `docs/v6.1-federation-embeddings-copy.md` — F-1 federation embeddings design (blocks future no-backfill bootstrap)
- `docs/v6.2-nexus-pattern-adoption.md` § 2 — reranker role MEDUSA :8091
- `scripts/backfill_sqlite_embeddings.py`, `migrate_embeddings_to_join_table.py`, `reencode_embeddings.py`
- MNEMOS authoritative memory: `mem_1779593671139_c145aa` (Phase C bootstrap complete)
- Operator-lock override: `mem_1779334716543_f8ebd4` EXCEPTION clause (HTTP embed permitted for MEDUSA only)
