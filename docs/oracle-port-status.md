# Oracle Porting Status & Gaps

**Date:** 2026-05-19
**Status:** M6 partial complete (data import done, basic perf measured)

## What Works
- Oracle 26ai Free container running on PROTEUS (FREEPDB1)
- mnemos user created + connects
- CHARON export from production (.67) succeeded (8157 memories)
- 8157 memories imported into Oracle `memories` table
- Raw Oracle micro-queries fast (COUNT 0.001s, filter 0.014s, scan100 0.008s)
- Production API export100 ~0.107s (higher level)

## Gaps
- Only `memories` table exists in Oracle; full schema (memory_versions, kg_triples, compression_manifest, deletion_log, etc.) missing
- No OracleMemoryRepository / OracleBackend with full MNEMOS semantics (search, RLS, triggers, sidecar handling)
- No running MNEMOS instance backed by Oracle
- Sidecars (kg_triples, versions) not yet imported
- No indexes or performance tuning on Oracle side
- No equivalent of Postgres RLS / namespace isolation
- Performance tests are micro only; no end-to-end workload parity (create+search+export+kg)
- CHARON import currently manual script, not via /v1/import API

## Next Steps
1. ✅ Core Oracle schema created (db/migrations_oracle/0001_core_schema.sql) with Codex help
2. Build OracleBackend + OracleMemoryRepository with parity to Postgres
3. Stand up test MNEMOS instance on PROTEUS using Oracle DSN
4. Wire CHARON /v1/import to Oracle backend
5. Run full test suite against Oracle
6. Expand perf harness (add kg_triples, versions, realistic load)
7. Add Oracle to CI matrix

## Perf Snapshot (2026-05-19)
- PROTEUS Oracle raw: COUNT=0.001s | infra filter=0.014s | scan100=0.008s
- PYTHIA API (full stack): export100=0.107s
- Gap: raw DB vs full API; need MNEMOS-on-Oracle for apples-to-apples

## Perf Harness Environment
`scripts/oracle_vs_pythia_perf.py` reads all credentials from environment variables and fails before making network calls if any are missing:

- `MNEMOS_TOKEN`: bearer token for the PYTHIA MNEMOS API
- `PROTEUS_SSH_PASS`: SSH password consumed by `sshpass -e` through `SSHPASS`
- `ORACLE_PASS`: Oracle password for the `mnemos` database user on PROTEUS
- `PROTEUS_USER` (optional): SSH user (default `root`). **Strongly recommended**: create a dedicated low-privilege `oraclebench` user on PROTEUS with forced command limited to the Oracle query script.
- `PROTEUS_HOST` (optional): override target host (default 192.168.207.25)

The harness uses `ssh -o StrictHostKeyChecking=yes`; STUDIO must already trust the PROTEUS host key before the script is run.

**Codex note (high):** Root SSH for read-only benchmark has host-level blast radius. Replace with dedicated benchmark user before operational use.

**Owner:** jperlow
**Last updated:** 2026-05-19
