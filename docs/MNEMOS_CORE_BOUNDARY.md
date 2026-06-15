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
