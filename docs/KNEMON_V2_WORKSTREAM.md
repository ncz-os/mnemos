# KNEMON v2 — Cost-Conscious Multi-Provider Router (Workstream + Spec)

**Decided:** 2026-06-01 · operator + GRAEAE consult (directive #2) · directive #10 triple-persist.
**Target:** the `mnemos/hive_mind` cutover path (NOT the live `agent_bus.py`; see `HIVE_ARCHITECTURE_DECISION.md`).
**Supersedes** the per-family-Elo routing in `mnemos/domain/knemon/router.py`.

## Why

KNEMON ranked 399 models by per-family Arena Elo → strong open-weight coders stranded in tier C, prices stale/$0, no per-provider load-spread/billing-caps/rate-limits, and the spend dashboard is empty because the live gateway dispatch never writes `usage_ledger`. Operator requires: maximally cost-conscious AND effective, least error-prone, load spread across providers without overextending any one, rate-limit aware, with real per-provider token/cost tracking in the dashboard.

## Providers (canonical set)

`openai` (one $100/mo OAuth pool — codex 5.1–5.3, gpt-5.5 share it), `xai`, `gemini`, `deepseek-direct`, `siliconflow`, `amazon-bedrock`, `groq`, `together`; **`nvidia` ONLY on the GB10 Spark** (remote-dispatch, host-locked). **Anthropic + Claude = same vendor, BANNED at ingress. Perplexity dropped.**

## Architecture (one model_registry + one usage_ledger)

```
PRICING SYNC (daily timer — also fixes failed mnemos-model-sync.service)
  litellm model_prices_and_context_window.json  (canonical, all providers)
  + provider inline pricing (Together returns it)  + manual override (newest models / our contract)
  → model_registry.price_in/out/cache + price_history

ROUTING — curated golden_path (NOT Elo)
  intent classifier → required capability + quality floor
  per-model: caps{code,reason,vision,tools,online,long_ctx} ranks · size_class · context
  DIRECT-provider-first: deepseek→deepseek-direct, gemini→gemini, openai→pool, xai→xai
    load-balance ONLY resold open models (qwen3-coder, GLM, Kimi, gpt-oss) across
    resellers (bedrock/together/Spark) by cheapest-healthy
  GATES: ban {anthropic,claude} · nvidia→Spark-only · min_params size-gate for code intents
         (NO 8B/32B for fix:/feat:)

GOVERNOR (per provider; separate service or shared Redis lib)
  monthly budget + spend tracker → circuit-breaker near cap (provider drops out)
  rate-limit tracker (rpm/tpm) → backoff + spill to next cheapest-healthy provider

USAGE-LEDGER PIPELINE  ← fixes the empty dashboard
  done-job result already carries tokens_total/tokens_out + gateway_provider + gateway_model
  → cost = tokens × synced price → usage_ledger row
  → consumers: (a) DASHBOARD token/$ per provider/day  (b) GOVERNOR spend→caps  (c) ROUTING cost-feedback
```

## Golden Path (intents × model + cost-ranked provider; prices live-verified 2026-06-01)

| Intent | needs | path (cheapest-healthy, direct-first) |
|---|---|---|
| `code:agentic` (multi-file, build/test) | code-hi + tools + ≥large | `gpt-5.3-codex` (OAuth, gated) → `deepseek-v4-pro` (deepseek-direct) → `qwen3-coder-480b` (**Spark** free / bedrock $0.22 / together $2.00) → `devstral-2-123b` (bedrock) |
| `code:edit`/`code:gen` | code-mid | `deepseek-v4-flash` (deepseek-direct) → `qwen3-coder-30b` (bedrock $0.15 / siliconflow) → `gemini-3.1-pro` ($2/$12) |
| `code:review`/`code:debug` | code+reason hi | `gpt-5.3-codex` → `deepseek-v4-pro` (direct) |
| `code:test`/`code:docs` | code-low | `deepseek-v4-flash` (direct) → `gpt-oss-120b` (groq $0.15/$0.60) |
| `reason:architecture`/`plan` | reason hi + long-ctx | `deepseek-v4-pro` (direct) → `gemini-3.1-pro` → `GLM-5.1`/`Kimi-K2.6` (bedrock/together) |
| `text:summarize`/`classify`/`extract` | low | `gemini-3.5-flash` ($1.5/$9) → `llama-3.1-8b-instant` (groq $0.05/$0.08) → `nova-2-lite` (bedrock) |
| `vision` | vision | `gemini-3.1-pro` → `qwen3-vl` (siliconflow/bedrock) → `nova-pro` |
| `web/online` | online | `gemini` (grounding) → `gpt-5.5-search` |
| `embed` | — | **`bge-m3` (PEGASUS GPU, 1024d — matches stored vectors)** |
| `rerank` | — | `Qwen3-Reranker-8B` (siliconflow) |

Notes: deepseek models → **deepseek-direct only** (never the Together/SiliconFlow/Bedrock deepseek routes — direct is first-party + cheaper). `qwen3-coder-480b` price spread ~9× across providers → governor picks cheapest with headroom. codex line bills the one OpenAI pool; circuit-break at $95 → fall to deepseek-direct.

## Build jobs (sequenced, each gated on adversarial review + codex-fix; directive 4/7)

1. **Schema:** `golden_path` table (intent, model_id, caps ranks, size_class, internal_rank, host_lock); per-provider `provider_budget` (monthly_cap, rpm, tpm) + `provider_spend` (rolling); extend `model_registry` (per-model per-provider price rows). ABC migrations across backends.
2. **Pricing sync:** fetch litellm JSON + Together inline + override → registry prices + `price_history`; **fix `mnemos-model-sync.service`** (currently failed); daily systemd timer.
3. **Usage-ledger writer:** **PENDING→COMPLETED with deterministic `job_id` as idempotency key** — write a `PENDING` row BEFORE dispatch, update to `COMPLETED` with actual tokens/cost post-job. Prevents missed rows (crash after provider bills) + double-counting (retries). Cost via synced price. **Fixes the dashboard.**
4. **Governor (embedded LIBRARY inside the router, shared Redis — NOT a microservice; no network hop on the hot path):** **Reserve & Commit (two-phase) via atomic Redis Lua** — reserve `est_max_tokens × price` BEFORE routing, release-unused + commit actual on completion (the naive check-before/deduct-after lets a burst blow the cap). Rate-limit tracker with **hysteresis: a rate-limited provider is marked degraded for a fixed 5-min penalty window** (not just until the TPM bucket empties) to stop flapping.
5. **Router rewrite:** golden_path intent→caps routing, direct-first + reseller load-balance, gates (ban/host/size), governor-aware (reserve) provider selection. **`No-Price-No-Route` hard gate: a model with null/$0 synced price is refused (or assigned a safety-ceiling price) so it can never bypass the governor.** Replace Elo path. **#5242 guard: cap open-weight→openai fallback volume so an open-weight spike can't drain the $100 OpenAI pool in hours.**
6. **Dashboard wiring:** aggregate `usage_ledger` → token/$ per provider/day + per-provider budget burndown.

## Open dependency

Open-weight dispatch is blocked live by **zeroclaw gateway #5242** (custom endpoints fall back to openai despite valid keys + base_url). Until it lands, the open-weight tiers are aspirational; codex (OAuth $0) carries code, gemini/groq carry standard/triage. The curation is still correct — it activates fully when #5242 unblocks. Repro logged `~/zeroclaw-singlerider-log.txt`.

## GRAEAE validation (2026-06-01, 8 muses, consensus 0.89)

**Verdict: NO-GO on the naive design** — routing/intent logic is solid, but the financial-governance pipeline has critical race conditions; the post-facto cost model makes the circuit-breaker illusory under concurrent load. **Build-ready once the 3 must-fixes (now folded into jobs 3–5 above) are implemented:**

1. **Atomic Reserve/Commit** in the Redis governor (Lua) — reserve estimated cost upfront, true-up on completion. *(job 4)*
2. **No-Price-No-Route gate** — null/$0 price → hard-refuse, else $0 cost bypasses the governor → infinite spend. *(job 5)*
3. **Hysteresis** — 5-min penalty cooldown on any rate-limited provider to stop cheapest↔secondary flapping. *(job 4)*

Other risks captured: ledger PENDING→COMPLETED idempotency for missed/double rows *(job 3)*; governor embedded as a library not a service *(job 4)*; **#5242 fallback threat** — open-weight spikes draining the OpenAI pool in hours *(job 5 guard)*.
