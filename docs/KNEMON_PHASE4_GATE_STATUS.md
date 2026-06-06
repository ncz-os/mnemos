# KNEMON Phase-4 A/B Test Rule Lift — PLANNING / STATUS NOTE (GATED)

**Date:** 2026-06-05  
**Job kind:** `mnemos:knemon-phase4-ab-test-rule-lift`  
**Status:** 🔴 **BLOCKED/GATED** — DO NOT EXECUTE  
**Eligible:** `[claude]` only (planning/status note — not executable by zeroclaw/workers)

---

## Gate Conditions (both required before Phase-4 execution)

### Gate 1: Phase-1 48h Ledger Baseline

Phase-1 (`fleet-infra:hive-token-tracking-instrument-baseline`, commit `0fd65cd5`) requires a **48-hour production usage ledger baseline** from the live Oracle backend. This baseline provides the token/cost data that Phase-3 and Phase-4 depend on for data-driven decisions.

**Current state:**
- The `usage_ledger` table exists on ORCLPDB1 (Oracle 0032 migration applied, 2026-05-28)
- The `OracleBackend.record_usage_ledger` recorder is committed to source (`mnemos/mnemos/persistence/oracle.py`)
- ⚠️ **The running `mnemos-api` container has the OLD oracle.py** — it must be rebuilt/redeployed before recording begins
- Once redeployed: accumulate 48 hours of production usage data → baseline exists

**Required action before Phase-4 ungating:**
1. Rebuild + redeploy `mnemos-api` container with updated `oracle.py` (recorder included)
2. Verify `usage_ledger` rows are being written in production (smoke test)
3. Let 48-hour baseline accumulate
4. Validate baseline data quality (non-zero rows, reasonable cost distribution)

### Gate 2: Phase-3 Tier Split (B1/B2, C1/C2)

Phase-3 (`mnemos:knemon-phase3-tier-split-b1b2c1c2`) subdivides cost tiers in `model_registry` and `pantheon` routing, informed by Phase-1 ledger data. Phase-4 builds on the refined tier model.

**Required action before Phase-4 ungating:**
1. Phase-3 design + implementation completed
2. Tier split deployed and validated against production ledger data
3. Pantheon routing updated to use B1/B2/C1/C2 tiers

---

## Phase-4 Scope (what we will build when ungated)

| Item | Description |
|------|-------------|
| **A/B test framework** | Dispatch rules that split traffic between routing strategies (e.g., baseline vs. candidate tier routing) |
| **Rule lift engine** | Extract dispatch rules from `usage_ledger` cost/quality data — identify which routing decisions improve cost-per-quality |
| **Data-driven dispatch matrix** | Build a dispatch decision matrix informed by historical cost and outcome quality |
| **Integration with Pantheon** | Wire A/B routing into `mnemos/domain/pantheon/triage.py` — Pantheon reads the dispatch matrix at route time |
| **Ledger tagging** | Tag `usage_ledger` rows with `experiment_id` / `rule_id` so A/B test results are measurable |

---

## Dependencies

```
Phase-1 (48h baseline)
    └── Phase-3 (tier split B1/B2/C1/C2)
            └── Phase-4 (A/B test rule lift) ← YOU ARE HERE (GATED)
```

---

## Refiling Instructions

When both gates are cleared (Phase-1 baseline exists AND Phase-3 is complete):

```bash
BUS=http://192.168.207.67:5005
URN=$(curl -sS -XPOST $BUS/v1/agents/register -H 'content-type:application/json' \
  -d '{"kind":"claude","host":"studio","capabilities":["fleet-orch","mnemos"],"version":"opus-4.7"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['urn'])")
curl -sS -XPOST $BUS/v1/jobs -H 'content-type:application/json' -d '{
  "submitter_urn":"'"$URN"'",
  "kind":"mnemos:knemon-phase4-ab-test-rule-lift",
  "priority":3,
  "max_cost_tier":"C",
  "eligible_kinds":["zeroclaw","codex","claude"],
  "description":"Phase-4 A/B test rule lift: A/B test tier routing + lift dispatch rules from usage_ledger cost/quality data. Depends on Phase-1 48h baseline (CONFIRMED) + Phase-3 tier split (CONFIRMED)."
}'
```

---

## References

- `docs/KNEMON_WORKSTREAM.md` — Canonical workstream spec, Phase-4 row (line 53)
- `fleet-ops/docs/HIVE_TOKEN_TRACKING_WORKSTREAM.md` — Token tracking workstream
- `mnemos/mnemos/persistence/oracle.py` — Oracle `record_usage_ledger` implementation
- `mnemos/mnemos/domain/pantheon/triage.py` — Pantheon triage formula (target for A/B dispatch)
- `mnemos/mnemos/llm.py` — LLM wrapper with `finally: ledger.record(...)` guarantee
- `db/migrations_oracle/0032_usage_ledger.sql` — Oracle schema for usage_ledger
