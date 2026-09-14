# PANTHEON + KNEMON — Unified Fleet LLM Dispatch Layer

PANTHEON and KNEMON are two halves of one fleet LLM dispatch layer: a **dispatch
plane** that decides where a request goes, and a **budget plane** that decides
whether it may go at all. They began as independent subsystems — KNEMON as a
token/cost ledger, PANTHEON as a provider proxy — and the seam between them was
always visible: PANTHEON's budget evaluation takes `spent_usd` from its caller,
which means it needs a spend source it does not own, and KNEMON's ledger **is**
that source.

Both ship as separately-installable add-on distributions, not as in-tree
mnemos-core code:

| Component | Distribution | Extra | Import path |
|---|---|---|---|
| PANTHEON — dispatch plane | `mnemos-pantheon` | `pantheon` | `mnemos.domain.pantheon.*` |
| KNEMON — budget plane | `mnemos-knemon` | `knemon` | `mnemos.domain.knemon.*` |

mnemos-core keeps only the integration seams — conditional route mounting, MCP
tool wrappers, configuration settings, schema migrations, and ops units — and
declares both in the `server` and `full` bundles. An install without the extra
does not carry a disabled copy of the code; it does not carry the code at all.

## Architecture

```
clients (OpenAI-compatible: agent harnesses, consult tooling, apps)
        │  OpenAI-compatible API
        ▼
  stable VIP (reverse proxy)   ← address never changes across cutover/rollback
        │
        ▼
  PANTHEON GATEWAY (dispatch plane)
   catalog ─ policy(cost/latency/quality_floor/max_cost) ─ cooldown ─ fallback ─ caps ─ keyvault ─ routing_audit
        │                                   ▲
        │ pre-dispatch budget verdict       │ spend/cost/tokens/outcome
        ▼                                   │
  KNEMON (budget plane): ledger + affordability + weekly spend caps + utilization
        │
        ▼
  PROVIDER MESH: OpenAI-compatible upstreams · reasoning/codex models on /v1/responses
                 · hosted open-weight NIMs · local vLLM · groq · xai · together · deepseek-direct
```

**Plane separation.** PANTHEON owns routing: catalog → policy candidate
selection → provider call with cooldown and fallback → routing audit. KNEMON
owns spend: ledger, affordability verdict, caps, utilization. PANTHEON calls
KNEMON **pre-dispatch** (402-style deny when over budget) and reports
cost/tokens/outcome back into the ledger afterwards. One budget loop, no
duplicate spend math on either side.

## Catalog — continual pricing ingest

The catalog is regenerated on a timer rather than hand-maintained. Tiered
sources:

| Tier | Source | Role |
|---|---|---|
| **Primary (machine-readable)** | `AgentOps-AI/tokencost` (MIT) / underlying LiteLLM `model_prices_and_context_window.json` | bulk price data, 400+ models — **not scraped** |
| Live API | OpenRouter `/api/v1/models` | real-time price + availability |
| Quality signal | public model-quality indices (score-per-dollar) | feeds policy `quality_floor` |
| Seed / last-good | vendored `LLM-Cost-Guardian` `pricing/*.yaml` (Apache-2.0) | fallback when a fetch fails |
| Cross-check (optional) | public price-comparison sites | validate / fill gaps |

Each source carries its own `fetched_at` and staleness; a failed refresh keeps
the last-good catalog. The result is normalized to `cost_per_mtok` (in/out),
context window, capabilities, and quality, keyed by provider plus model with
alias mapping onto configured providers, and cached as JSON and SQLite for the
gateway to read.

mnemos-core ships the ops surface for this:
`systemd/pantheon-catalog-sync.service` and `.timer` (daily, `Persistent=true`,
randomized delay), `scripts/refresh_pantheon_catalog.py` as the unit's
entrypoint, and `mnemos/tools/refresh_pantheon_catalog.py` as the console-script
shim that exits 2 with an install hint when the add-on is absent.

## Correctness constraints folded into the unification

1. **Endpoint routing by model.** Reasoning/codex-class models require
   `/v1/responses`; the rest use `/v1/chat/completions`. The wrong endpoint is a
   400, so the choice is a property of the resolved model, not a global setting.
2. **Reasoning-model token budget.** The output budget must be high enough that
   the answer is not truncated (configurable, default ≥8000) because reasoning
   tokens consume it.
3. **Tool-call passthrough.** OpenAI `tools`, `tool_calls`, and tool results
   pass through faithfully, streaming included — no dropped or mangled function
   arguments.
4. **Telemetry names the real wire model.** Each request stamps its resolved
   model, never a process boot-seed default.
5. **Routing audit lands in a table.** The durable destination is
   `pantheon_routing_audit`, not the memory store, through a backend-aware
   consumer rather than Postgres-only parameter and cast syntax. mnemos-core
   ships that migration for PostgreSQL, SQLite, Oracle, and Db2.

## Shipped

**Catalog sync.** Pricing ingest, timer-driven regeneration, and last-good
retention. Evidence: the catalog-sync unit and timer, the refresh script, and
the console-script shim named above.

**Shadow gateway.** The OpenAI-compatible gateway runs as
`mnemos.api.pantheon_shadow:app` — single-process on a shadow port for dev,
and as a gunicorn/uvicorn worker pool bound to loopback for production, with
the reverse proxy keeping the public VIP stable. `deploy/pantheon/` carries the
validated launcher, the environment template, the systemd unit, a review-only
proxy site snippet, and the cutover/rollback README.
`scripts/pantheon_shadow_smoke.py` exercises health, chat, tool-call
passthrough, and `/responses` routing against a running gateway.
`MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT` is a first-class setting in
`mnemos/core/config.py`, alongside the rest of the `MNEMOS_PANTHEON_*` surface
(caps, policy weights, upstream timeout, catalog cache paths, passthrough
pricing, audit queue sizing).

**Routing audit to a table.** The `pantheon_routing_audit` schema ships for all
four supported backends, and the audit consumer is a named service
(`pantheon_routing_audit_consumer`) enabled with the `pantheon` component.

**Extraction.** Both planes now live in their own distributions, replacing the
earlier in-tree arrangement. The budget-unification wiring itself —
PANTHEON's pre-dispatch call into the KNEMON ledger — lives inside those
distributions; mnemos-core sees only the extras and the seams.

## Open

**VIP cutover.** Moving the stable public VIP from its pre-cutover upstream to
the PANTHEON worker pool is operator-orchestrated and remains outstanding. It
is deliberately not an automated job: the VIP is on the critical path for every
LLM-calling client, so the sequence is shadow-validate, mirror a slice of real
traffic and compare results/cost/latency, flip the proxy upstream, and keep the
previous upstream hot as a one-line rollback. PANTHEON is purely additive until
the flip.

**Pre-flip gate.** Before the flip:

- `MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT` is set to a fleet-scale value (or
  disabled while measuring parity against the old upstream). A process-local
  single-worker default is not a valid cutover limit.
- The gateway runs as a worker pool, not one process. A single-worker
  throughput ceiling is a capacity finding, not the target topology.
- Cooldown, breaker, and request-limit state are shared through NATS/JetStream
  before any multi-worker run — process-local state is incorrect the moment the
  proxy fans out.
- A concurrent load test passes at fleet concurrency, recording p50/p95/p99 and
  requiring **zero spurious 429s** from the gateway rate limiter.

**Enablement posture.** PANTHEON is off in every default service profile,
including `server` — it is a niche model-proxy surface rather than required
substrate. Operators turn it on with the `pantheon` component selection (which
also enables the routing-audit consumer), the `full` bundle, or
`MNEMOS_PANTHEON_ENABLED` directly. Off-by-default is the intended posture, not
a hold pending a gate.

**Catalog source fragility.** Mitigated, not eliminated, by preferring
machine-readable primary feeds over scraping and by keeping a last-good cache;
an upstream schema change still degrades freshness.

## HEADROOM — lossless token compression

HEADROOM is a lossless token-compression library the gateway can call
pre-dispatch: fewer input tokens means lower per-request cost, which feeds
KNEMON affordability. It is a clean-room implementation of the lossless
transforms only — no ML, no proxy, no telemetry, no model-hub coupling.

It ships in mnemos-core at `mnemos/domain/headroom/`:

| Module | Contents |
|---|---|
| `json_minify.py` | `minify_json_text`, `is_json_lossless_equivalent`, `JSONMinifyError` |
| `code_strip.py` | `strip_fenced_code_lossless` — comment/whitespace removal inside fenced code blocks |
| `library.py` | `compress_text`, `compress_messages`, and an overloaded `compress()` accepting either a string or a message list, all returning `CompressionResult` |

`CompressionResult` reports `supported`, `changed`, `lossless`, the per-transform
`TransformRecord` trail, and derived `bytes_saved` / `compression_ratio`.
Numbers round-trip exactly — digit mutation is a hard failure, covered by
numeric property tests over large integers, high-precision decimals, scientific
notation, and zeros. Anything unsupported passes through untouched; if an
internal proof check fails, the library returns the original content rather than
a compressed one.

**The library is not yet wired into any call path.** Nothing in mnemos-core
imports `mnemos.domain.headroom` outside its own tests — the package docstring
states the intent plainly: it is a pure importable library, callers opt in by
invoking `compress` before sending messages or tool arguments to a provider.
Connecting it to PANTHEON's pre-dispatch path, and evaluating the
latency-versus-savings tradeoff live against the KNEMON cost model, is
remaining work.
