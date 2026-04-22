# MNEMOS Roadmap

Forward-looking scope for MNEMOS releases beyond the current version. Current shipping version in `pyproject.toml`. Release-by-release history in [`CHANGELOG.md`](./CHANGELOG.md).

This document is kept intentionally narrow. It lists what the next release will contain, what has been consciously deferred, and why. It does not list wishlist items, speculative features, or aspirational claims.

---

## v3.1 — compression platform + v3.0 unblocks

**Headline:** four-engine compression platform with competitive selection, first-class GPU batcher supporting everything from integrated graphics to discrete GPUs, and tenancy fixes that make the "per-owner scoping" claim true across every state-bearing subsystem.

### Tier 1 — small fixes that unblock real surfaces (v3.1.0-rc1)

1. **MCP stdio server path prefix.** The published stdio MCP server in `mcp_server.py` calls `/memories*` but the REST router registers `/v1/memories*`. Nine of fourteen memory-related MCP tools return 404 against a default install. Ship the prefix fix + an end-to-end wire regression test (`tests/test_mcp_stdio_wire.py`).
2. **Installer `api_keys` schema.** Fresh installs with auth enabled currently fail at seed because `installer/db.py` writes columns that no longer exist on the schema. Align the insert with the current `db/migrations_v1_multiuser.sql` table definition.
3. **Federation-role admin provisioning.** `api/handlers/admin.py` currently rejects `role="federation"` at validator time; `api/handlers/federation.py` requires that role. Extend the admin validator so peer onboarding does not require direct DB writes.

### Tier 2 — the compression platform (v3.1.0 GA)

4. **Four-engine roster.** LETHE (extractive token/sentence filtering — honest about being rule-based, not ML), ALETHEIA (LLM-assisted semantic rewriting with swappable small-LLM judge — `gemma4:e2b` default, `gemma4:e4b` for quality-critical paths), ANAMNESIS (knowledge-graph triple extraction, structural axis), and APOLLO (schema-aware dense encoding — descended from Apollo-era telemetry protocol design). The `CompressionEngine` ABC is adapted from the plugin-interface pattern in OpenClaw's `CompactionProvider` (credited prior art).
5. **Competitive selection.** The distillation worker runs every eligible engine per memory, scores each candidate via a composite function (quality score × compression ratio × speed factor, with a quality floor that disqualifies damaged candidates), and keeps the winner. The manifest records both the winner and every losing candidate with its score and disqualification reason — a full audit trail of every compression decision. Scoring profile is configurable (`balanced`, `quality_first`, `speed_first`, `custom`) via `~/.mnemos/compression_scoring.toml`.
6. **GPU as a first-class platform resource.** `compression/gpu_batcher.py` accumulates GPU-bound work (ALETHEIA, APOLLO narration, quality scoring) and batches against the configured `GPU_PROVIDER_HOST` endpoint. Each engine declares `gpu_intent` (`cpu_only` | `gpu_optional` | `gpu_required`). Endpoint is backend-agnostic — Ollama on an Intel iGPU, vLLM on an A10, a remote provider. CPU fallback is mandatory for every GPU-gated engine; users without a GPU see graceful degradation, not broken behavior.
7. **Narration endpoint.** `GET /v1/memories/{id}/narrate` renders APOLLO-encoded or otherwise-dense memories to prose on demand via a small LLM. Cached; human inspection after the first read is a cache hit, not a GPU roundtrip.
8. **Manifest read surface.** `GET /v1/memories/{id}/compression-manifests` returns the winner + candidates + scoring trace for every compression decision.
9. **Migration.** `db/migrations_v3_1_compression.sql` adds `memory_compressed_variants` (winner), `memory_compression_candidates` (full contest log), and `memory_compression_queue` (write-time task queue). Read paths (`/v1/memories/search`, `/v1/memories/rehydrate`, gateway inject, session inject) query the winner variant and fall through to raw content when no variant exists.

### Tier 3 — tenancy and correctness (alongside Tier 2)

10. **Knowledge-graph tenancy.** `kg_triples` gets an `owner_id` column, backfilled to the root user on existing installs. KG handlers enforce owner scoping. Decision captured in ADR: multi-tenant KG (default) or root-only KG (explicit opt-out for operators who do not want the isolation tax).
11. **Namespace enforcement.** Memory list/search/rehydrate filter on `user.namespace` by default. RLS policies extended to include the namespace key when the session variable is set.
12. **Application-layer owner filter.** Every list/search/rehydrate adds an explicit `WHERE owner_id = $1` clause. RLS remains the canonical enforcement; the application filter is defense in depth for operators who deploy with RLS disabled by accident.
13. **Registry-backed `/v1/models`.** The endpoint reads from the `models` table populated by `graeae/provider_sync.py` rather than returning a hardcoded list. The README's claim of a self-maintaining model registry becomes true end to end.

### Tier 4 — nice-to-have in v3.1 if time allows, otherwise v3.1.1 patch

- Structured `model="auto"` provider resolution (drop substring matching).
- Rate-limit bucket keyed on `user_id` when authenticated, falling back to IP when not.
- DAG manual-merge — return `422` with a clear error rather than a misleading success-shaped payload; actual conflict resolution is v3.2 work.
- Session rolling context summarization once compression is wired into the session-injection path.
- Delete the orphaned `api/mcp_tools.py` surface now that the stdio server is canonical.

### Deferred to v3.2 or later (with rationale)

- **Horizontal scaling.** GRAEAE reliability primitives (circuit breakers, rate limiters, semaphores) are in-process singletons today; moving them to shared state is a dedicated refactor. v3.1 documents the single-worker constraint prominently in `DEPLOYMENT.md`. v3.2 scope.
- **DAG merge conflict resolution.** Three-way merge with operator-assisted resolution is a protocol design task. Spec first, then code. v3.2 feature.
- **Secrets abstraction.** Today MNEMOS uses three different secret stores (`~/.api_keys_master.json`, federation peer tokens in the DB, session secret regenerated at startup). A unified `SecretsProvider` interface with env-var passthrough, Vault plug-in, and KMS plug-in is v3.2 work.
- **Observability surface.** Prometheus metrics and OpenTelemetry traces with a default Grafana dashboard. Nice to have; not blocking v3.1.
- **Embedding-axis quantization.** `pgvector`'s built-in `halfvec` and `bit` types are available today for operators who want embedding compression; MNEMOS v3.1 does not bundle this. Research-grade vector quantization (TurboQuant / PolarQuant / QJL — Google Research, ICLR 2026) has no license-compatible production release yet; revisit when official implementations land.
- **Migration rollback tooling.** Eleven forward-only migrations is acceptable for a young project. A rollback tool is v3.2 quiet-quarter work.

### Shipping criteria for v3.1.0

- Every Tier 1 and Tier 2 item lands or gets an explicit ADR deferring it.
- Every Tier 3 item lands; tenant-safety claims in the README are true end to end.
- End-to-end contract tests for MCP stdio wire compatibility, OpenAI gateway multi-turn + streaming, federation peer pull round-trip, and compression competitive selection.
- `docs/benchmarks/compression-2026-04-22.md` with measured numbers across a real memory sample, not single-input anecdata.
- `DEPLOYMENT.md` updated with the single-worker constraint and the scaling roadmap pointer.

---

## v3.2 and beyond

Placeholder section. v3.2 scope is set during the v3.1 release cycle once the compression-platform shape is settled in practice.

Candidate themes, in rough priority order: horizontal scaling, observability, secrets abstraction, DAG merge conflict resolution, streaming and tool calling in the gateway.

---

*This document reflects committed plans, not speculative features. Items listed here are intended to land in their scheduled release unless explicitly deferred with an ADR. Priorities may shift during the release cycle; the document will be updated in the same commit that shifts them.*
