# PANTHEON + KNEMON — Unified Fleet LLM Dispatch Layer

**Status:** DESIGN (approved 2026-06-14). Decision: MNEMOS `mem_1781480942011_4a920a`;
pricing sources `mem_1781481174631_98dffc`. GRAEAE consults `1ccf4810` (repurpose=yes),
`1607ccaa` (unified architecture + cutover), 8 responders.

## Goal

Unify the two parallel, overlapping mnemos subsystems —

- **PANTHEON** (`mnemos/domain/pantheon/`): multi-provider LLM **gateway** — catalog,
  policy selection, cooldown/backoff, fallback chains, consultation caps, keyvault,
  routing audit.
- **KNEMON** (`mnemos/domain/knemon/`, `routes/knemon_*`, `ledger.py`): **budget**
  ledger, affordability, utilization.

— into **one fleet LLM dispatch layer**, and have PANTHEON **expose EIH/NGC + groq + xai
+ together + deepseek-direct directly**, replacing the thin caddy reverse-proxy at the
a stable VIP on port 4100.

They are split today only by parallel evolution (KNEMON born as a token/cost ledger;
PANTHEON born as a provider proxy). The tell: `pantheon/budget.py:evaluate_budget`
takes `spent_usd` from the caller — it needs a spend source it does not own, and
KNEMON's ledger **is** that source.

## Target architecture

```
clients (zc-build/hive hive_ngc_1, nllm, GRAEAE consults, doctor, apps)
        │  OpenAI-compatible API
        ▼
  VIP <host>:4100           ← stays stable across cutover
        │
        ▼
  PANTHEON GATEWAY (dispatch plane)
   catalog ─ policy(cost/latency/quality_floor/max_cost) ─ cooldown ─ fallback ─ caps ─ keyvault ─ routing_audit
        │                                   ▲
        │ pre-dispatch budget verdict       │ spend/cost/tokens/outcome
        ▼                                   │
  KNEMON (budget plane): ledger + affordability + <$200/wk caps + utilization
        │
        ▼
  PROVIDER MESH: EIH/NGC (/v1/responses for codex) · Azure(claude/gpt) · GCP(gemini)
                 · deepseek-v4-pro · NVCF NIMs · groq · xai · together · deepseek-direct
```

**Plane separation:**
- **PANTHEON = dispatch plane.** Owns routing: catalog → policy candidate selection →
  provider call with cooldown/fallback → routing audit.
- **KNEMON = budget plane.** Owns spend tracking (ledger), affordability verdict, caps,
  utilization. PANTHEON calls KNEMON **pre-dispatch** (402-style deny when over budget)
  and reports cost/tokens/outcome back into the ledger. One budget loop — no duplicate
  spend math.

## Catalog — continual pricing ingest

Regenerated on a timer (mirror `graeae-model-sync.service/.timer`). Tiered sources:

| Tier | Source | Role |
|---|---|---|
| **Primary (machine-readable)** | `AgentOps-AI/tokencost` (MIT) / underlying LiteLLM `model_prices_and_context_window.json` | bulk price data, 400+ models — **NOT scraped** |
| Live API | OpenRouter `/api/v1/models` | real-time price + availability |
| Quality signal | Artificial Analysis, BenchLM.ai (Score/$) | feed policy `quality_floor` |
| Seed / last-good | vendored LLM-Cost-Guardian `pricing/*.yaml` (Apache-2.0) | fallback when fetch fails |
| Cross-check (scrape, optional) | llm-prices.com, sesen.ai, llmpricecheck.com, iternal.ai | validate / fill gaps |

Per-source `fetched_at` + staleness; on refresh failure keep last-good. Cached catalog
(json/sqlite) the gateway reads. Normalize to `cost_per_mtok` (in/out), context,
capabilities, quality — keyed provider+model with alias mapping to our providers.

## Fixes folded into the unification

1. **codex/responses routing** — `gpt-5.3-codex` etc. MUST use `/v1/responses`, others
   `/v1/chat/completions` (wrong endpoint = 400). Route by model.
2. **reasoning-model token budget** — output budget high enough the answer isn't truncated
   (configurable, default ≥8000); reasoning tokens consume the budget.
3. **tool-call passthrough** — preserve OpenAI `tools`/`tool_calls`/results faithfully
   (incl. streaming); no dropped/mangled function-call args.
4. **telemetry real wire model** — stamp the per-request resolved model, not a process
   boot-seed default (the "gpt-5.4-mini" mislabel).
5. **routing audit destination** — `routing_log` → `pantheon_routing_audit` table (not the
   memory store); backend-aware consumer (was Postgres-only `$1`/`::jsonb`).

## Cutover (VIP-stable)

1. **Shadow** — PANTHEON gateway runs on a shadow port (4101); `:4100` (caddy) untouched.
   Validate: OpenAI-compat parity, tool-call roundtrip, codex `/responses`, policy/cooldown/
   fallback, budget deny path, audit lands in `pantheon_routing_audit`.
2. **Parallel** — mirror a slice of real traffic; compare results/cost/latency vs caddy.
3. **Flip** — point the `:4100` VIP at PANTHEON (VIP unchanged → workers/aliases transparent,
   as the prior litellm→caddy cutover was). Keep caddy hot as instant rollback.
4. **Rollback** — flip VIP back to caddy; pantheon is additive until flip.

**PRE-FLIP CHECKLIST (load-test gate):**

- `MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT` is fleet-scale, or disabled when testing caddy
  parity; a process-local/single-worker default is not a valid cutover limit.
- Gateway runs as a worker pool behind caddy (`gunicorn -w N ...`), not one process;
  single-worker testing ceilings around ~350 req/s are capacity findings, not the target
  topology.
- Cooldown, breaker, and request-limit state is shared through NATS/JetStream before any
  multi-worker run; process-local state is incorrect once caddy fans out.
- caddy remains the stable `:4100` VIP and load-balances across the PANTHEON worker pool;
  rollback is repointing the caddy upstream back to `inference-api`.
- Concurrent load test passes at fleet concurrency before flip: record p50/p95/p99 and
  require **0 spurious 429s** from the gateway rate limiter.

The flip is **operator/Claude-orchestrated, not a blind hive job** (fleet critical path).

## Phased build (hive jobs)

- **Phase A — catalog-costsync** (`019ec888-c2c9`): pricing ingest + timer regen + last-good.
- **Phase B — gateway-shadow** (`019ec888-c335`): OpenAI-compat gateway on shadow port, the
  5 fixes, provider mesh; `:4100`/caddy untouched.
- **Phase C — knemon-budget-unify** (`019ec888-c39c`): pantheon.budget → KNEMON ledger;
  routing audit → table + backend-aware consumer.
- **Phase D — cutover** (orchestrated, not hive): shadow-validate → VIP flip → rollback-ready.

## Risks

- `:4100` is the fleet critical path (zc-build/hive/nllm/GRAEAE/doctor). De-risk: VIP-stable,
  shadow+parallel before flip, caddy hot-standby.
- pantheon currently DISABLED — re-enable only behind the validated gateway.
- catalog scrape fragility — mitigated by tokencost/LiteLLM-JSON primary (machine-readable).
- decouple cost — pantheon stays in-tree (mnemos); KNEMON in-tree; no extraction needed for v1.

## Phase E — HEADROOM token-compression library (operator-greenlit 2026-06-14)

Decision: build HEADROOM as a lossless token-compression **library** the gateway calls
**pre-dispatch** — fewer input tokens → lower per-request cost → feeds KNEMON affordability.
(Supersedes the prior default-SKIP; `mem_1781485342861_3d44b5`.)

- Clean-room (Option-D): discard ML/CCR/proxy/telemetry/hf-hub; reimplement only the
  lossless transforms. `mnemos/domain/headroom/`, library-mode, importable.
- **Piece 1 (primary): JSON-minify** — collapse insignificant whitespace in JSON
  payloads/tool-args. **Financial correctness: numbers round-trip EXACTLY** (serde_json
  `arbitrary_precision` via pyo3, or Python `Decimal`/precision-preserving JSON) — digit
  mutation is a hard fail; numeric property tests (big ints, high-precision decimals,
  sci-notation, zeros).
- **Piece 2 (secondary, deferrable): AST code-strip** — strip comments/whitespace from
  fenced code blocks losslessly (reimpl of `code_compressor.py`/`astgrep.py`).
- API: `compress(text|messages) -> lossless result`; passthrough no-op for unsupported
  content (never corrupt). Job E builds the pure library; a follow-on (E2) wires it into
  the pantheon gateway pre-dispatch path.
- Default-skip override: pursue because it's a library asset of the unified system, not a
  caveman replacement; latency-vs-benefit evaluated live in the KNEMON cost-model.
