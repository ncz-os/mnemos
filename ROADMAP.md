# MNEMOS Roadmap

Forward-looking scope for MNEMOS releases beyond the current version. Current shipping version in `pyproject.toml`. Release-by-release history in [`CHANGELOG.md`](./CHANGELOG.md).

This document is kept intentionally narrow. It lists what the next release will contain, what has been consciously deferred, and why. It does not list wishlist items, speculative features, or aspirational claims.

---

## v3.1 — compression platform + v3.0 unblocks

**Headline:** plugin-interfaced compression platform with competitive per-memory engine selection, a persisted audit log on every compression decision, and a first-class GPU batcher that works across integrated graphics, discrete GPUs, and remote OpenAI-compatible endpoints.

Three engines ship under the platform: LETHE (extractive, CPU), ALETHEIA (LLM-assisted token importance, GPU-optional), and ANAMNESIS (LLM fact extraction, GPU-optional). The `CompressionEngine` ABC is open: operators can register additional engines, and a first-party fourth engine (APOLLO — schema-aware dense encoding for LLM-to-LLM consumption) is staged across v3.2–v3.4 (see "Apollo Program" below).

### Tier 1 — small fixes that unblock real surfaces (shipped on master)

1. **MCP stdio server path prefix.** The published stdio MCP server in `mcp_server.py` called `/memories*` but the REST router registers `/v1/memories*`. Nine of fourteen memory-related MCP tools returned 404 against a default install. Fixed with the prefix + an end-to-end wire regression test (`tests/test_mcp_stdio_wire.py`).
2. **Installer `api_keys` schema.** Fresh installs with auth enabled failed at seed because `installer/db.py` wrote columns that no longer existed on the schema. Aligned the insert with the current `db/migrations_v1_multiuser.sql` table definition.
3. **Federation-role admin provisioning.** `api/handlers/admin.py` rejected `role="federation"` at validator time; `api/handlers/federation.py` required that role. Extended the admin validator so peer onboarding no longer requires direct DB writes.

### Tier 2 — the compression platform (v3.1.0 GA)

4. **Three-engine roster under a plugin ABC.** LETHE (extractive token/sentence filtering — honest about being rule-based, not ML), ALETHEIA (LLM-assisted semantic rewriting with swappable small-LLM judge — `gemma4:e2b` default, `gemma4:e4b` for quality-critical paths), and ANAMNESIS (LLM fact extraction — atomic facts, entities, concepts, summary). The `CompressionEngine` ABC is adapted from the plugin-interface pattern in OpenClaw's `CompactionProvider` (credited prior art). Operators can register additional engines at startup; the ABC is public and documented.
5. **Competitive selection.** The distillation worker runs every eligible engine per memory, scores each candidate via a composite function (quality × compression ratio × speed factor, with a quality floor that disqualifies damaged candidates), and keeps the winner. The manifest records both the winner and every losing candidate with its score and disqualification reason — a full audit trail of every compression decision. Scoring profile is operator-configurable (`balanced`, `quality_first`, `speed_first`, `custom`) via `~/.mnemos/compression_scoring.toml`.
6. **GPU endpoint circuit breaker + CPU-fallback coordination.** `compression/gpu_guard.py` tracks the health of each configured `GPU_PROVIDER_HOST` via a per-endpoint circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED). GPU-backed engines consult the guard before every HTTP call; when the circuit is open, they fast-fail with `reject_reason='disabled'` instead of piling doomed requests onto a dead endpoint. Each engine declares `gpu_intent` (`cpu_only` | `gpu_optional` | `gpu_required`); `gpu_required` engines (ALETHEIA, ANAMNESIS) skip when the circuit is open, `gpu_optional` engines (none in v3.1) would degrade to a CPU path if they had one, `cpu_only` engines (LETHE) never consult the guard. Endpoint is backend-agnostic — Ollama on an Intel iGPU, vLLM on an A10, a remote provider. **Actual request batching** (accumulating concurrent calls into one HTTP roundtrip) is a v3.2 optimization; modern inference servers (vLLM, Ollama) already batch internally at the model layer, so the v3.1 work is the correctness surface (fast-fail + routing) rather than the throughput surface.
7. **Manifest read endpoint.** `GET /v1/memories/{id}/compression-manifests` returns the winner + candidates + scoring trace for every compression decision, as JSON. Read-only view over `memory_compressed_variants` and `memory_compression_candidates`.
8. **Migration.** `db/migrations_v3_1_compression.sql` adds `memory_compressed_variants` (winner), `memory_compression_candidates` (full contest log), and `memory_compression_queue` (write-time task queue). Migration is idempotent and has been dry-run-validated against a real pgvector/pg16 container. **In v3.1 these tables are populated by the distillation worker; read paths continue to serve `memories.content` unchanged.** Hot-path invocation (rehydrate / gateway inject / session context reading the winner variant) is a substantial separate surface with its own audit, benchmarks, and migration story — scheduled for v3.2 alongside APOLLO.

### Shipping criteria for v3.1.0

- Every Tier 1 item already on master (verified).
- Every Tier 2 item lands with unit tests plus at least one live integration test against real infrastructure (no mocks-only coverage on the success path for any GPU-touching engine).
- End-to-end contract tests for MCP stdio wire compatibility (already shipped) and the new compression contest path.
- `docs/benchmarks/compression-2026-04-22.md` with measured numbers across a real stratified memory sample from the production install — not single-input anecdata.
- `CHANGELOG.md` entry listing every item above, with SHA references.
- `DEPLOYMENT.md` updated with the single-worker constraint and the scaling roadmap pointer.

### Consciously out of v3.1 scope (moved to later releases)

These were in earlier v3.1 plans and have been explicitly deferred to keep v3.1 tight and deliverable:

- **APOLLO engine + schema-aware dense encoding.** Moved to v3.2–v3.4 staged rollout (see "Apollo Program" below). The design needs deliberate time — not mining 1966-era NASA telemetry docs, but building on InvestorClaw's consultative-LLM pipeline as the canonical working pattern.
- **Narration endpoint** (`GET /v1/memories/{id}/narrate`). APOLLO's companion read path; deferred to v3.2 with APOLLO itself.
- **Hot-path compression-variant reads.** Making `/v1/memories/rehydrate`, the gateway inject path, and the session context injection path serve the winning compressed variant instead of raw `memories.content` is a substantial change to the read surface. The v3.1 tables hold the winners; v3.2 wires the reads.
- **Tier 3 tenancy fixes** (KG `owner_id`, namespace enforcement on memory paths, application-layer owner filter, registry-backed `/v1/models`). These deserve a dedicated tenancy-focused release. Targeted for **v3.1.1** as a follow-on patch series, with migration guides and per-fix regression coverage.
- **Horizontal scaling.** GRAEAE reliability primitives (circuit breakers, rate limiters, semaphores) are in-process singletons today; moving them to shared state is a dedicated refactor. v3.1 documents the single-worker constraint prominently in `DEPLOYMENT.md`.

---

## Apollo Program — v3.2 to v3.4 staged rollout

APOLLO is MNEMOS's fourth compression engine: schema-aware dense encoding targeted at **LLM-to-LLM wire use**, not human reading. The insight is that LETHE/ALETHEIA/ANAMNESIS all assume the final reader is human or a search-ranking pass. APOLLO assumes the final reader is a downstream LLM (a GRAEAE muse, a consultative agent, a tool-use caller) and encodes accordingly: typed key:value dense forms that LLMs parse natively in fewer tokens than the prose equivalent. Humans read through a narrator at read time; the raw dense form is never shown to them.

The canonical production pattern is InvestorClaw's consultative layer, which already demonstrates that `AAPL:100@150.25/175.50:tech` (12 tokens) is equivalent context for a downstream LLM to the 50-token prose sentence it was derived from.

Rolled out in stages, Saturn V-style — each stage delivers a usable payload on separation, not a deferred promise.

### v3.2 — S-IC (first stage: get off the pad)

- `APOLLOEngine` under the `CompressionEngine` ABC; `gpu_intent=gpu_optional`.
- First schema: portfolio (InvestorClaw data model lifted read-only; field shapes only, no code shared).
- Rule-based detection (regex for tickers + shares/cost patterns); LLM fallback via ANAMNESIS-pattern httpx scaffolding for content that looks fact-shaped but doesn't match the portfolio schema.
- Narration endpoint (`GET /v1/memories/{id}/narrate`) expands dense form back to prose via a small LLM; cached.
- Judge-LLM scoring integrated into the contest: quality_score is no longer a self-report from the engine — it's the judge-rated fidelity of narrated-derivative versus root memory.
- Hot-path reads wired: `/v1/memories/rehydrate`, the gateway inject path, and the session context path read from `memory_compressed_variants` when a winner exists, falling through to raw content otherwise.

### v3.3 — S-II (second stage: to upper atmosphere)

- Additional schemas: decision (decided X because Y, alternatives considered), person (name/role/org/contact), event (date/type/scope/description).
- DAG wiring for derivations: each compression candidate lands as a `memory_versions` child row with `parent_version_id → root`, branch='distilled'; narrated prose as `parent_version_id → distilled`, branch='narrated'. Each derivation is content-addressed (SHA-256), tamper-evident.
- Read-path routing: `Accept: text/plain` → narrated prose; `Accept: application/x-apollo-dense` → raw dense form; default prose narration for human users.

### v3.4 — S-IVB (third stage: trans-lunar injection)

- Distill-on-ingest becomes the default write path: new memories run the contest synchronously if they fit a schema, returning the winning variant alongside the stored original.
- ANAMNESIS deprecation path: APOLLO's LLM-fallback "generic fact extraction" mode subsumes ANAMNESIS's role. ANAMNESIS stays exported for backward compat with a deprecation notice; v4.0 removes it.
- Full round-trip fidelity benchmark as GA gate: stratified memory sample, narration judged against root, per-schema pass rates published.
- Design paper draft: git-like DAG + LLM-synthesized distillation + LLM-synthesized narration + judge-verified round-trip fidelity, the specific combination that MNEMOS appears to be the first public system to ship.

### Deferred beyond v3.4

- Full observability surface (Prometheus metrics, OpenTelemetry traces, default Grafana dashboard).
- Secrets abstraction (unified `SecretsProvider` interface with env-var passthrough, Vault plug-in, KMS plug-in).
- DAG merge conflict resolution (three-way merge with operator-assisted resolution).
- Embedding-axis quantization beyond pgvector's built-in `halfvec` and `bit` types — revisit when official TurboQuant / PolarQuant / QJL reference implementations land with compatible licenses.
- Migration rollback tooling.

---

*This document reflects committed plans, not speculative features. Items listed here are intended to land in their scheduled release unless explicitly deferred with an ADR. Priorities may shift during the release cycle; the document will be updated in the same commit that shifts them.*
