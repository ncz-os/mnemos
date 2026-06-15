# mnemos core vs add-on boundary — RATIFIED

**Status:** Decision ratified by operator 2026-06-15. GRAEAE consult
`c1f5cda104864e568d216390200395be` (mode=all, winning muse gemini, consensus 1.0;
groq/claude/perplexity/xai/gemini concurred).

## Decision

`mnemos` is a **memory system**. PANTHEON, KNEMON, GRAEAE, and HIVE are **separable
platform services**, not core memory primitives. They will be **extracted to the
`ncz-os` org** (the existing org `mnemos` already lives under on gitlab + github +
codeberg) as sibling packages depending on a carved-out **`mnemos-core`**.

### Core (stays = `mnemos-core`)
Persistence (sqlite/postgres/oracle/db2/mysql), federation, search, embeddings,
recall/decay, MCP memory tools, audit chain, secret-vault, namespace enforcement,
and the shared primitives those need (config, lifecycle, **resilience, rate-limit,
NATS transport, cache**). Memory-serving extras stay too (morpheus/persephone/
kronos/knossos/hot).

### Add-on products (extract → `ncz-os`)
- **PANTHEON** — multi-provider LLM dispatch gateway → `ncz-os` networking/gateway.
- **KNEMON** — budget/cost plane (serves pantheon) → `ncz-os` resource-mgr; the hive
  control-plane *calls* it for routing.
- **GRAEAE** — agent coordination (muse-consult panel) → `ncz-os`.
- **HIVE** — job bus + dispatch + control-plane + native executor + worker → `ncz-os`.

Code signal at decision time: `EXTRA_PROBES` already gated `pantheon`; `knemon`,
`graeae`, `hive` were loose in-tree (not gated) — an inconsistency this corrects.

## Sequencing (strangler-fig — reversible first)

1. **Phase 1 — in-tree decouple (reversible, do first):**
   - `mnemos` must NEVER import pantheon/knemon/graeae/hive. Invert dependencies:
     inject `Embedder` / `BudgetTracker` (and any LLM-call provider) at runtime.
   - Add `knemon`, `graeae`, `hive` to `EXTRA_PROBES`; move them behind the same
     optional-extra boundary as pantheon.
2. **Phase 2 — carve `mnemos-core`:** strip to pure storage/vector/index/retrieval +
   the shared primitive surface; the add-ons depend only on its public API.
3. **Phase 3 — extract to `ncz-os`:** new sibling packages depending on `mnemos-core`;
   move PANTHEON/KNEMON/GRAEAE/HIVE out of the mnemos tree.

Operator ratifies before code physically moves; Phase 1 stays in-tree and reversible.
**codeberg is rebuilt after the gitlab+github reorg**, not during.

## Division of labor (two concurrent Claudes, coordinated on hive bus `coord.hive-build`)

- **This track (mnemos/pantheon):** `mnemos-core` import boundary, `EXTRA_PROBES`
  gating for knemon/graeae, dependency-inversion (inject Embedder/BudgetTracker),
  pantheon/knemon/graeae library decouple.
- **Hive-build-redesign track:** the HIVE build fabric — bus + control-plane + native
  executor (replaces `zc_oneshot`/WS-direct) + worker — extracted to `ncz-os` on
  `mnemos-core`. Holds all worker/zc_oneshot/build-hook changes until cutover drains.

Joint open item: agree the exact `mnemos-core` public API surface that
hive/graeae/knemon/pantheon all depend on before the dependency-inversion lands.

## `mnemos-core` public surface (proposed, derived from actual add-on imports)

The de-facto surface the add-ons import today (audit of `mnemos/domain/{pantheon,knemon,graeae}`):

| Module | Used for |
|---|---|
| `mnemos.core.config` (`get_settings`, `GRAEAE_CONFIG`) | settings/config access |
| `mnemos.core.extras` (`is_extra_installed`, `require_extra`, `missing_extra_detail`) | optional-extra gating |
| `mnemos.core.numeric` (`safe_float`) | numeric helpers |
| `mnemos.core.lifecycle` (cache helpers, `_lc`) | shared cache / lifecycle |
| `mnemos.core.provider_registry` (`GRAEAE_REGISTRY_MAP`) | provider/model registry |
| `mnemos.core.plan_windows` (`compute_plan_window_id`) | plan-window math |
| `mnemos.core.resilience` (`CircuitBreakerPool`, NATS breaker) | resilience primitives |
| `mnemos.core.rate_limit` (`limiter`) | HTTP rate limiting |
| `mnemos.persistence.base` (backend `Protocol`, `UsageLedgerRecord`) | persistence interface |

These define `mnemos-core`. Rules for the extracted packages:
- Add-ons import **only** from this surface (no reach into private core internals).
- **Core never imports add-ons** (verified clean today). Phase-1 ✅.
- **Phase-1 step-2 (dependency-inversion):** the few places core *would* need an add-on
  (embedding generation, budget tracking) take an injected `Embedder` / `BudgetTracker`
  Protocol at runtime instead — defined in `mnemos-core`, implemented by morpheus/KNEMON.
- HIVE build-fabric (bus/executor/worker/control-plane) depends on the same surface; its
  extra registers like the others (`hive` for control-plane; optional separate `build`
  for executor/worker — TBD with the hive-build track).

### Phase-1 status (this track)
- ✅ `EXTRA_PROBES` registers `knemon`/`graeae`/`hive` (`5a8ce3f`).
- ✅ Import boundary verified clean (core hard-imports zero add-ons).
- ✅ KNEMON routes gated via `extra_guards.require_extra` → 503-when-disabled (`3a2a879`, codex-approved).
- ✅ Step-2 audit done. The dependency-inversion is **almost entirely moot**: core has
  **one-way** dependence already (add-ons import core, not vice versa). Embedding is
  `mnemos.runtime.embedder` (core-adjacent, in-process — not an extracted add-on), so no
  `Embedder` injection needed. Budget (KNEMON) is never imported by core. **Sole remaining
  coupling:** `mnemos/core/lifecycle.py::_close_graeae_engine_if_loaded()` lazy-imports
  `mnemos.domain.graeae.engine.get_graeae_engine` to close the engine at shutdown.
  - Fix (deferred, **bundle with Phase-3 extraction**): invert via the existing
    `_lifespan_cleanup_hooks` — GRAEAE registers its own engine-close hook; core stops
    importing graeae. Delicate (handles the "API-hook-registration-skipped" shutdown path)
    + harmless today (lazy import, doesn't break core import-time), so it is intentionally
    NOT changed unattended; it breaks only when graeae physically leaves the tree (Phase 3,
    operator-gated) and is fixed there.

Net: **Phase-1 is functionally complete.** Phase-2 (carve `mnemos-core` package) and
Phase-3 (move add-ons to `ncz-os`, incl. the one cleanup-hook inversion) are operator-gated
structural moves; `ncz-os` code-move stays operator-gated (confirmed with the hive-build track).
