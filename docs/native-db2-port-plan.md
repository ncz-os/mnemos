# Native Db2 Dialect Port — Requirements & Design

**Branch**: `feat/oracle-port` | **Author**: planning pass, 2026-05-21 | **Target**: MNEMOS v6.x
**Status**: requirements / design — no code yet. One method (`Db2MemoryRepository.semantic_search`) already ported in commit `34c5618` as the reference shape.

---

## 1. Why port to a native Db2 dialect

- **Optimizer visibility.** Today every query travels through `_Db2AsyncCursor.execute` → `_adapt_oracle_to_db2` → regex/replace rewrites (`SYSTIMESTAMP`→`CURRENT TIMESTAMP`, `:name`→`?`, `TO_VECTOR(?)`→`VECTOR(?, dim, FLOAT32)`). The Db2 optimizer sees the rewritten output, never the canonical Db2 form — plan-cache + statement-text matching are degraded.
- **Parse-time overhead.** Every call hits `_translate_sql_cached` (LRU 512) + per-call positional-bind + `dim` scan over params. Even with the LRU cache, the first hit on each unique SQL pays full mask/restore/regex cost. Bench-v4 measurements include this overhead.
- **Removes Oracle Compatibility Mode as a hard runtime dependency.** Today the Db2 container ships with `DB2_COMPATIBILITY_VECTOR=ORA` + `UPDATE DB CFG USING ORA_COMPATIBILITY ON`. IBM ships ORA-compat as a migration aid — relying on it as a steady-state production posture is awkward for IBM-credibility positioning (we tell the IBM-eng review crowd "we ship native Db2," then they `db2set -all` and see ORA-compat on).
- **DiskANN engagement.** The `semantic_search` override already proves native syntax (`VECTOR_DISTANCE(..., EUCLIDEAN)` + `FETCH APPROX FIRST K ROWS ONLY`) is the only path that engages the Db2 12.1.5 DiskANN index. Inherited COSINE + `FETCH FIRST` falls back to exact scan. We want the rest of the surface to express the same level of intent.
- **z/OS reach (partial).** Native LUW SQL is a step closer to Db2 z/OS DRDA compatibility than Oracle-compat output — see §6 for the honest scope.

---

## 2. What changes — file inventory

### 2.1 `mnemos/persistence/db2.py` (899 lines today)

Every `Db2*Repository` class currently relies on inheritance + the cursor translation layer. The port replaces inheritance with **explicit native overrides**, method by method.

#### `Db2MemoryRepository` (today subclasses `OracleMemoryRepository`)

Methods on `OracleMemoryRepository` that need native overrides:

| Method (oracle.py line) | Oracle pattern | Db2 native pattern | Risk |
|---|---|---|---|
| `insert_memory` (971) | `NVL(CAST(:created AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP)` + `:name` binds | `COALESCE(CAST(? AS TIMESTAMP), CURRENT TIMESTAMP)` + positional `?` | Low — types unchanged under ORA-compat |
| `fetch_memory_by_id` (1043) | `:name` binds, plain SELECT | `?` positional, plain SELECT | Low |
| `set_suppress_version_snapshot` (1064) | Session var | Db2 session var equivalent | Low — review trigger semantics |
| `fetch_versioned_memory_ids` (1070) | IN-list expansion via `:name` binds | IN-list with `?` positional | Low |
| `fetch_memory_head_checks` (1092) | Same | Same | Low |
| `gather_stats` (1120) | Aggregate query | Identical | Low |
| `get_memory` (1165) | Visibility clause includes `MOD(NVL(...permission_mode, 0), 10)` + `TRUNC(NVL(.../10), 10)` | `MOD(COALESCE(...,0), 10)`; `TRUNC` works in Db2 ORA-compat — port to `INT(... / 10)` or keep `TRUNC` (Db2 has it natively as `TRUNC(numeric)`) | Low — `TRUNC` is native Db2 |
| `list_memories` (1198) | `OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY` | Same — ISO SQL, already Db2 native | None |
| `update_memory` (1273) | `sets.append("updated = SYSTIMESTAMP")` | `sets.append("updated = CURRENT TIMESTAMP")` | Low |
| `delete_memory` (1315) | `SET deleted_at = SYSTIMESTAMP` | `SET deleted_at = CURRENT TIMESTAMP` | Low |
| `fts_search` (1350) | `FETCH FIRST :limit ROWS ONLY` + text predicates | Same — verify Oracle Text vs Db2 Text Search syntax (`CONTAINS` differs) | **HIGH** — Db2 uses `XMLSEARCH`/`CONTAINS` differently; may require LIKE fallback or Db2 Text Search install |
| `assert_memory_readable` (1400) | Visibility-only | Same | Low |
| `fetch_memory_export` (1420) | `OFFSET ... FETCH NEXT` | Same | None |
| `fetch_referenced_memory_allowlist` (1458) | IN expansion | Same | Low |
| `find_active_duplicate_by_content_hash` (1485) | `FETCH FIRST 1 ROWS ONLY` | Same | None |
| `fetch_memory_log` (1517) | `FETCH FIRST :limit ROWS ONLY` | Same | None |
| `fetch_diff_commit_pair` (1554) | Standard SELECT | Same | None |
| `fetch_checkout_commit` (1579) | Standard SELECT | Same | None |
| `bump_recall_and_get_memory` (1606) | `recall_count = NVL(recall_count,0)+1, last_recalled_at = SYSTIMESTAMP` | `COALESCE(recall_count,0)+1, CURRENT TIMESTAMP` | Low |
| `find_duplicate_content_groups` (1637) | Group-by aggregate | Same | None |
| `consolidate_duplicate_memories` (1663) | `UPDATE … SET deleted_at = SYSTIMESTAMP` | `CURRENT TIMESTAMP` | Low |
| `semantic_search` (1692) | **ALREADY OVERRIDDEN** in db2.py:587 — EUCLIDEAN + `FETCH APPROX FIRST` | — | Done |
| `fetch_memory_context` (1763) | Standard SELECT | Same | None |

#### `Db2KGRepository`

Methods on `OracleKGRepository`:

| Method (line) | Oracle pattern | Db2 native | Risk |
|---|---|---|---|
| `insert_kg_triple` (489) | `NVL(CAST(:valid_from AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP)`, `NVL(CAST(:confidence AS NUMBER), 1.0)`, `NVL(CAST(:created AS DATE), SYSDATE)` | `COALESCE(CAST(? AS TIMESTAMP), CURRENT TIMESTAMP)`, `COALESCE(CAST(? AS DECFLOAT), 1.0)` (NUMBER→DECFLOAT or DOUBLE), `COALESCE(CAST(? AS DATE), CURRENT DATE)` | Med — verify `NUMBER`→`DECFLOAT` vs `DOUBLE` for confidence values |
| `fetch_kg_triple_by_id` (555) | Plain SELECT | Same | None |
| `fetch_kg_triples_for_export` (576) | `OFFSET … FETCH NEXT` | Same | None |

#### `Db2VersionRepository`

| Method (line) | Pattern | Native | Risk |
|---|---|---|---|
| `insert_memory_version` (623) | `NVL(:namespace, 'default')`, `NVL(CAST(:permission_mode AS NUMBER), 600)`, `NVL(CAST(:snapshot_at AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP)`, `NVL(:change_type, 'create')`, `NVL(:branch, 'main')` | `COALESCE(...)` throughout, `NUMBER`→`INTEGER`, `TIMESTAMP WITH TIME ZONE`→`TIMESTAMP` | Low |
| `fetch_memory_version_by_id` (714) | Plain SELECT | Same | None |
| `fetch_memory_versions_for_export` (743) | `OFFSET … FETCH NEXT` | Same | None |
| `fetch_memory_versions_by_ids` (780) | IN expansion | Same | Low |

#### `Db2BranchRepository`

| Method (line) | Pattern | Native | Risk |
|---|---|---|---|
| `upsert_memory_branch_head` (800) | `MERGE INTO memory_branches m USING (…) src ON (…) WHEN MATCHED THEN UPDATE SET … WHEN NOT MATCHED THEN INSERT (…)` | Db2 supports MERGE natively. Syntax is essentially identical; verify `USING (SELECT ? id, ? branch FROM SYSIBM.SYSDUMMY1) src` instead of `… FROM DUAL` | Med — DUAL→SYSDUMMY1 already auto-rewritten today but should be explicit |
| `fetch_memory_branch_heads` (833) | SELECT | Same | None |
| `delete_memory_branches_for_memories` (869) | IN expansion | Same | Low |
| `create_memory_branch` (885) | `FETCH FIRST 1 ROWS ONLY`, ROW_NUMBER OVER | Same | None |

#### `Db2CompressionRepository`

| Method (line) | Pattern | Native | Risk |
|---|---|---|---|
| `compression_candidate_exists` (1806) | SELECT | Same | None |
| `insert_compressed_variant` (1836) | NVL + SYSTIMESTAMP | COALESCE + CURRENT TIMESTAMP | Low |
| `fetch_compressed_variant_by_memory_id` (1903) | SELECT | Same | None |
| `gather_stats` (1924) | Aggregate | Same | None |
| `fetch_compressed_variants_for_export` (1948) | `OFFSET … FETCH NEXT` | Same | None |

#### `Db2WebhookRepository`

| Method (line) | Pattern | Native | Risk |
|---|---|---|---|
| `dispatch_event` (1983) | `SYSTIMESTAMP`, namespace defaulting | `CURRENT TIMESTAMP`, COALESCE | Low |

#### `Db2ConsultationAuditRepository`

| Method (line) | Pattern | Native | Risk |
|---|---|---|---|
| `fetch_recommended_model` (2061) | `COALESCE(:selected_at, SYSTIMESTAMP)` | `COALESCE(?, CURRENT TIMESTAMP)` | Low |
| `fetch_model_recommendation` (2071) | SELECT | Same | None |
| `lookup_provider_for_model` (2081) | SELECT | Same | None |
| `fetch_available_models` (2085) | SELECT | Same | None |
| `fetch_model_provider` (2089) | SELECT | Same | None |

#### `Db2FederationRepository`

This is the heaviest port — 17 methods, several MERGE INTOs, lots of `TIMESTAMP WITH TIME ZONE` casts:

| Method (line) | Pattern | Native | Risk |
|---|---|---|---|
| `list_peers`, `get_peer`, `delete_peer` (2097, 2109, 2122) | SELECT/DELETE | Same | None |
| `list_due_peers` (2135) | `SYSTIMESTAMP - NUMTODSINTERVAL(sync_interval_secs, 'SECOND')` | `CURRENT TIMESTAMP - sync_interval_secs SECONDS` (Db2 native interval arithmetic) | **MED-HIGH** — interval syntax is different. Validate against Db2 12.1.x docs; alternative `CURRENT TIMESTAMP - (sync_interval_secs * 1 SECOND)` |
| `fetch_memory_page` (2168) | `FETCH FIRST :limit ROWS ONLY` | Same | None |
| `create_peer` (2203) | Insert with namespace defaults, SYSTIMESTAMP | COALESCE + CURRENT TIMESTAMP | Low |
| `update_peer` (2250) | `sets.append("updated = SYSTIMESTAMP")` | `CURRENT TIMESTAMP` | Low |
| `upsert_peer` (2284) | `MERGE INTO federation_peers p USING (…) ON … WHEN MATCHED … WHEN NOT MATCHED INSERT …` | Same shape, `USING (… FROM SYSIBM.SYSDUMMY1)` | Med |
| `get_sync_peer`, `fetch_sync_log` (2323, 2336) | SELECT | Same | None |
| `create_sync_log`, `finish_sync_log`, `record_sync_error`, `record_sync_success`, `record_schema_abort`, `update_peer_schema_check` (2357–2495) | SYSTIMESTAMP throughout | CURRENT TIMESTAMP | Low |
| `fetch_federated_memory_marker` (2503) | SELECT | Same | None |
| `insert_federated_memory` (2520) | `CAST(:remote_updated AS TIMESTAMP WITH TIME ZONE)`, SYSTIMESTAMP | `CAST(? AS TIMESTAMP)`, CURRENT TIMESTAMP | Low |
| `update_federated_memory_if_newer` (2588) | Same casts | Same | Low |
| `apply_consolidation_tombstone` (2642) | MERGE INTO + NVL + SYSTIMESTAMP | MERGE + COALESCE + CURRENT TIMESTAMP | Med |
| `delete_federated_memory` (2710) | `SET deleted_at = SYSTIMESTAMP` | CURRENT TIMESTAMP | Low |
| `feed_query`, `get_feed_memory` (2729, 2777) | `FETCH FIRST :limit ROWS ONLY` | Same | None |

#### `Db2StateRepository`

| Method (line) | Pattern | Native | Risk |
|---|---|---|---|
| `get` (2819) | `SELECT key, value, TO_CHAR(updated) AS updated, …` | `TO_CHAR(updated)` is Db2 native — works. Alternatively `VARCHAR_FORMAT(updated, 'YYYY-MM-DD HH24:MI:SS.FF6')` for explicit format | Low |
| `set` (2846) | `MERGE INTO state s USING (… FROM DUAL) src ON (s.key=src.key) WHEN MATCHED THEN UPDATE SET value=:value, updated=SYSTIMESTAMP WHEN NOT MATCHED THEN INSERT (…, SYSTIMESTAMP, 1)` | Same shape, `FROM SYSIBM.SYSDUMMY1` + `CURRENT TIMESTAMP` | Med |
| `delete` (2891) | `SET deleted_at = SYSTIMESTAMP` | CURRENT TIMESTAMP | Low |
| `list_namespace` (2918) | `OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY` + TO_CHAR | Same | Low |
| `delete_namespace` (2967) | `SET deleted_at = SYSTIMESTAMP` | CURRENT TIMESTAMP | Low |

### 2.2 `_Db2AsyncCursor.execute` (db2.py:238–249) + helpers

Once every repository method emits native Db2 SQL with positional `?` binds, `_adapt_oracle_to_db2` becomes dead code for the production path. **However, keep the translation layer as a safety net through Phase 2** — for two reasons:

1. **Inherited methods we haven't ported yet** during the transition. If we port repo-by-repo, the unported repos still emit Oracle SQL.
2. **Backwards-compat fallback.** External consumers (the proof harness, bench scripts) may construct queries that go through the same cursor.

**Phase 3 cleanup** removes:
- `_ORA_TO_DB2_PAIRS`, `_TO_VECTOR_RE`, `_BIND_RE`, `_VECTOR_CALL_RE`, `_NVL_LITERAL_RE`
- `_mask_sql_literals_and_comments`, `_unmask_sql`
- `_translate_sql_cached`, `_adapt_oracle_to_db2`
- The translation call inside `_Db2AsyncCursor.execute` — replaced with a pass-through that still handles dict→positional conversion (the one Oracle-style affordance worth keeping is `:name` named binds with dict params, but only if we choose to keep that convention in native Db2 code; recommendation: drop it, write pure positional `?` in the native SQL).

The cursor stays alive as the `asyncio.to_thread` bridge over `ibm_db_dbi` — that part is independent of dialect.

### 2.3 `db/migrations_db2/0001_core_schema.sql` (302 lines)

Currently uses Oracle-compat-friendly DDL that happens to be accepted by Db2 under ORA-compat. Native rewrite:

| Current | Native Db2 | Notes |
|---|---|---|
| `VARCHAR2(100)` | `VARCHAR(100)` | identical semantics for VARCHAR(n ≤ 32672); Db2 doesn't have a separate `VARCHAR2` outside ORA-compat |
| `VARCHAR2(200)` | `VARCHAR(200)` | same |
| `VARCHAR2(64)` | `VARCHAR(64)` | same |
| `CLOB` | `CLOB(2G)` or just `CLOB` | Db2 default CLOB size is 1MB unless declared; recommend `CLOB(1M)` for content/metadata, `CLOB(64K)` for shorter fields, or explicitly `CLOB(2G) INLINE LENGTH 4000` for content if we want inline storage of short content |
| `NUMBER` | `BIGINT` for counters; `INTEGER` for small ints; `NUMERIC(p,s)` if a specific precision is needed | `NUMBER` without precision in Db2 ORA-compat maps to DECFLOAT(34) — wasteful for counters |
| `NUMBER(4)` (permission_mode) | `SMALLINT` or `INTEGER` | 4-digit values fit SMALLINT |
| `recall_count NUMBER DEFAULT 0` | `recall_count BIGINT DEFAULT 0` | counter semantics |
| `TIMESTAMP DEFAULT CURRENT TIMESTAMP` | `TIMESTAMP DEFAULT CURRENT TIMESTAMP` | already native — no change |
| `TIMESTAMP WITH TIME ZONE` (not present in 0001 — used in code SQL only) | `TIMESTAMP` | Db2 12.1 supports TIMESTAMP WITH TIME ZONE; decide whether to adopt it or keep naive TIMESTAMP across the board. Recommend keep naive UTC (matches Oracle code that's already collapsing to TIMESTAMP under ORA-compat) |
| `VECTOR({{embedding_dim}}, FLOAT32)` | identical | Db2 native type since 12.1.2 |
| `CREATE VECTOR INDEX … WITH DISTANCE EUCLIDEAN` | identical | already native Db2 12.1.5 syntax |
| `@` statement terminator | keep — Db2 CLP convention | unchanged |

Additional schema considerations:

- **IDENTITY columns**. The schema today uses application-generated string IDs everywhere (UUIDv7-style). No sequences. No change needed.
- **PRIMARY KEY clause**. `id VARCHAR2(100) NOT NULL PRIMARY KEY` → `id VARCHAR(100) NOT NULL PRIMARY KEY` — fine in Db2 native.
- **`REFERENCES … ON DELETE CASCADE`**. Native Db2. No change.
- **Index DDL**. `CREATE INDEX … ON …(col1, col2)` — native Db2. No change.

### 2.4 `docker/db2-eap/entrypoint.sh`

Today the entrypoint defaults `ENABLE_ORACLE_COMPATIBILITY=true`, runs `db2set DB2_COMPATIBILITY_VECTOR=ORA` and `UPDATE DB CFG USING ORA_COMPATIBILITY ON`. Plan:

- **Phase 1 (during port)**: keep ORA-compat ON. Migration accepts both dialects.
- **Phase 2 (port validation)**: parity tests run against an ORA-compat-OFF container variant (`ENABLE_ORACLE_COMPATIBILITY=false`). Test matrix needs both.
- **Phase 3 (post-cleanup)**: default `ENABLE_ORACLE_COMPATIBILITY=false`. Keep the toggle for emergency fallback / users running an older Db2 without 12.1.x VECTOR. Document in `docs/db2-eap-recipe-2026-05-20.md`.

### 2.5 Other touched files

| File | Change |
|---|---|
| `scripts/db2_proof_run.py` | Re-run after each Phase 2 milestone; should pass against both modes |
| `scripts/db2_apply_migration.py` | If schema DDL changes (Phase 2), update; pin migration version |
| `scripts/bench_three_backends.py`, `scripts/bench_v4.py`, future `bench_v5.py` | Bench-v5 measures native vs compat A/B |
| `mnemos/core/lifecycle.py:402-403` | Backend factory — add optional `Db2BackendNative` selector via env (`MNEMOS_DB2_DIALECT=native|compat`); defaults to `compat` until Phase 3 |
| `tests/test_db2_*` | Existing tests stay, new `test_db2_dialect_parity.py` added |
| `docs/v6.1-roadmap.md`, `docs/oracle-port-status.md` | Cross-link this plan |

---

## 3. Type mapping table

| Oracle type | Db2 native | Range / behavior delta | Notes |
|---|---|---|---|
| `VARCHAR2(n)` | `VARCHAR(n)` | Identical for n ≤ 32672. Both reject NULL by default unless declared. | NULL semantics identical: empty string `''` is **not** NULL in Db2 native (Oracle treats `''` as NULL — this is the only behavior delta to audit) |
| `VARCHAR2(4000)` | `VARCHAR(4000)` | same | |
| `CLOB` | `CLOB(1M)` default; `CLOB(2G)` max | Db2 needs an explicit size or defaults to 1MB | Recommend `CLOB(1M)` for content, `CLOB(64K)` for metadata. Inline if small. |
| `NUMBER` (no precision) | `DECFLOAT(34)` under ORA-compat; recommended native: `BIGINT` for counters, `DOUBLE` for floats, `NUMERIC(p,s)` for fixed-precision | DECFLOAT is 17 bytes wide vs BIGINT 8 bytes — both ergonomics + storage delta | Choose deliberately per column |
| `NUMBER(p)` | `INTEGER` (p ≤ 9), `BIGINT` (p ≤ 18), `NUMERIC(p)` (any) | Range explicit | |
| `NUMBER(p,s)` | `NUMERIC(p,s)` | Identical decimal semantics | |
| `TIMESTAMP` | `TIMESTAMP` | Both 6 fractional digits by default; both can take `(n)` for precision | Identical |
| `TIMESTAMP WITH TIME ZONE` | `TIMESTAMP WITH TIME ZONE` (Db2 12+) or `TIMESTAMP` | Db2 supports TZ but we collapse to UTC TIMESTAMP today | Decision: continue collapsing — simpler and matches the existing Oracle path under ORA-compat |
| `DATE` | `DATE` | Db2 DATE is date-only (no time) — Oracle DATE includes a time component | **WATCH**: Oracle `DATE` columns that need time precision must port to `TIMESTAMP` |
| `BLOB` (not used in schema) | `BLOB(n)` | Same as CLOB on sizing | n/a today |
| `VECTOR(d, FLOAT32)` | `VECTOR(d, FLOAT32)` | Identical | already native |
| `RAW(n)` | `VARBINARY(n)` | Identical | n/a today |

NULL-string gotcha caveat: MNEMOS application code already treats empty strings and NULLs separately (no Oracle-style `'' IS NULL` reliance in repos), so this is a documentation-only concern.

---

## 4. SQL idiom mapping table

| Oracle pattern | Native Db2 pattern | Notes |
|---|---|---|
| `FROM DUAL` | `FROM SYSIBM.SYSDUMMY1` | Db2 ORA-compat aliases DUAL; native code uses SYSDUMMY1. Used in `OracleBackend.open()` smoke (`SELECT 1 FROM DUAL`) and inside MERGE USING clauses |
| `SYSTIMESTAMP` | `CURRENT TIMESTAMP` | Already in `_ORA_TO_DB2_PAIRS`; port emits directly |
| `SYSDATE` | `CURRENT DATE` | Same |
| `NVL(a, b)` | `COALESCE(a, b)` | COALESCE is ISO SQL; Db2 also accepts `NVL` under ORA-compat. Native uses COALESCE |
| `DECODE(x, a, 1, b, 2, 0)` | `CASE x WHEN a THEN 1 WHEN b THEN 2 ELSE 0 END` | Not currently used in MNEMOS oracle.py — no action needed; flag for future |
| `SUBSTR`, `LENGTH`, `TRIM`, `UPPER`, `LOWER` | Identical | Both native |
| `MOD(x, y)` | `MOD(x, y)` | Native in both |
| `TRUNC(numeric)` | `TRUNC(numeric)` or `TRUNCATE(x, 0)` | Db2 native; used in visibility clause at oracle.py:172 |
| `TO_CHAR(ts)` | `TO_CHAR(ts)` or `VARCHAR_FORMAT(ts, 'YYYY-MM-DD HH24:MI:SS.FF6')` | TO_CHAR works in Db2 ORA-compat; recommend VARCHAR_FORMAT for explicit native |
| `TO_DATE`, `TO_TIMESTAMP` | `TO_DATE`, `TO_TIMESTAMP` (Db2 ORA-compat) or `DATE(string)`, `TIMESTAMP(string)` | n/a today |
| `NUMTODSINTERVAL(n, 'SECOND')` | `n SECONDS` (interval literal) | Used in `list_due_peers` (oracle.py:2145). Native: `CURRENT TIMESTAMP - sync_interval_secs SECONDS`. Verify against ibm_db_dbi parameterized intervals — may need `CURRENT TIMESTAMP - (CAST(? AS INTEGER) * 1 SECOND)` |
| `ROWNUM` | row_number() OVER, `FETCH FIRST n ROWS ONLY`, `OFFSET … FETCH NEXT …` | Not used in oracle.py |
| `:name` named binds | `?` positional binds (ibm_db_dbi standard) | Today bridged in cursor; native ports drop `:name` and write `?` directly, removing dict→positional reorder |
| `RETURNING col INTO :var` (Oracle DML returning) | `SELECT col FROM FINAL TABLE (INSERT … VALUES …)` or `SELECT col FROM NEW TABLE (UPDATE …)` | **Not used in MNEMOS** — no RETURNING clauses in oracle.py — no action needed |
| `MERGE INTO t USING (… FROM DUAL) src ON … WHEN MATCHED … WHEN NOT MATCHED …` | `MERGE INTO t USING (… FROM SYSIBM.SYSDUMMY1) src ON … WHEN MATCHED THEN UPDATE SET … WHEN NOT MATCHED THEN INSERT …` | 4 MERGE sites: `OracleBranchRepository.upsert_memory_branch_head` (814), `OracleFederationRepository.upsert_peer` (2299), `OracleFederationRepository.apply_consolidation_tombstone` (2663), `OracleStateRepository.set` (2863). Db2 MERGE has minor differences: optional `ELSE IGNORE`, no `RETURNING`, deterministic-target rule slightly stricter. All four current shapes work natively. |
| `LIMIT n` | `FETCH FIRST n ROWS ONLY` | Oracle code already uses ISO `FETCH FIRST` everywhere — none of this changes |
| `OFFSET n ROWS FETCH NEXT m ROWS ONLY` | identical | ISO SQL, Db2 native |
| `TO_VECTOR(:q)` | `VECTOR(?, dim, FLOAT32)` | Db2 12.1.x native vector literal constructor. Today cursor regex-rewrites; native code emits directly. The dim must be embedded into the SQL string (it's not a bindable parameter on Db2). |
| `VECTOR_DISTANCE(a, b, COSINE)` / `EUCLIDEAN` | identical | Both native |
| `FETCH APPROX FIRST n ROWS ONLY` | Db2-only — required to engage DiskANN | Already in semantic_search override |
| `DEFAULT NEXT VALUE FOR seq` | `DEFAULT NEXT VALUE FOR seq` (Db2 native) OR `GENERATED ALWAYS AS IDENTITY` | MNEMOS uses app-generated IDs — no sequences |

---

## 5. Test strategy

The risk is that a native port silently changes one query's semantics — wrong row, wrong order, wrong NULL handling — and the test suite passes because it covered the row shape but not the exact predicate. Strategy:

**Step 1 — Coexistence period (Phase 2).** Both `Db2BackendCompat` (today's class, renamed) and `Db2BackendNative` (new) live side-by-side. The `Db2Backend` symbol becomes an alias selected by env: `MNEMOS_DB2_DIALECT=compat` (default) or `=native`. Lifecycle (`mnemos/core/lifecycle.py:402`) reads the env and instantiates accordingly.

**Step 2 — Parity tests.** New file `tests/test_db2_dialect_parity.py`. For every repository method, set up the same fixture under both backends pointing at separate databases (e.g. `MNEMOS_COMPAT` and `MNEMOS_NATIVE` schemas in the same Db2 instance, or two containers). Assert row-for-row equality. Covers:

- `insert_memory` → `fetch_memory_by_id` round-trip equality
- `semantic_search` returns same top-K IDs in same order (this is the one already proven)
- `list_memories` with pagination — same row IDs in same order, same OFFSET/FETCH
- `upsert_memory_branch_head` followed by re-read — identical state
- `OracleStateRepository.set` then `get` — identical value + timestamp shape
- Federation tombstones — `apply_consolidation_tombstone` semantics
- KG triple insert + fetch — same value + casts

**Step 3 — bench-v5.** New `scripts/bench_v5.py` measures latency delta native-vs-compat on the same DiskANN index, same dataset, same query mix. Targets:
- `semantic_search` (already native; expect zero delta)
- `list_memories` (compat does dict→positional rewrite — exact delta TBD (measured against Db2 12.1.5 GA, not EAP))
- `insert_memory` (compat does full mask/unmask — exact delta TBD (measured against Db2 12.1.5 GA, not EAP))
- MERGE operations (`state.set`, `upsert_peer`) — exact delta TBD (measured against Db2 12.1.5 GA, not EAP)

**Step 4 — Promotion + deprecation.** Once parity + bench prove native is correct and at least as fast, flip lifecycle default to `native`. Emit `DeprecationWarning` on the compat path with a one-release window. Phase 3 removes the compat backend.

---

## 6. z/OS portability scope — honest answer

**DRDA is the network protocol.** A native LUW dialect does **not** automatically work on z/OS — DRDA tells the client how to talk to a remote Db2, not what SQL surface that Db2 accepts. z/OS Db2 has its own dialect quirks:

- z/OS doesn't support `MERGE INTO … USING (… FROM SYSIBM.SYSDUMMY1)` the same way LUW does — there are restrictions on the source table expression.
- z/OS `VECTOR` data type is **not yet GA** as of 2026-05-21 (LUW 12.1.x has it; z/OS is on a different release train).
- z/OS reserved-word and identifier limits differ slightly.
- z/OS doesn't have the LUW `SYSIBMADM.REG_VARIABLES` view we probe at startup.
- z/OS catalog views live under `SYSIBM` directly, not `SYSIBMADM`.

**What native LUW SQL helps with**: closer-to-portable code for **Db2 Warehouse** and **Db2 Cloud (DPF mode)** — both are LUW-derived. We get incremental z/OS compatibility (CURRENT TIMESTAMP, COALESCE, FETCH FIRST, MERGE shape) but the VECTOR layer + the startup probe are LUW-only and would need a separate adapter for z/OS.

**Realistic z/OS posture**: out of scope for the v6.x dialect port. A z/OS adapter would be a v7+ project requiring (a) z/OS Db2 with native VECTOR, (b) z/OS-specific catalog probes, (c) ANN index strategy review. We document that the native dialect port is a **prerequisite** for any future z/OS work, not a delivery of it.

---

## 7. Sequencing + effort estimate

**Phase 1 — Table-stakes native overrides (≈ 5 working days)**
- `Db2StateRepository` (5 methods, includes MERGE — high-touch but small)
- `Db2BranchRepository` (4 methods, includes MERGE)
- `Db2KGRepository` (3 methods)
- `Db2VersionRepository` (4 methods)
- `Db2CompressionRepository` (5 methods)
- `Db2WebhookRepository` (1 method)
- `Db2ConsultationAuditRepository` (5 methods, mostly SELECT)
- Each method ships with a focused unit test pinning the emitted SQL string + a live integration check against the EAP container.

**Phase 2 — Heavy lifters + migration rewrite + bench-v5 (≈ 7 working days)**
- `Db2MemoryRepository` remaining 23 methods (semantic_search done). Audit each for `FETCH APPROX FIRST` opportunity — likely only similarity paths benefit; rest are exact filters.
- `Db2FederationRepository` 17 methods, including 2 MERGEs + interval arithmetic in `list_due_peers`
- Rewrite `db/migrations_db2/0001_core_schema.sql` to native types (VARCHAR/BIGINT/CLOB sized)
- Add `Db2BackendNative` + `MNEMOS_DB2_DIALECT` selector in `lifecycle.py`
- Write `tests/test_db2_dialect_parity.py` (one parametrized case per method; run against compat + native)
- Write `scripts/bench_v5.py` + capture A/B numbers (against Db2 12.1.5 GA, not EAP) into <archived bench artifact>

**Phase 3 — Compat deprecation + cleanup (≈ 3 working days)**
- Flip lifecycle default to `native`
- `DeprecationWarning` on `Db2BackendCompat`
- Remove `_adapt_oracle_to_db2` + cache + mask helpers + `_ORA_TO_DB2_PAIRS` + `_NVL_LITERAL_RE` from db2.py
- Strip Oracle subclassing from every `Db2*Repository` class (now inherits from base contract directly)
- Default `ENABLE_ORACLE_COMPATIBILITY=false` in `docker/db2-eap/entrypoint.sh`, keep the toggle as escape hatch
- Documentation pass: `docs/v6.1-roadmap.md`, `docs/db2-eap-recipe-2026-05-20.md`, `docs/oracle-port-status.md`, new `docs/native-db2-dialect.md`

**Total**: ~15 working days of focused work, ≈ 3 calendar weeks at one-engineer cadence with normal interruptions. Add 1 week buffer for the inevitable cursor-bind-order bug surfaced under load → **4 calendar weeks** realistic.

---

## 8. Risks + open questions

- **`NUMBER` → `BIGINT` vs `DECFLOAT(34)`.** Existing schema declares `recall_count NUMBER` (default 0). Under ORA-compat this maps to DECFLOAT(34); native we want BIGINT. Need a one-shot column-type migration (`ALTER TABLE memories ALTER COLUMN recall_count SET DATA TYPE BIGINT`) on existing deployments. Db2 supports inline ALTER for numeric type changes when no overflow — verify with proof-harness data.
- **`Db2 12.1.5 EAP vs GA SQL surface.** 12.1.5 ships DiskANN; verify `FETCH APPROX FIRST` syntax doesn't shift between EAP and GA (Jun 9 2026). If `APPROX` keyword moves under a different clause, the override breaks. Pin against the published GA syntax once available; today we're EAP.
- **Positional `?` bind ordering errors.** Today's translation layer extracts `_BIND_RE.findall(adapted)` ordering from the rewritten SQL. Pure native code with explicit `?` requires the caller to pass params in textual order — high-risk for refactors that reorder predicates. Mitigation: keep the `(sql, params)` shape but require `params` is a tuple (positional) in native code, not a dict. Add a lint check / mypy override.
- **MERGE concurrency under load.** Db2 MERGE has slightly different deterministic-target semantics from Oracle — under heavy contention on `state.set` or `federation_peers.upsert_peer`, Db2 may raise `SQL0913N` (deadlock) where Oracle returns silently. Validate with concurrent-writer stress in bench-v5.
- **`NUMTODSINTERVAL` rewrite.** Interval arithmetic `CURRENT TIMESTAMP - n SECONDS` is the natural native form, but Db2 may require `CURRENT TIMESTAMP - (n) SECONDS` or `CURRENT TIMESTAMP - INTERVAL n SECOND` — confirm exact syntax against 12.1.x docs. Used at `list_due_peers` (oracle.py:2145).
- **`TO_CHAR(updated)`** in `OracleStateRepository.get`. Db2 native `TO_CHAR` exists in ORA-compat. Pure-native form is `VARCHAR_FORMAT(updated, 'YYYY-MM-DD HH24:MI:SS.FF6')` — verify the consumer of this column (state KV in MNEMOS) is format-stable across the rewrite.
- **`OFFSET … FETCH NEXT` pagination.** Already ISO SQL, both backends compatible. No change.
- **Db2 Text Search availability for `fts_search`.** Oracle uses `CONTAINS` (Oracle Text). Db2 Text Search is a separate optional install (`db2ts ENABLE FOR TEXT`). If the EAP container doesn't have it enabled, `fts_search` must fall back to `LIKE '%term%'` with substring matching. Document the operator step + add a feature probe.
- **`set_suppress_version_snapshot`.** Oracle path may use a session-level variable or context. Db2 equivalent (global temp table or session register) needs verification — likely needs custom implementation, not a simple SQL rewrite.

---

## 9. Validation checklist

Every test file below must pass before the port is declared done:

- `tests/test_db2_live.py` — existing integration tests against the EAP container
- `tests/test_db2_semantic_search_dialect.py` — semantic_search emits the documented native SQL shape (extend to cover all repos via the new parity file below)
- `tests/test_db2_translation_string_safety.py` — translation layer still safe-on-strings during Phase 1/2 coexistence
- **NEW** `tests/test_db2_dialect_parity.py` — one parametrized case per ported method × {compat, native}; rows must match exactly
- `tests/test_persistence_interface.py` — abstract contract conformance for both Db2 backends
- `tests/test_persistence_parity.py` — cross-backend parity (Oracle, Db2-compat, Db2-native, Postgres, SQLite)
- `tests/test_persistence_helpers.py` — `_validate_and_format_vector`, `_render_visibility` semantics unchanged
- `tests/test_oracle_vector_validation.py` — vector path tests still hold
- `scripts/db2_proof_run.py` — green against `MNEMOS_DB2_DIALECT=native`
- `scripts/bench_v5.py` — results published against Db2 12.1.5 GA
- `scripts/db2_apply_migration.py` — applies the native-type 0001 schema cleanly to a fresh container
- Db2 EAP container starts cleanly with `ENABLE_ORACLE_COMPATIBILITY=false`

---

## 10. What ships in v6.0 (NOT this port)

The port is **implementation-only**. User-visible surface is unchanged. Specifically:

- **Backend factory API**: `mnemos.persistence.db2.Db2Backend`, `create_db2_pool(...)` — unchanged signatures
- **MCP tool list**: identical — `mcp__mnemos__*` (search, create, get, list, …) — no tool added or removed
- **REST endpoints**: identical — `/memories/search`, `/memories`, `/kg/*`, `/federation/*`, `/state/*`
- **Public docs about backends**: `docs/INSTALL.md`, `docs/MEMORY_ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/SCALING.md` — describe Db2 as a supported backend; native-vs-compat is an internal implementation detail
- **DSN format**: `db2://user:pass@host:port/database` — unchanged
- **Environment variables**: `MNEMOS_DB2_VECTOR_INDEX={approx|exact}` — unchanged. **NEW** `MNEMOS_DB2_DIALECT={compat|native}` is added for Phase 2 transition only; removed (or hardcoded `native`) in Phase 3
- **Migration files**: `db/migrations_db2/0001_core_schema.sql` content changes (Phase 2) but apply target + script unchanged
- **Vector index name**: `idx_memories_emb_diskann` — unchanged
- **Performance characteristics**: `semantic_search` already native (no change); other methods expected to improve per Phase 2 bench (run against Db2 12.1.5 GA, not EAP); no regression expected, validated by parity tests
- **Operator workflow**: identical — `db2set DB2_VECTOR_INDEXING=YES`, `db2start`, point MNEMOS at the DSN. The ORA_COMPATIBILITY toggle defaults flip from on to off in Phase 3, but the toggle remains for fallback.

---

*Plan compiled 2026-05-21 from adversarial Db2-eng audit + post-rc1 fixes. The next engineer can start at §2.1, pick the smallest repo (`Db2WebhookRepository.dispatch_event`, one method), write its native override + parity test, validate against the EAP container, then climb the dependency tree.*
