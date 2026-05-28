# MEDUSA Vulkan embed + reranker services (2026-05-23)

**Host:** MEDUSA `192.168.207.64`
**GPU:** AMD Radeon Pro 5500M (NAVI14, 4 GB) via Vulkan
**Kernel:** 7.0.9-1-t2-resolute (Ubuntu T2 24.04)
**llama.cpp version:** 8681 (Debian)

---

## Services

| Service | Port | Model | Dim | VRAM | Throughput |
|---|---|---|---|---|---|
| llama-embed-bge-m3 | 8090 | bge-m3 Q5_K_M (467 MB) | 1024 | ~300 MB | 22 embeds/s (batch=100) · 8 embeds/s (serial) |
| llama-rerank-bge   | 8091 | bge-reranker-v2-m3 Q5_K_M (468 MB) | n/a | ~300 MB | 16 pairs/s (200-doc batch) |

Both services pin to `Vulkan0` (AMD NAVI14). `Vulkan1` (Intel UHD 630, 12 GB shared) is available as fallback but slower for transformer ops; reserve for ic-engine spillover.

### Endpoints

```bash
# Embed (OpenAI-compat)
curl -sS http://192.168.207.64:8090/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"bge-m3","input":"text or [list of texts]"}'

# Rerank
curl -sS http://192.168.207.64:8091/v1/rerank \
  -H 'Content-Type: application/json' \
  -d '{"model":"bge-reranker-v2-m3","query":"...","documents":["...","..."]}'

# Health (both)
curl -sS http://192.168.207.64:8090/health
curl -sS http://192.168.207.64:8091/health
```

### Systemd units

- `/etc/systemd/system/llama-embed-bge-m3.service` — `--embedding --pooling mean --ctx-size 8192 --batch 512 --parallel 4 --gpu-layers 99 --device Vulkan0`
- `/etc/systemd/system/llama-rerank-bge.service` — `--rerank --pooling rank --ctx-size 8192 --batch 256 --parallel 2 --gpu-layers 99 --device Vulkan0`

Both `Restart=on-failure`, `MemoryMax=4G`, `User=jasonperlow`.

### Models

- `~/models/bge-m3-Q5_K_M.gguf` — from `https://huggingface.co/lm-kit/bge-m3-gguf`
- `~/models/bge-reranker-v2-m3-Q5_K_M.gguf` — from `https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF`

---

## Use cases

### A — Fleet embedding worker (PYTHIA delegation)

MNEMOS distillation / ingest writes to PYTHIA primary. For new embeddings, PYTHIA HTTP POSTs to `http://192.168.207.64:8090/v1/embeddings` instead of running on Intel Xe iGPU + OpenVINO. Saves PYTHIA CPU for orchestration.

Config side: set `INFERENCE_EMBED_ENDPOINT=http://192.168.207.64:8090/v1/embeddings` + `INFERENCE_EMBED_MODEL=bge-m3` in mnemos-api environment.

### B — v6.2 retrieval-profile `deep` reranker

`mnemos/domain/search/pipeline_deep.py` (per `docs/v6.2-nexus-pattern-adoption.md` § 2) calls `:8091/v1/rerank` for the 100-doc cross-encoder rerank pass. Originally earmarked for CERBERUS:8090 in the v6.2 design — MEDUSA picked up the role to keep CERBERUS free for narrative LLM (gemma-4-31B).

### C — MEDUSA SQLite MNEMOS replica (Phase C planned)

After v6.1 F-1 plumbing lands (`F-1.2..F-1.5` hive jobs), MEDUSA runs a local mnemos-api with `MNEMOS_BACKEND=sqlite` + federation peer = PYTHIA with `copy_embeddings=1`. MEDUSA serves `/v1/memories/search` with 100% bge-m3 vectors, no PYTHIA round-trip.

---

## Phase B: PYTHIA fleet migration to bge-m3 (open)

Current PYTHIA default = `nomic-embed-text` (768d) per `mnemos/core/config.py:inference_embed_model`. Migration to bge-m3 (1024d) requires:

1. Update `INFERENCE_EMBED_MODEL=bge-m3` + add `INFERENCE_EMBED_ENDPOINT=http://192.168.207.64:8090/v1/embeddings` to PYTHIA mnemos-api env
2. Schema change: PG/Oracle/Db2 `embedding VECTOR(*, 1024)` instead of `VECTOR(*, 768)` — or add `embedding_v2 VECTOR(*, 1024)` parallel column with read-fallback
3. Re-embed sweep: 8235 memories × bge-m3 = ~375s on MEDUSA Vulkan (22 emb/s batched)
4. Validate `semantic_search` Recall@10 vs golden set after switch

The sweep itself can be a hive job `mnemos:fleet-embed-migration-batch` dispatched to MEDUSA via Goose worker (Goose registered on MEDUSA per fleet deployment 2026-05-23). Estimated 1h focused work.

---

## Bench raw

```
$ python3 ~/bench_medusa.py
embed serial 100x: 12.54s  -> 8.0 embeds/sec
embed batch  100x: 4.53s  -> 22.1 embeds/sec  (100 returned)
rerank 1q 32d:    2.037s  -> 15.7 pairs/sec
rerank 1q 200d:   12.215s -> 16.4 pairs/sec
```

Serial is 3× slower than batched — HTTP overhead + cold-cache between requests. Always batch when possible (MNEMOS ingest already batches 100-row chunks; just plumb through to the endpoint).

---

## VRAM accounting

```
$ llama-server --list-devices
  Vulkan0: AMD Radeon (RADV NAVI14)  4080 MiB,  3501 MiB free   ← 579 MiB used by both services
  Vulkan1: Intel UHD 630 (CFL GT2)  11916 MiB, 10724 MiB free
```

Combined model weights (~580 MB Q5 each) + tiny KV cache fit in <600 MB. Headroom on AMD for a third small model if needed; Intel iGPU available as overflow.

---

## Cross-references

- `docs/v6.2-nexus-pattern-adoption.md` § 2 — retrieval profiles + reranker design (MEDUSA picks up the M-2.2.3 service)
- `docs/v6.1-federation-embeddings-copy.md` — F-1 spec consumed by Phase C
- `~/.claude/rules/fleet-roles-canonical-2026-05-06.md` — MEDUSA role (was "InvestorClaw devtest"; expand to "embed+rerank GPU host")
- MNEMOS `mem_1779581468076_491ff9` — prior macstudio session confirming MEDUSA Vulkan inference path
