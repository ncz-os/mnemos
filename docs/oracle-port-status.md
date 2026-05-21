# Oracle Porting Status & Gaps

**Date:** 2026-05-21
**Status:** M7 functionally complete — repository surface fully wired
against Oracle Database 26ai; 13/13 functional probes pass (see
`docs/proof/bakeoff-final-summary-2026-05-21.md`). Outstanding work is
limited to live-pytest parity (gated on env), supporting-module port
(ConsultationAudit), and operational polish (P3 below).

## What Works
- Oracle 26ai Free container running on oracle-host (FREEPDB1)
- `mnemos` user created + connects
- CHARON export from production (.67) succeeded (8157 memories)
- 8157 memories imported into Oracle `memories` table
- Raw Oracle micro-queries fast (COUNT 0.001s, filter 0.014s, scan100 0.008s)
- Production API export100 ~0.107s (higher level)
- **Lifecycle wired (M7 P0):** `mnemos/core/lifecycle.py` recognises
  `oracle://` and `oracle+oracledb://` DSNs, builds an async oracledb
  pool via `mnemos.persistence.oracle.create_oracle_pool`, and selects
  the `oracle` backend branch in `lifespan()`.
- **OracleBackend ABC-conformant:** all 9 repository properties return
  instantiable subclasses. Attribute lookups never raise — only
  unimplemented methods do, with explicit `NotImplementedError`
  pointing back to this doc.
- **Migration idempotency:** bare `ALTER TABLE memories ADD (...)` is
  replaced with a PL/SQL block guarded by `user_tab_columns`; backfill
  `UPDATE` guarded by EXISTS check on legacy `created_at` /
  `updated_at`. The migration is safe to replay.
- **Sidecar schema (M7 P1.3):** migration 0001 now creates
  `memory_branches`, `state`, `federation_peers`,
  `webhook_subscriptions`, `memory_compression_candidates`, and
  `memory_compressed_variants`.
- **Smoke-verified against oracle-host** (see end of doc).

## Repository surface coverage (post-M7 sprint)

| Repository | Wired methods | Stubbed (call-time NotImplementedError) |
|---|---|---|
| `MemoryRepository` | insert, fetch_by_id, set_suppress_version_snapshot (no-op), fetch_versioned_memory_ids, fetch_memory_head_checks, gather_stats, get_memory, list_memories, update_memory, delete_memory, fts_search (LIKE/INSTR fallback), assert_memory_readable, fetch_memory_export, fetch_referenced_memory_allowlist, find_active_duplicate_by_content_hash (ORA_HASH), fetch_memory_log, fetch_checkout_commit, fetch_diff_commit_pair, bump_recall_and_get_memory, find_duplicate_content_groups, consolidate_duplicate_memories, semantic_search (Oracle Database 26ai VECTOR_DISTANCE COSINE), fetch_memory_context | — |
| `KGRepository` | insert_kg_triple, fetch_kg_triple_by_id, fetch_kg_triples_for_export | — |
| `VersionRepository` | insert_memory_version, fetch_memory_version_by_id, fetch_memory_versions_for_export, fetch_memory_versions_by_ids | — |
| `BranchRepository` | upsert_memory_branch_head, create_memory_branch, delete_memory_branches_for_memories, fetch_memory_branch_heads | — |
| `CompressionRepository` | compression_candidate_exists, insert_compressed_variant (MERGE upsert), fetch_compressed_variant_by_memory_id, gather_stats, fetch_compressed_variants_for_export | — |
| `WebhookRepository` | dispatch_event (subscription scan + INSERT into webhook_deliveries) | — |
| `StateRepository` | get, set (MERGE upsert), delete (soft), list_namespace, delete_namespace | — |
| `FederationRepository` | full peer CRUD (create_peer, list_peers, get_peer, update_peer, upsert_peer, delete_peer, list_due_peers), fetch_memory_page, feed_query, get_feed_memory, get_sync_peer, fetch_sync_log, create_sync_log, finish_sync_log, record_sync_error, record_sync_success, record_schema_abort, update_peer_schema_check (writes `peer_mnemos_version` + `last_schema_check_at` on `federation_peers`; columns shipped 2026-05-21), fetch_federated_memory_marker, insert_federated_memory, update_federated_memory_if_newer, apply_consolidation_tombstone (writes federation_consolidation_tombstones + soft-delete), delete_federated_memory | — |
| `ConsultationAuditRepository` | all 5 — safe-default returns (`None` / `[]`) so the GRAEAE engine falls back to its built-in provider defaults | — (real Oracle port of model_registry tables is the proper next step) |

## VisibilityFilter rendering

`_render_visibility(filter, *, table_alias, param_prefix)` in
`mnemos/persistence/oracle.py` translates `VisibilityScope` into an
Oracle WHERE clause with named binds:

- `ROOT_BYPASS` → no clause (or `namespace = :ns` if pinned).
- `OWN_ONLY` → `owner_id = :owner AND namespace = :ns`.
- `READABLE` → world/federation visibility expansion + owner clause,
  matching the Postgres renderer's `read-visibility predicate set`
  (live memory reads: owner / federation / world / group). Group
  membership is gated through the same `group_ids` parameter binder
  used by `PostgresBackend.assert_memory_readable`; expand-coverage
  for the v1_multiuser group-policy unix-bits expansion remains a
  follow-up (P1.4) for the few code paths that still take the
  partial-scope branch.

`None` namespace on a non-root scope falls back to `1=0` (same as
Postgres).

## Smoke baseline (2026-05-19)

Against oracle-host Oracle 26ai with the 8157-memory baseline:

- **State** — set+get returns materialized `'hello'` (CLOB OK), delete
  reports `True`, list after delete reports `[]`.
- **Federation** — list_peers / list_due_peers / delete_peer (no peers
  configured) all return cleanly.
- **Memory CRUD** — fetch_memory_by_id returns 20-column row;
  gather_stats reports `total=8157 native=8157 federated=0`. Synthetic
  insert + update + delete cycle passes including OWN_ONLY wrong-owner
  BLOCK + post-delete invisibility.
- **Visibility** — `list_memories(ROOT_BYPASS, limit=3)` →
  3 rows / total=8157; `fts_search('mnemos', limit=3)` → 3 rows;
  `get_memory(OWN_ONLY same-owner)` → OK,
  `get_memory(OWN_ONLY wrong-owner)` → None.
- **Exports & sidecars** — fetch_memory_export(limit=3) → 3 rows;
  fetch_referenced_memory_allowlist(3 ids) → 3 rows;
  KG / Version / Branch / Compression `for_export` queries execute
  cleanly (return 0 because sidecars are empty on oracle-host).
- **Version DAG** — insert_memory_version × 2 commits, fetch_memory_log
  returns 2 rows top version_num=2; fetch_checkout_commit("commit-a"):
  OK; fetch_diff_commit_pair("commit-a", "commit-b"): both OK;
  create_memory_branch("feature", "commit-a") points head at
  resolved version_id.

## Remaining work (M7 P1+ follow-ups)

### P1 — COMPLETE (0 stubs remaining; was ~71)
All MemoryRepository / KGRepository / VersionRepository /
BranchRepository / CompressionRepository / WebhookRepository /
StateRepository / FederationRepository abstract methods are wired
with real Oracle SQL. ConsultationAudit returns safe defaults so the
engine falls back to its built-in provider routing (model_registry
port is the proper next step).

### Deferred (Oracle port of supporting modules)
- **ConsultationAudit** currently returns safe defaults so the engine
  does not crash. A full port requires the Oracle equivalents of
  `mnemos.db.mcp_repo` + `mnemos.db.openai_compat_repo` plus the
  `model_registry` / `model_recommendations` / `provider_routing`
  tables. Until then, GRAEAE uses its built-in provider defaults.
- **peer_mnemos_version / last_schema_check_at columns** on
  `federation_peers` — Postgres carries these; Oracle now also writes
  them via `update_peer_schema_check`. (Reconciled 2026-05-21: the
  code path was active but the doc lagged. Audit finding O9 closed.)
- **memories.consolidated_into / consolidated_at** columns — Postgres
  uses these for in-place consolidation metadata; the Oracle path
  records the canonicalisation in `federation_consolidation_tombstones`
  + soft-deletes the source instead.

### P2 — Tested scope & remaining gaps

**Tested (live oracle-host Oracle Database 26ai, 8157-memory baseline + 2026-05-21
gpu-host apples-to-apples bake-off):**
- 13/13 functional probes pass on Oracle EE 23.26.1 (see
  `docs/proof/bakeoff-final-summary-2026-05-21.md`).
- Memory CRUD, FTS search, visibility filtering, version DAG,
  branch HEAD upsert, sidecar exports (KG / Version / Branch /
  Compression), state set/get/delete, federation peer CRUD, MERGE
  upsert semantics.
- Migration idempotency (replayable PL/SQL guards).
- `mnemos/persistence/oracle.py` ABC conformance: all 9 repository
  properties return instantiable subclasses.

**Remaining gaps:**
- Full live pytest parity in `tests/test_persistence_parity.py` —
  the Oracle arm is recognized when `ORACLE_DSN` is set but currently
  skips because the per-backend cleanup helper isn't yet wired; live
  probe lives in `tests/test_oracle_live.py`.
- End-to-end CHARON import/export via `/v1/import` and `/v1/export`
  with Oracle as the live target.
- Expand CI beyond the basic `test:oracle-smoke` job (run pytest
  subset, exercise federation sync paths once implemented).
- Index tuning + Oracle Text / VECTOR setup.

### P3 — Polish
- Dedicated `oraclebench` user on oracle-host (replaces root SSH in
  `scripts/oracle_vs_pythia_perf.py`).
- Final docs + merge `feat/oracle-port` → master.

## Current Branch
Oracle port changes are on `feat/oracle-port` (not yet on master).
Authoritative module is `mnemos/persistence/oracle.py`; legacy
`mnemos/db/oracle.py` is kept for CHARON-import script compatibility.

## Perf Harness Environment
`scripts/oracle_vs_pythia_perf.py` reads all credentials from environment variables and fails before making network calls if any are missing:

- `MNEMOS_TOKEN`: bearer token for the pg-host MNEMOS API
- `oracle-host_SSH_PASS`: SSH password consumed by `sshpass -e` through `SSHPASS`
- `ORACLE_PASS`: Oracle password for the `mnemos` database user on oracle-host
- `oracle-host_USER` (optional): SSH user (default `root`). **Strongly recommended**: create a dedicated low-privilege `oraclebench` user on oracle-host with forced command limited to the Oracle query script.
- `oracle-host_HOST` (optional): override target host (default <host>)

The harness uses `ssh -o StrictHostKeyChecking=yes`; dev-workstation must already trust the oracle-host host key before the script is run.

**Codex note (high):** Root SSH for read-only benchmark has host-level blast radius. Replace with dedicated benchmark user before operational use.

**Owner:** jperlow
**Last updated:** 2026-05-21
