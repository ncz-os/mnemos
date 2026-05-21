# Codex adversarial review — Oracle + Db2 compat stack (2026-05-20)

**Reviewer:** Codex via `codex-companion task --background --effort high`
**Elapsed:** 4m 37s
**Scope:** `mnemos/persistence/{oracle,db2,base}.py`, both migration SQL files, both proof harnesses, 4 EE feature scripts, lifecycle dispatcher, DB2 container recipe (Dockerfile + response.rsp + entrypoint.sh)
**Total findings:** 33

---

## CRITICAL — fixed in this session

| # | File:line | Issue | Fix applied |
|---|---|---|---|
| **C1** | `db2.py:94` | `params.get(':name')` lookup used `:name` with leading colon while dict keys are bare → **always returned None** → every temporal CAST silently folded to `CURRENT TIMESTAMP` even when value was real | **Removed null-cast fold entirely.** Rationale (CLI0109E buffer-exhaustion) was wrong; actual cause was NVL type-unification (now handled by `_NVL_LITERAL_RE`). 3 silent-corruption bugs gone. |
| **C2** | `db2.py:75` | Bare `CAST(:p AS TIMESTAMP)` (no NVL wrapper) folded to `CURRENT TIMESTAMP` whenever value was None → `valid_until=None` (open-ended window) became "expires now" | Same fold removal. Db2 binds NULL through `CAST(? AS TIMESTAMP)` correctly without help. |
| **C3** | `db2.py:96` | `compiled.sub(..., count=1)` replaced **first** match regardless of which bind was identified as null → wrong site clobbered under multi-match | Same fold removal. |

**Verification:** Db2 12/12 still passes (proof `db2-proof-20260520T224924Z.json`); Oracle 13/13 unchanged (`oracle-proof-20260520T224933Z.json`).

---

## HIGH — recommended for v6.0

| # | File:line | Issue | Suggested fix |
|---|---|---|---|
| H1 | `db2.py:112` (_ORA_TO_DB2_PAIRS) | Blind `str.replace` rewrites tokens inside SQL string literals/comments → user content containing `TIMESTAMP WITH TIME ZONE` / `SYSTIMESTAMP` / `TO_VECTOR` gets corrupted at write | SQL-aware tokenizer OR scope rewrites to identifier positions only |
| H2 | `db2.py:126` (_BIND_RE) | `:name` regex scans inside SQL string literals/comments → `'pattern_with_:colon'` literal becomes a bound parameter and raises KeyError | Parse outside quoted regions only |
| H3 | `db2.py:235` (pool acquire) | `_lock` held while opening a physical Db2 connection → serializes slow connects, blocks releases | Reserve capacity under lock, open outside |
| H4 | `db2.py:237` (pool exhaustion) | Raises immediately on full pool instead of waiting → burst-load failure | `asyncio.Condition` / queue wait with timeout |
| H5 | `oracle.py:107` (_is_unique_violation) | Db2 inherits this helper but it only checks `ORA-00001`; Db2 duplicate races become hard failures | Add SQLSTATE `23505` / SQL0803N handling per-dialect |
| H6 | `oracle.py:2324` (insert_federated_memory) | Catches only `ORA-00001` → Db2 duplicate imports raise instead of returning `False` | Route through shared dialect-aware helper |
| H7 | `db/migrations_db2/...sql:49` | Db2 hardcodes `embedding VECTOR(768, FLOAT32)` → deployments using 384/1536/3072-dim embeddings fail at write/search | Generate dim from config OR ship dimension-specific migration files |
| H8 | `db/migrations_db2/...sql:10` | Comment claims idempotent wrap, but file emits raw `CREATE TABLE` → replay skips existing objects, never backfills missing columns | Real versioned migration framework OR guarded ALTER blocks |
| H9 | `db/migrations_db2/...sql:19` | Db2 schema omits Oracle bootstrap tables `users`, `sessions`, `session_messages`, `deletion_log`, `compression_manifest` → non-persistence-backend routes break on Db2 | Add tables OR mark Db2 unsupported for those APIs |
| H10 | `oracle_ee_hnsw_bench.py:36` | EE proof HMAC key hardcoded in source → artifacts forgeable by anyone with repo access | Read signing key from env/secret store; fail closed |
| H11 | `oracle_ee_tde_proof.py:100` | Readiness probe records pass even if restart loop never observed SQL*Plus ready → false-positive proof | Track loop result, fail probe on timeout |
| H12 | `oracle_ee_tde_proof.py:81` | Container restart return code ignored before destructive TDE steps continue | Check `subprocess.run(...).returncode`, abort on failure |
| H13 | `oracle_ee_tde_proof.py:29` | TDE proof hardcodes host, SSH user, container name, SYS password, wallet password | CLI/env input + redact from artifacts |

---

## MEDIUM — v6.1 candidate

| # | File:line | Issue | Suggested fix |
|---|---|---|---|
| M1 | `db2.py:53` (TO_VECTOR substr) | Raw substring replace will rewrite future identifiers like `TO_VECTOR_DISTANCE` | Word-boundary/function-call regex |
| M2 | `db2.py:127` (extra dict keys) | Extra param dict keys silently dropped → stale call sites hidden | `set(params) - set(names)` validation gate |
| M3 | `db2.py:93` (regex per-call) | Null-cast regexes compiled every execute despite being hot path | Precompile (moot since fold is removed; applies if regex resurrected) |
| M4 | `db2.py:133` (vec dim infer) | First positional string starting with `[` wins → earlier JSON/list bind hijacks dimension | Track the `TO_VECTOR` bind name specifically |
| M5 | `db2.py:174` (to_thread granularity) | Db2 pays threadpool hop on execute/fetch/close/commit/rollback per repo call | Batch sync cursor work in one `to_thread` OR move to real async driver |
| M6 | `db2.py:215` (`_min_size` unused) | Configured warm pool size is fiction | Pre-open or remove |
| M7 | `db2.py:225` (DSN escape) | UID/PWD with semicolons could inject CLI attributes | Use driver structured connect OR escape DSN attrs |
| M8 | `db2.py:380` (Db2Backend.__init__) | Reimplements OracleBackend.__init__ → new repo property additions can be missed silently | `super().__init__()` + rebind only Db2-specific repos |
| M9 | `oracle.py:1033` (update_memory) | Silently ignores unknown fields while `update_peer` raises → inconsistent error handling | Raise `ValueError` for unsupported memory fields |
| M10 | `db/migrations_db2/...sql:93` | Db2 drops most Oracle secondary indexes on `memory_versions`, webhook, compression, federation | Port access-path indexes OR document measured replacements |
| M11 | `db/migrations_db2/...sql:96` | Db2 `kg_triples` omits Oracle's `extracted_by_run_id` → schema drift for KG provenance | Add column OR remove from Oracle if obsolete |
| M12 | `scripts/db2_apply_migration.py:34` | Treating SQLSTATE `42704` (undefined object) as benign masks real broken dependencies | Only ignore duplicate-object states for create-replay |
| M13 | `oracle_ee_hnsw_bench.py:49` | Bench deletes prior rows at start but leaves new rows + vector index after → contaminates later proofs | Add explicit cleanup or `--keep-data` flag |
| M14 | `oracle_ee_duality_proof.py:47` (and similar) | HMAC + artifact + connect + probe plumbing duplicated across 4 EE scripts | Extract shared `mnemos/proof/__init__.py` |
| M15 | `docker/db2-eap/Dockerfile:70` | Healthcheck hardcodes `db2inst1` despite `DB2INSTANCE` being configurable | Use `${DB2INSTANCE}` in healthcheck |

---

## LOW — nits

| # | File:line | Issue |
|---|---|---|
| L1 | `db2.py:298` (_Db2OraCompatMixin) | Constants documented but unused (no overrides currently consume them) — can delete since cursor-level translation makes them moot |
| L2 | `oracle.py:9` | Module docs claim parts are stubs/pending but many paths are implemented |
| L3 | `Dockerfile:45` | `ln -sf /bin/bash /bin/sh` global mutation has surprising side effects |
| L4 | `response.rsp:2` | Install path hardcoded to `/opt/ibm/db2/V12.1` — June 6 GA may relocate to V12.2 |

---

## Summary

**Critical bugs found + fixed:** 3 (all in `_remove_none_cast_binds`). Removed via single-block deletion.

**Test impact:** Oracle 13/13 + Db2 12/12 unchanged → fold was actively introducing risk while contributing zero correctness.

**v6.0 ship-blockers remaining:** 13 HIGH items. Of those:
- 6 are translation-layer correctness (H1-H6) → ~1 day SQL-tokenizer refactor
- 3 are schema parity (H7-H9) → ~1 day migration rewrite
- 4 are EE proof + secret hygiene (H10-H13) → ~half-day

**v6.1 backlog:** 15 MEDIUM + 4 LOW already in `docs/v6.1-roadmap.md`. Will fold these in.

**Architectural wins flagged:**
- Cursor-level translation pattern (DeepSeek's design) confirmed sound under adversarial scrutiny — Codex didn't propose tearing it down, only making the regexes SQL-literal-aware
- `_Db2OraCompatMixin` can be deleted (cursor translation made it moot)
- Codex correctly noted my session's NVL widening fix (H1 hypothesis was wrong on cause; H1 fix correct)

---

## Cross-references

- Original handoff before review: `docs/db2-translation-handoff-2026-05-20.md`
- v6.1 roadmap: `docs/v6.1-roadmap.md` (will integrate M1-M15 + L1-L4)
- Latest signed proofs:
  - `docs/proof/oracle-proof-20260520T224933Z.json` (13/13)
  - `docs/proof/db2-proof-20260520T224924Z.json` (12/12, post-fold-removal)

---

*Codex review fed back into the stack 2026-05-20. 3 silent-corruption bugs neutralized. Remaining 30 findings triaged for v6.0 / v6.1 backlog.*
