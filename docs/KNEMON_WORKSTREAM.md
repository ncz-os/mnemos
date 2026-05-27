# KNEMON Workstream — Canonical Job Spec (resubmittable)

**Purpose:** durable source-of-truth for the KNEMON workstream + post-KNEMON roadmap, so jobs can be **resubmitted to the hive if hive state is lost** (e.g. the 2026-05-27 corrupt-queue serve-stall that required a wipe+resubmit).

**Owner:** Studio Claude (KNEMON = MNEMOS Model-Intelligence + Token/Cost Ledger subsystem).
**Hive bus:** `http://192.168.207.67:5005` · **MNEMOS:** `http://192.168.207.67:5002`
**Branch:** `feat/knemon-mvp` (off `feat/db2-native`), repo `ncz-os/mnemos`.
**Last updated:** 2026-05-27.

---

## Rules (learned the hard way 2026-05-27)

1. **Work jobs → `eligible_kinds` includes `zeroclaw`** (so the fleet claims them). Root/parent/tracker + gated-deploy jobs → `["human","claude"]` ONLY (a zeroclaw worker will claim a no-op tracker and **fail** it — knemon-root did exactly this).
2. **File build jobs TOP-LEVEL (no `parent_job_id`).** A failed/orphaned parent broke the hive serve-query → 204 fleet-wide. Link to root in the description text, not via parent_job_id.
3. **A design job is not done until its build-step jobs are filed.** Design-done ≠ work-queued.
4. **Every code job gates on adversarial codex review + codex-fix-in-place** (directive #4+#7). `review:`/`codex:`/`security:`/`audit:` kinds route to `codex_cli_run` (codex exec via ChatGPT OAuth — works).
5. **Resubmit template:**
   ```bash
   BUS=http://192.168.207.67:5005
   URN=$(curl -sS -XPOST $BUS/v1/agents/register -H 'content-type:application/json' \
     -d '{"kind":"claude","host":"studio","capabilities":["fleet-orch","mnemos"],"version":"opus-4.7"}' \
     | python3 -c "import sys,json;print(json.load(sys.stdin)['urn'])")
   curl -sS -XPOST $BUS/v1/jobs -H 'content-type:application/json' -d '{
     "submitter_urn":"'"$URN"'","kind":"<KIND>","priority":<P>,"max_cost_tier":"C",
     "eligible_kinds":[<ELIG>],"description":"<DESC>"}'
   ```

---

## KNEMON MVP build steps (branch `feat/knemon-mvp`)

| # | kind | elig | prio | deps | spec |
|---|---|---|---|---|---|
| 1 | (committed `1c0566d`) | — | — | — | `usage_ledger` table (0032 migration, 3 dialects PG/Oracle/Db2 — parity required), `mnemos/api/routes/ledger.py` `/v1/ledger` POST (Pydantic, tokens≥0, outcome enum, est_cost server-side from model_registry), persistence base/postgres. **DONE.** |
| 2 | `mnemos:knemon-build-step2-price-ingest` | zeroclaw,codex,claude | 12 | 1 | Extend `scripts/sync_provider_models.py` + `mnemos/domain/graeae/provider_sync.py`: parse `llm_provider_registry.json` input/output/cached price_per_mtok; add `model_registry` price_in/out/cached + price_updated_at cols + `price_history`; UPSERT on sync. **DONE (incl price-merge `2f93a4e`/`9b77374`).** |
| 3 | `mnemos:knemon-build-step3-triage-formula` | zeroclaw,codex,claude | 11 | 1 | `mnemos/domain/pantheon/triage.py`: `score=(perf_rank*0.55+newness_norm*0.25)/max(price_per_mtok,0.01)*ctx_factor`; pantheon picks argmax for (task_kind,priority). **DONE (`aad517a` — codex fallback after hive phantom-commit; 3/3 tests pass).** |
| 4 | `mnemos:knemon-build-step4-mnemos-llm-wrapper` | zeroclaw,codex,claude | 10 | 1 | `mnemos/llm.py` `call(task)`: `model=pantheon.route(task); try invoke finally ledger.record(...)` — finally guarantees ledger write even on exception. Wire `provider_registry`. **DONE (`aad517a`).** |
| 5 | `mnemos:knemon-build-step5-pantheon-wire-integration` | zeroclaw,codex,claude | 9 | 3,4 | Wire pantheon route → ledger.record end-to-end + integration smoke on Postgres + KNEMON docs. **DONE (wiring already realized in `mnemos/llm.py::call` per step 4 = `aad517a`; integration smoke on LIVE Postgres is gated on step G [human-only PG migration]; docs = this update).** NOTE 2026-05-27: hive workers no-op-closed 10+ step5 instances (zero commits) because no code remained to write — wiring landed in step 4. step5 re-files STOPPED. |
| G | `mnemos:knemon-deploy-live-pg-migration-grant-GATED` | **human** | 8 | 2 | **G1 GATED, operator-only:** apply 0032 on LIVE PYTHIA PG + `GRANT INSERT,UPDATE ON model_registry,usage_ledger,price_history TO mnemos_app`. Explicit GO required. |

> **KNEMON MVP build COMPLETE (2026-05-27)** — steps 1-4 landed (`1c0566d` ledger schema/recorder, `2f93a4e`/`9b77374` price ingest, `aad517a` triage formula + `mnemos.llm` wrapper). step5 wiring is in `llm.py::call`; its live-PG integration smoke is gated on step G (human). Remaining before KNEMON is production-live: step G (operator applies 0032 on PYTHIA PG + grants) → then Phase-1 48h ledger baseline → then post-KNEMON roadmap (hive→Oracle port, refactor). The codex `019e6b03-6714` "commit cc8cf7ff" was a hive-worker phantom (committed to its own clone, never pushed — bad object on canonical); real landing = `aad517a`.

## KNEMON phases (token-tracking workstream — ref `fleet-ops/docs/HIVE_TOKEN_TRACKING_WORKSTREAM.md`)

| phase | kind | elig | prio | deps | spec |
|---|---|---|---|---|---|
| 1 | `fleet-infra:hive-token-tracking-instrument-baseline` + `doctor:codex-fix:knemon-phase1a-token-tracking-fields` | zeroclaw | — | — | Instrumentation: record token/cost per external-model call + 48h baseline. **DONE (`0fd65cd5`).** |
| 2 | `mnemos:knemon-phase2-silent-coder-rollout` | zeroclaw,codex,claude | 8 | MVP triage+wrapper | Route coding/refactor/code-fix through cost-optimized silent path (cheap primary, escalate via triage/RouterProvider). |
| 3 | `mnemos:knemon-phase3-tier-split-b1b2c1c2` | zeroclaw,codex,claude | 5 | Phase1 ledger data | Subdivide cost tiers B→B1/B2, C→C1/C2 in model_registry + pantheon routing, informed by usage_ledger data. |
| 4 | `mnemos:knemon-phase4-ab-test-rule-lift` | zeroclaw,codex,claude | 3 | Phase3 | A/B test tier routing + lift dispatch rules from usage_ledger cost/quality data (data-driven dispatch matrix). |

## Supporting jobs

| kind | elig | status | spec |
|---|---|---|---|
| `fleet-infra:knemon-root` | human,claude | tracker | Workstream parent tracker. **Orchestrator-only — never zeroclaw** (worker claims + fails a no-op tracker). |
| `mnemos:model-intelligence-ledger-design` | — | DONE | KNEMON design (`knemon-design-draft.md`). |
| `mnemos:knemon-fix-model-sync-ingest` / `-fix-recommend-endpoint-empty-bug` / `-fix-capability-taxonomy` / `-price-merge-into-model-registry` | — | DONE | Prereq fixes (committed). |
| `graeae:knemon-direct-codex-worker` | — | DONE | codex_worker.py + systemd (`886d20c`). |
| `ncz-os:zeroclaw-worker-adversarial-review-gate` | zeroclaw,codex,claude | 14 | — | Wire post-commit adversarial-review gate into `zeroclaw_wss_worker.py` (default-on for code-fix/build/refactor/fix:/security:/feat; codex fixes in place per #7; re-review until approve). |
| `review:knemon-mvp-adversarial` | zeroclaw,codex | 7 | MVP steps | Retro adversarial review of all KNEMON code, codex-fix-in-place. |

---

## Post-KNEMON roadmap (order: hive→Oracle, THEN refactor) — GATED until KNEMON MVP complete

| # | kind | elig | spec |
|---|---|---|---|
| 1 | `fleet-infra:post-knemon-1-hive-to-oracle-port-GATED` | human,claude | Port Hive SQLite (`/srv/agent-bus/agents.db` PYTHIA) → Oracle 23ai (PYTHIA ORCLPDB1, DG primary → DR via CERBERUS standby). (C) extract shared persistence; just-enough Oracle backend for hive tables (agents/jobs/messages); (D) migrate data; (E) cutover Oracle primary. Hardens vs SQLite serve-stall. Decompose to zeroclaw sub-jobs when ungated. |
| 2 | `fleet-infra:post-knemon-2-mnemos-refactor-optimization-GATED` | human,claude | Per Codex audit (`mnemos-audit-2026-05-27.md`): BLOCKER = 4-backend persistence over-promised (Oracle/Db2 no-op/NotImplementedError @ oracle.py:2152-2234, db2.py:3547, base.py:1214) → implement or honestly gate; complete backend parity; modularization; sanitary pass; Rust-port candidates. Each sub-job gates on adversarial-review + codex-fix-in-place. |

---

## MNEMOS references
- `mem_1779909571771_6b58f5` — ai-tools-router / tier-router study (Work Claude)
- `mem_1779910921458_cac7e0` — KNEMON-zeroclaw-eligible policy
- `mem_1779911007818_1d70c5` — design-done≠work-queued lesson
- `mem_1779911945496_781f0c` — hive claim-stall incident + fix
- `mem_1779912376114_96ab98` — adversarial-review + codex-fix-in-place policy
- `knemon-design-draft.md`, `mnemos-audit-2026-05-27.md` (STUDIO ~)
