# MNEMOS Layered Install — Modular Deployment Architecture

**Status:** scaffolded on `knemon-hive-unified`, ready for Codex inspect + complete.
**Design authority:** GRAEAE consult 2026-06-01 (8 muses, consensus 0.89, winner gemini) on `de8f4b2b` Option-A layering. **Directive #2 (GRAEAE-first) satisfied.**

## Problem

The stack grew into a highly-layered system. Two **orthogonal** axes must be independently selectable; today neither is:

- **Feature layers (stack):** `core` (memory/persistence, always on) ← `graeae` (multi-muse reasoning) ← `hive` (job coordination + KNEMON cost/model routing)
- **Storage backend:** `sqlite` / `postgres` / `oracle` / `db2` / `mysql` (behind the persistence ABC)

A base install must pull **no** GRAEAE/hive deps and mount **none** of their routers. Not every operator wants all layers. Backend × layer support is uneven (Oracle/Db2 have `NotImplementedError` gaps).

## Design (GRAEAE consensus — DO this, not options)

1. **Optional install = Python extras + runtime feature-flags.** Single codebase, isolated deps.
2. **Conditional mount = lazy local imports** in the FastAPI app + a flag-driven router registry. If a layer is off, its third-party deps are never imported (no `ImportError` on base installs).
3. **Dependency direction enforced twice:** install-time (extras chaining: `hive` requires `graeae`) + runtime (`Settings.enforce_layer_direction` Pydantic validator).
4. **Honest backend gating = capabilities matrix** on the persistence ABC, **fail-fast at startup** if an enabled layer isn't supported by the chosen backend.

## Scaffolded in this commit (compiles, default-ON = full deploy byte-for-byte unchanged)

- `mnemos/core/config.py`: `_LayerSettings` (`enable_graeae`/`enable_hive`, env `MNEMOS_ENABLE_GRAEAE`/`_HIVE`, default `True`) + `Settings.layers` + `Settings.active_layers` + `enforce_layer_direction` validator (hive⇒graeae). Verified: rejects hive-without-graeae.
- `mnemos/api/main.py`: GRAEAE router (`consultations`) gated on `enable_graeae`; KNEMON/hive routers (`ledger`, `knemon_*`) gated on `enable_hive`.
- `mnemos/persistence/base.py`: `LAYER_REQUIRED_CAPABILITIES` + `backend_supported_layers()` + `assert_backend_supports_layers()` — derives layer support from the existing per-backend `capabilities` set (no per-backend edits needed).
- `pyproject.toml`: `graeae` + `hive` (chains `graeae`) layer extras; `full` updated.

## Gap list — Codex inspect + fix on `knemon-hive-unified`

**P0 — pre-existing router reconciliation (blocks merge to master):**
- `mnemos/domain/knemon/router.py` `route()` body came from a stale-base (bf0e166) rewrite and regresses `tests/domain/test_knemon_router.py::test_mid_priority_rejects_tier_c_ceiling` — a contract master ADDED in the 75 commits (priority-tier rejection must fire BEFORE capability-empty rejection). Re-apply ONLY the model-affinity wiring onto **master's** route(); keep the affinity helpers; `diff` route() vs `gitlab/master` to confirm no other master routing behavior was dropped. Full-green `test_knemon_router.py` + `test_knemon_capabilities.py`.

**P1 — finish layering wiring:**
- `main.py`: classify the remaining routers into layers (`pantheon`, `openai_compat`, `sessions`, `dag`, `webhooks`, `oauth`, `federation`, `kg`, …) — which are core vs graeae vs hive — and gate accordingly. Convert top-of-file router IMPORTS to lazy/local so a base install doesn't import GRAEAE/hive modules at all (currently they're imported even when gated off).
- `lifespan`/`mnemos.core.lifecycle`: gate layer init (GRAEAE warmup, hive triage/claim wiring) on the flags; call `assert_backend_supports_layers(backend, settings.layers.active_layers)` at startup (fail-fast).
- `LAYER_REQUIRED_CAPABILITIES`: pin the exact capability names per layer once the taxonomy is final (graeae⇒consultations persistence; hive⇒usage_ledger + hive_mind claim path). Make Oracle/Db2 `NotImplementedError` gaps surface as missing capabilities so gating is honest.
- `pyproject.toml`: move GRAEAE-only / hive-only third-party deps OUT of base `dependencies` into the `graeae`/`hive` extras (currently base pulls them).

**P2 — tests:**
- deployment-profile tests for each layer combo × backend: base-only mounts no GRAEAE/hive routes; `[graeae]` mounts consultations; `[hive]` mounts KNEMON; hive-without-graeae fails fast; unsupported backend×layer fails fast.

## Install matrix (target)

```
pip install mnemos-os[sqlite]                 # core memory only
pip install mnemos-os[graeae,postgres]        # + reasoning, on Postgres
pip install mnemos-os[hive,postgres]          # + coordination (pulls graeae)
MNEMOS_ENABLE_HIVE=0 MNEMOS_ENABLE_GRAEAE=0   # runtime slim, even if full installed
```
