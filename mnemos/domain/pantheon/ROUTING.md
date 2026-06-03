# PANTHEON routing (provider routing · retry · fallback · cooldown)

This package adds resilient, cost-aware provider routing to PANTHEON by adopting
LiteLLM's proven routing **patterns** — reimplemented natively, with **no
separate datastore**: all durable state flows through the mnemos persistence ABC
(architectural law). It exists to make two real production outages impossible:

1. A non-retryable `400` from one model was retried and cascaded until it stalled
   every job. → a non-retryable error now **falls over instantly** and never
   consumes the retry budget.
2. One broken lead model stalled the whole group. → fallback is **bounded and
   cross-deployment** so a dead lead is bypassed.

It also fixes gateway latency: the gateway now uses a **pooled HTTP client**
(connection reuse) instead of a fresh `httpx.AsyncClient` per request.

## Module map (`mnemos/domain/pantheon/`)

| module | responsibility | purity |
|---|---|---|
| `errors.py` | error taxonomy · `normalize_error` · `is_retryable_status` · `decide` (RETRY/FALLOVER/RAISE) | pure |
| `backoff.py` | `compute_backoff` (exp + jitter + Retry-After + instant-sibling) | pure |
| `fallback.py` | `execute_with_fallbacks` — in-group retry + cross-group fall-over | pure async (DI) |
| `cooldown.py` | `evaluate_cooldown` · `CooldownStore` ABC (cache-aside) · `InMemoryCooldownStore` · `CooldownManager` | pure + L1 store |
| `runtime.py` | `RouterRuntime` — cooldown pre-filter + fallback + post-call recording | pure async (DI) |
| `http_bridge.py` | `classify(exc)` (httpx/gateway → `NormalizedError`) · `retry_after_seconds` | pure |
| `chains.py` | `resolve_chain` (model-group → ordered deployment chain) — **not yet wired** | pure |
| `gateway.py` | wired: `forward_chat_completion` routes through `RouterRuntime`; pooled `get_http_client()` | I/O |

## Request flow (chat completion)

```
router (alias → RouteDecision)
  → gateway.forward_chat_completion
      → RouterRuntime.route([decision], _forward_chat_once, classify)
          → CooldownManager.filter_available   (drop cooled deployments)
          → execute_with_fallbacks             (retry transient w/ backoff; fall over non-retryable)
              → pooled httpx POST              (get_http_client, keep-alive reuse)
          → record_success / record_failure    (feeds the breaker)
```

Non-retryable errors surface as the original `PantheonGatewayError` (the
`AllDeploymentsFailed` wrapper is unwound to its `last_exception`), so the API
error surface is unchanged.

## Defaults (from the LiteLLM pattern spec)

- retryable iff status ∈ `{408, 409, 429}` or `>= 500`
- `num_retries = 2`, `max_fallbacks = 5`
- backoff: `INITIAL 0.5s`, `MAX 8.0s`, `JITTER 0.75`; instant retry when a healthy sibling exists; honor `Retry-After` if `0 < ra <= 60`
- cooldown: `5s`; trips on `429` / permanent `401`·`404` / failure-rate `> 50%` over `>= 5` reqs; **never** on a plain `400` or `APIConnection`; **never** cools a single-deployment group
- HTTP pool: `max_keepalive=50`, `max_connections=200`, `keepalive_expiry=30s`

## Enabling cross-provider fallback (deferred — needs sign-off)

Today the gateway passes a **single-element chain** (`[decision]`) → behavior is
identical to pre-redesign routing, plus transient retry + cooldown. To enable
true cross-provider fall-over:

1. Build a registry `{group: [Deployment, ...]}` and a fallback map
   `{group: [backup_group, ...]}` (plus generic `"*"`).
2. `chain = chains.resolve_chain(primary_group, registry, fallbacks)`.
3. Map each `Deployment` → `RouteDecision`, then
   `RouterRuntime.route(chain, _forward_chat_once, classify=...)`.

This **changes routing semantics**, so it is gated on operator / GRAEAE sign-off
(it is not done unattended).

## GRAEAE mandate status (design: MNEMOS `mem_1780459842961`)

Done:
- cache-aside `CooldownStore` contract (logical TTL = `cooled_until` compared at
  read; minute-bucketed atomic counters; keyed by `(tenant, deployment)`)
- non-retryable gate · bounded fallback · per-model cooldown
- tenant-scoped cooldown (one tenant's bad BYOK key never cools another)
- pooled HTTP + clean shutdown hook

Deferred (need operator presence / sign-off):
- Oracle/SQLite `CooldownStore` concrete (durable write-behind tier behind the L1)
- chains wiring into `router.py` (cross-provider fallback)
- multi-tenancy threading (BYOK key vault, per-tenant budgets, fair-share dequeue)
- `agent_bus.py` queue fixes on the live bus (UNIQUE dedup_hash, exp-backoff
  `next_retry_at`, HARD vs SOFT capability labels + load-signal + DLQ-with-reason)

## Tests

```
cd <worktree>
PYTHONPATH=$PWD .venv/bin/python -m pytest tests/test_pantheon_*.py -q --noconftest
```

All routing modules are pure / dependency-injected, so the suite runs with no
network and no real sleeping.

## Cooldown storage tiers

- `cooldown.py` — `CooldownStore` ABC + `InMemoryCooldownStore` (process-local L1) + `CooldownManager` + pure `evaluate_cooldown`.
- `cooldown_sqlite.py` — `SqliteCooldownStore`: durable backend (logical-TTL cooldowns, minute-bucket counters via `ON CONFLICT` upsert, tenant-scoped, WAL). Survives process restart.
- `cooldown_cache.py` — `WriteBehindCooldownStore`: L1 + durable backend. Cooldowns read-through (survive restart); counters L1-authoritative per process+minute; writes hit L1 immediately and flush to durable in batches (auto at threshold or via `flush()` on a timer). Satisfies the GRAEAE cache-aside mandate.
- Production wiring: `CooldownManager(WriteBehindCooldownStore(SqliteCooldownStore(path)))` — swap SQLite for an Oracle `CooldownStore` to ride the persistence ABC. End-to-end composition proven in `tests/test_pantheon_integration.py` (trip → persist → fallover → restart-recovery → TTL expiry).
