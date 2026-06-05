# KNEMON Phase 3 — Tier Split B→B1/B2, C→C1/C2 — GATED

**Status:** BLOCKED — DO NOT EXECUTE  
**Job:** `mnemos:knemon-phase3-tier-split-b1b2c1c2`  
**Original attempt:** `019e6b08-40a4` (failed prematurely)  
**Date noted:** 2026-06-05

## Gate

**Phase-1 48h ledger baseline does not exist.** Phase 3 depends on `usage_ledger` data to inform the B→B1/B2 and C→C1/C2 tier subdivision. Without real cost/token data from production, the split thresholds would be arbitrary guesses.

## Root cause

Phase 1 instrumentation (`0fd65cd5`) is committed, but the production `mnemos-api` container has not been redeployed with the updated Oracle backend that writes `usage_ledger` rows. Until redeploy + 48h of accumulation, no baseline data exists.

## Prerequisites (before refile)

1. Rebuild + redeploy `mnemos-api` with the updated `mnemos-os:oracle` image (Oracle recorder is in `aad517a`).
2. Let `usage_ledger` accumulate ≥48h of production token/cost data.
3. Verify baseline data exists: query `usage_ledger` on PYTHIA ORCLPDB1 for a 48h window of rows.

## What Phase 3 will do (when ungated)

- Subdivide cost tiers B → B1 / B2 and C → C1 / C2 in `model_registry`.
- Update `pantheon` routing to use the finer-grained tiers.
- Thresholds informed by actual `usage_ledger` cost distributions (not guessed).

## Refiling

When the 48h baseline exists, refile this job as:  
`kind=mnemos:knemon-phase3-tier-split-b1b2c1c2`  
`eligible_kinds=[zeroclaw,codex,claude]`  
`deps=Phase1 ledger data (verified)`
