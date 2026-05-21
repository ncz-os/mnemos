# Handoff: MNEMOS Oracle Port (feat/oracle-port)

**Date:** 2026-05-19
**Branch:** feat/oracle-port
**Repo:** mnemos-production.git (ARGONAS)

## Objective
Port MNEMOS to support Oracle 26ai Free as a first-class persistence backend, including CHARON sidecar import/export and parity with the existing Postgres implementation.

## Current Status
- Oracle container + mnemos user running on PROTEUS
- 8157 memories imported via CHARON from production
- Core schema + surgical ALTERs for sidecar support
- OracleMemoryRepository with most required methods + factory
- `_repo_for` bridge implemented in portability layers
- Hard Postgres gate removed from `/v1/import` and `/v1/export`
- Basic Oracle smoke test in CI
- Expanded performance harness
- Codex (v0.131.0) running on PROTEUS with writable sandbox
- NFS mount from PROTEUS to ARGONAS configured

## Completed Work
- `db/migrations_oracle/0001_core_schema.sql` (core tables + sidecar parity)
- `mnemos/db/oracle.py` (OracleMemoryRepository with CRUD + sidecars)
- `mnemos/domain/portability/import_.py` and `phases.py` (_repo_for bridge)
- `mnemos/api/routes/portability.py` (removed Postgres-only gate)
- `scripts/oracle_vs_pythia_perf.py` (relative performance harness)
- `.gitlab-ci.yml` (oracle-smoke job)
- `docs/oracle-port-status.md` (living status document)

## Remaining Work / Blockers
1. **P0** — Wire Oracle into `mnemos/core/lifecycle.py` (pool creation + backend selection)
2. **P1** — Complete remaining repository methods (delete, semantic search, visibility, full sidecars)
3. **P1** — Fix migration idempotency (bare ALTER TABLE statements)
4. **P2** — Run full test suite + end-to-end CHARON import/export against Oracle
5. **P3** — Expand CI, performance testing, and production readiness (dedicated benchmark user)

## Key Files Modified
- mnemos/db/oracle.py
- db/migrations_oracle/0001_core_schema.sql
- mnemos/domain/portability/import_.py
- mnemos/domain/portability/phases.py
- mnemos/api/routes/portability.py
- docs/oracle-port-status.md
- scripts/oracle_vs_pythia_perf.py
- .gitlab-ci.yml

## Environment
- PROTEUS (192.168.207.25): Oracle 26ai + Codex v0.131.0 + writable sandbox
- ARGONAS (192.168.207.101): Canonical git + NFS
- Branch: feat/oracle-port (not yet merged to master)

## Next Steps
1. Implement Oracle pool + backend selection in lifecycle.py
2. Finish remaining OracleMemoryRepository methods
3. Run full test suite and CHARON sidecar import
4. Expand CI and performance testing
5. Merge feat/oracle-port to master

**Owner:** jperlow
**Status:** Ready for continuation
