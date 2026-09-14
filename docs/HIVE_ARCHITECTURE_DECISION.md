# Hive Architecture Decision — staged cutover agent_bus.py → mnemos/hive_mind

**Decided:** 2026-06-01 · **Authority:** GRAEAE consult (8 muses, consensus 0.89, winner gemini) · directive #2 (GRAEAE-first) + directive #10 (triple-persistence).

> **Superseded — retained as a decision record.** The staged in-tree cutover
> below was not the outcome. The hive was **extracted**, not cut over: it ships
> as the standalone **STIPHOS** distribution (`mnemos-stiphos`) with its own
> ASGI app, port, and image, deployed alongside `mnemos-core` rather than
> living in `mnemos/hive_mind/`. There is no `hive` extra on `mnemos-core`;
> `mnemos/core/extras.py::UNAVAILABLE_EXTRAS` records the carve-out explicitly.
> See [LAYERED_INSTALL.md](LAYERED_INSTALL.md) and
> [MNEMOS_CORE_BOUNDARY.md](MNEMOS_CORE_BOUNDARY.md) for the shipped topology.
>
> The design reasoning below survived the change of vehicle and is kept for the
> record — in particular decoupling the application cutover from the database
> cutover, and reconciling the cost-tier claim-gating drift before any traffic
> moves.

## Context

Two parallel codebases run agent-job coordination:
- **(A) LIVE** = `/srv/agent-bus/agent_bus.py` (the primary, `graeae-hive.service`, :5005) — 138KB monolith, **no mnemos import**, SQLite `agents.db`. Battle-tested, drives the real fleet. Intentionally has **no cost-tier claim gating** (all executors mutually eligible).
- **(B) REFACTOR** = `mnemos/hive_mind/` (repository + service + triage) — ABC-based, Oracle/Postgres/SQLite-portable, validated through 5 codex adversarial cycles + ~128 tests. **NOT deployed.** Adds cost-tier/pool claim gating.

They have drifted. Post-KNEMON roadmap requires porting the hive SQLite → Oracle AI Database 26ai.

## Decision: Option C — STAGED cutover to `mnemos/hive_mind`

Deprecate `agent_bus.py`; cut the live `:5005` bus over to `mnemos/hive_mind` — but **not big-bang**.

**Why (decisive trade-offs):**
1. **Oracle migration is the forcing function.** The ABC persistence layer in `hive_mind` is the only sane path to Oracle AI Database 26ai. Keeping the monolith means eventually ripping a SQLite-coupled monolith apart to bolt on Oracle — massive risk, duplicates the refactor already done.
2. **Big-bang (Option B) is reckless** — the cost-tier-gating drift would starve/reject currently-functioning live agents. (This is why blind-porting the gating into the live bus was avoided.)
3. **Two parallel layers = slow death** — the 128 tests + 5 codex cycles decay every day they aren't the live path.

**Execution constraints (from the consult):**
- **Decouple application cutover from database cutover.** Cut the app (agent_bus → hive_mind) on SQLite first; migrate SQLite → Oracle as a separate step.
- **Reconcile the business-logic drift BEFORE cutover** — especially the cost-tier claim gating: `hive_mind` enforces it, the live bus does not. Decide the canonical behavior (likely: keep all-executors-eligible by default, make gating opt-in) so the cutover doesn't starve live agents.
- Preserve the live bus's host-affinity + worker-only-submitter guard semantics (already correct in agent_bus).

## Status at the time of the decision
- Refactor `mnemos/hive_mind` landed on master (`ab31d77`), validated, green.
- Not yet the live bus. Cutover was planned as a staged workstream (app-cutover-on-sqlite → drift-reconcile → oracle-migration), each step gated on adversarial review.

That staged cutover was overtaken by the core/add-on carve-out: the
`hive_mind` package left this tree with the rest of the platform services and
became STIPHOS. See the note at the top of this document.
