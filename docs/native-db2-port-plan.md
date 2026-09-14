# Native Db2 Dialect — Design and Reference

**Originally written:** 2026-05-21 as a requirements/design plan.
**Status:** **Delivered.** Every persistence repository emits native Db2 SQL,
the migration DDL is native-typed, `Db2BackendNative` ships with a
pass-through cursor, and the container entrypoint defaults to
`ENABLE_ORACLE_COMPATIBILITY=false`. The remaining work is the final
compat-layer removal (§7 Phase 3) — the compat path is still the
`MNEMOS_DB2_DIALECT` default and the Oracle→Db2 translation helpers are
still in `mnemos/persistence/db2.py` as a fallback.

This document now serves two purposes: a record of what was ported, and the
dialect reference (§3, §4) that any future Db2 work should start from.

Citations below name functions and classes rather than line numbers, because
line numbers in a ~5,600-line `db2.py` and a ~9,000-line `oracle.py` go stale
faster than the prose does.

---

## 1. Why a native Db2 dialect

- **Optimizer visibility.** Under compat mode every query travels through
  `_Db2AsyncCursor.execute` → `_adapt_oracle_to_db2` → regex/replace rewrites
  (`SYSTIMESTAMP`→`CURRENT TIMESTAMP`, `:name`→`?`,
  `TO_VECTOR(?)`→`VECTOR(?, dim, FLOAT32)`). The Db2 optimizer sees the
  rewritten output, never the canonical Db2 form — plan-cache and
  statement-text matching are degraded.
- **Parse-time overhead.** Every compat call hits `_translate_sql_cached`
  (LRU 512) plus a per-call positional-bind pass and a `dim` scan over params.
  Even with the cache, the first hit on each unique SQL pays full
  mask/restore/regex cost.
- **Removes Oracle Compatibility Mode as a runtime dependency.** IBM ships
  ORA-compat as a migration aid; relying on it as a steady-state production
  posture is awkward when the claim is "we ship native Db2" and a reviewer can
  run `db2set -all` and see ORA-compat on.
- **DiskANN engagement.** Native syntax (`VECTOR_DISTANCE(..., EUCLIDEAN)` plus
  `FETCH APPROX FIRST K ROWS ONLY`) is the only path that engages the Db2
  12.1.5 DiskANN index. Inherited COSINE plus `FETCH FIRST` falls back to an
  exact scan.
- **z/OS reach (partial).** Native LUW SQL is a step closer to Db2 z/OS
  compatibility than Oracle-compat output — see §6 for the honest scope.

---

## 2. What shipped

### 2.1 `mnemos/persistence/db2.py` — repository overrides

Every `Db2*Repository` class subclasses its Oracle counterpart but **overrides
each method with explicit native Db2 SQL**: `?` positional binds,
`CURRENT TIMESTAMP` / `CURRENT DATE`, `FROM SYSIBM.SYSDUMMY1` for MERGE
sources, and `VECTOR(?, dim, FLOAT32)` for vector literals. `_Db2OraCompatMixin`
supplies the token constants (`_SQL_NOW`, `_SQL_TSTZ_CAST`, `_SQL_TODAY`) so an
override can cite the Db2 form by name rather than inlining a string.

| Repository | Native method overrides |
|---|---|
| `Db2MemoryRepository` | 29 |
| `Db2FederationRepository` | 22 |
| `Db2OAuthRepository` | 8 |
| `Db2ConsultationAuditRepository` | 5 |
| `Db2CompressionRepository` | 5 |
| `Db2StateRepository` | 5 |
| `Db2VersionRepository` | 4 |
| `Db2BranchRepository` | 4 |
| `Db2ConsultationsRepository` | 4 |
| `Db2AclRepository` | 4 |
| `Db2KGRepository` | 3 |
| `Db2WebhookRepository` | 3 |
| `Db2MorpheusRepository` | 2 |

`Db2SessionsRepository` and `Db2AuditChainRepository` carry no native
overrides: their inherited Oracle SQL is already dialect-neutral for this
schema. `Db2Backend` itself overrides 20 backend-level methods.

The methods that needed real thought, and are worth reading before writing a
new one:

- **`Db2MemoryRepository.semantic_search`** — the reference shape for the
  whole port. `VECTOR_DISTANCE(..., EUCLIDEAN)` plus
  `FETCH APPROX FIRST ? ROWS ONLY`. EUCLIDEAN over COSINE is
  correctness-preserving for L2-normalized embeddings: for unit-norm `a`, `b`,
  `|a-b|² = 2 - 2·cos(a,b)`, so both metrics produce the same top-K ordering.
  `MNEMOS_DB2_VECTOR_INDEX={approx|exact}` selects index engagement versus
  exact scan; `approx` is the default.
- **`Db2StateRepository.set`** — the first `MERGE INTO ... USING (SELECT ?
  ... FROM SYSIBM.SYSDUMMY1)` on the port; the pattern the Branch and
  Federation MERGEs were built from.
- **`Db2FederationRepository.list_due_peers`** — native interval arithmetic
  replacing Oracle's `NUMTODSINTERVAL(n, 'SECOND')`.
- **`Db2MemoryRepository.fts_search`** — Db2 Text Search is a separate optional
  install (`db2ts ENABLE FOR TEXT`), so the deterministic fallback is substring
  matching rather than Oracle's `CONTAINS`.
- **`Db2StateRepository.get`** — keeps `TO_CHAR(updated)`; it is native in Db2
  12.1.x, not an ORA-compat affordance.

### 2.2 `Db2BackendNative` and the dialect selector

`Db2BackendNative` (subclass of `Db2Backend`) pairs the same repository wiring
with `_Db2NativeAsyncCursor` / `create_db2_native_pool` — a pass-through cursor
that performs **no** Oracle→Db2 token translation. It overrides
`_LIVENESS_PROBE_SQL` to `SELECT 1 FROM SYSIBM.SYSDUMMY1`, because the native
cursor rejects the inherited `FROM DUAL` probe.

`mnemos/core/lifecycle.py::_build_db2_backend` reads `MNEMOS_DB2_DIALECT`
(alias `PG_DB2_DIALECT`, resolved on `settings.database.db2_dialect`):

- `compat` — **current default.** `Db2Backend` over `create_db2_pool`, with
  cursor-layer translation as a safety net.
- `native` — `Db2BackendNative` over `create_db2_native_pool`.
- Anything else — logs a warning and falls back to `compat`.

`backend.open()` runs the `DB2_VECTOR_INDEXING` registry probe on both paths.

### 2.3 Compat translation layer — still present, still the default

`_ORA_TO_DB2_PAIRS`, `_TO_VECTOR_RE`, `_BIND_RE`, `_VECTOR_CALL_RE`,
`_NVL_LITERAL_RE`, `_mask_sql_literals_and_comments`, `_unmask_sql`,
`_translate_sql_cached` and `_adapt_oracle_to_db2` all remain in `db2.py`.
They are dead code for the production SQL path now that every repository emits
native SQL, but they are retained deliberately as the fallback for operators
running customized repositories with Oracle-shape SQL. Removing them is the
Phase 3 work in §7.

The cursor itself stays regardless of dialect: it is the `asyncio.to_thread`
bridge over `ibm_db_dbi`, which is independent of SQL dialect.

### 2.4 `mnemos/db_migrations/migrations_db2/` — native DDL

`0001_core_schema.sql` is native-typed and states so in its header: it does
**not** require `ENABLE_ORACLE_COMPATIBILITY=true`. `VARCHAR2` no longer
appears anywhere in it; the schema uses `VARCHAR`, `DECIMAL`, `BIGINT`,
`TIMESTAMP`, `CLOB` and `VECTOR(d, FLOAT32)`. The chain now runs through the
0061-series parity migrations.

Statement terminator: `@`. **A file may not mix `;` and `@`.**
`split_db2_statements` (`mnemos/persistence/schema.py`) switches the entire
file to `@`-only splitting the moment any line ends with `@`, so a mixed file
silently concatenates every `;`-terminated statement into one invalid blob.
Every statement in an `@`-terminated file, including plain `ALTER TABLE`, must
end with a bare `@` on its own line.

### 2.5 Db2 SQL PL traps in guarded DDL

These are exact, measured constraints of the Db2 12.1.5 instance this project
targets. Both were latent for months because the affected statements had never
actually been executed against a live instance — a migration file that parses
is not a migration file that runs.

- **`DECLARE ... HANDLER` must live in a plain `BEGIN ... END`, never
  `BEGIN ATOMIC`.** Db2 rejects it outright inside an ATOMIC compound
  statement:
  `SQL0104N unexpected token "HANDLER" ... expected "CONDITION"`.
  Every idempotency guard in the migration chain therefore uses plain `BEGIN`.
- **The SQLSTATE for "column does not exist" is `42703`, not `42704`.** A
  guarded `DROP COLUMN` that handles `42704` does not swallow anything and the
  migration crash-loops on replay. `42704` is *undefined object* — a missing
  **constraint** or table — so a guarded `DROP CONSTRAINT` correctly handles
  `42704`. The two are not interchangeable.

The working idiom, as used in `0061c_morpheus_runs_parity.sql`:

```sql
BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42703'
        BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE morpheus_runs DROP COLUMN run_type';
END
@
```

Note also that the migration applier swallows SQLSTATE `42711` (column already
exists) as a benign-replay signal, so a plain `ALTER TABLE ... ADD COLUMN`
needs no guard — but `42710` / `42P07` / `42701` are the only other codes it
treats that way. Anything else needs an explicit handler block.

Both of these sit under the standing replay principle: the migration runner has
no applied-state tracking and replays the full chain on every boot, so every
statement must independently be safe against a fresh **and** an
already-migrated database. See
[`docs/PERSISTENCE_ABC_STANDARDIZATION.md`](PERSISTENCE_ABC_STANDARDIZATION.md)
"Item 5".

### 2.6 `docker/db2-eap/entrypoint.sh`

`ENABLE_ORACLE_COMPATIBILITY` **defaults to `false`.** The container no longer
sets `db2set DB2_COMPATIBILITY_VECTOR=ORA` or
`UPDATE DB CFG USING ORA_COMPATIBILITY ON` unless the toggle is explicitly
turned on. The toggle is retained as an emergency fallback for operators
running customized Oracle-shape repository overrides. Recipe:
[`docs/db2-eap-recipe-2026-05-20.md`](db2-eap-recipe-2026-05-20.md).

### 2.7 Other touched files

| File | Role |
|---|---|
| `scripts/db2_proof_run.py` | Repository-surface proof harness; should pass against both dialect modes |
| `scripts/db2_apply_migration.py` | Splits on `@` regardless of `--#SET TERMINATOR` directives |
| `scripts/bench_three_backends.py`, `scripts/bench_four_backends.py` | Cross-backend latency benches |
| `mnemos/core/lifecycle.py::_build_db2_backend` | Dialect selector (§2.2) |
| `tests/test_db2_dialect_parity.py` | Compat-vs-native emitted-SQL parity |

---

## 3. Type mapping reference

| Oracle type | Db2 native | Range / behavior delta | Notes |
|---|---|---|---|
| `VARCHAR2(n)` | `VARCHAR(n)` | Identical for n ≤ 32672 | NULL semantics differ in one place: empty string `''` is **not** NULL in Db2 (Oracle treats `''` as NULL). The only real behavior delta. |
| `CLOB` | `CLOB(1M)` default; `CLOB(2G)` max | Db2 needs an explicit size or defaults to 1MB | `CLOB(1M)` for content, `CLOB(64K)` for metadata; inline if small |
| `NUMBER` (no precision) | `BIGINT` for counters, `DOUBLE` for floats, `NUMERIC(p,s)` for fixed precision | Under ORA-compat `NUMBER` maps to `DECFLOAT(34)` — 17 bytes vs BIGINT's 8 | Choose deliberately per column |
| `NUMBER(p)` | `INTEGER` (p ≤ 9), `BIGINT` (p ≤ 18), `NUMERIC(p)` (any) | Range explicit | |
| `NUMBER(p,s)` | `NUMERIC(p,s)` | Identical decimal semantics | |
| `TIMESTAMP` | `TIMESTAMP` | Both 6 fractional digits by default; both take `(n)` | Identical |
| `TIMESTAMP WITH TIME ZONE` | `TIMESTAMP` | Db2 12+ supports TZ, but the schema collapses to naive UTC | Deliberate: matches what the Oracle path collapses to anyway |
| `DATE` | `DATE` | Db2 `DATE` is date-only; Oracle `DATE` includes a time component | **WATCH**: an Oracle `DATE` column needing time precision must port to `TIMESTAMP` |
| `BLOB` | `BLOB(n)` | Same sizing rules as CLOB | not used in schema |
| `VECTOR(d, FLOAT32)` | `VECTOR(d, FLOAT32)` | Identical | native both sides |
| `RAW(n)` | `VARBINARY(n)` | Identical | not used in schema |

The empty-string caveat is documentation-only for MNEMOS: application code
already treats empty strings and NULLs separately, with no Oracle-style
`'' IS NULL` reliance in any repository.

---

## 4. SQL idiom mapping reference

| Oracle pattern | Native Db2 pattern | Notes |
|---|---|---|
| `FROM DUAL` | `FROM SYSIBM.SYSDUMMY1` | ORA-compat aliases DUAL; native code uses SYSDUMMY1. Appears in the backend liveness probe and inside every MERGE `USING` clause |
| `SYSTIMESTAMP` | `CURRENT TIMESTAMP` | `_Db2OraCompatMixin._SQL_NOW` |
| `SYSDATE` | `CURRENT DATE` | `_Db2OraCompatMixin._SQL_TODAY` |
| `NVL(a, b)` | `COALESCE(a, b)` | COALESCE is ISO SQL; Db2 also accepts NVL under ORA-compat |
| `DECODE(x, a, 1, b, 2, 0)` | `CASE x WHEN a THEN 1 WHEN b THEN 2 ELSE 0 END` | Not used in MNEMOS; noted for future work |
| `SUBSTR`, `LENGTH`, `TRIM`, `UPPER`, `LOWER` | Identical | native both sides |
| `MOD(x, y)` | `MOD(x, y)` | native both sides — used in the permission-bit visibility clause |
| `TRUNC(numeric)` | `TRUNC(numeric)` or `TRUNCATE(x, 0)` | Db2 native; used in the visibility clause's group-bit extraction |
| `TO_CHAR(ts)` | `TO_CHAR(ts)` or `VARCHAR_FORMAT(ts, 'YYYY-MM-DD HH24:MI:SS.FF6')` | `TO_CHAR` is native in Db2 12.1.x — `Db2StateRepository.get` keeps it |
| `NUMTODSINTERVAL(n, 'SECOND')` | native interval arithmetic | Used in `list_due_peers`; the parameterized form may need `CURRENT TIMESTAMP - (CAST(? AS INTEGER) * 1 SECOND)` depending on driver binding |
| `ROWNUM` | `ROW_NUMBER() OVER`, `FETCH FIRST n ROWS ONLY`, `OFFSET … FETCH NEXT …` | not used in `oracle.py` |
| `:name` named binds | `?` positional binds | ibm_db_dbi standard. Native overrides write `?` directly, removing the dict→positional reorder |
| `RETURNING col INTO :var` | `SELECT col FROM FINAL TABLE (INSERT …)` / `NEW TABLE (UPDATE …)` | **Not used in MNEMOS** — no RETURNING clauses anywhere |
| `MERGE INTO t USING (… FROM DUAL) …` | `MERGE INTO t USING (… FROM SYSIBM.SYSDUMMY1) …` | Four MERGE sites: `upsert_memory_branch_head`, `upsert_peer`, `apply_consolidation_tombstone`, `StateRepository.set`. Db2 MERGE adds optional `ELSE IGNORE`, has no `RETURNING`, and applies a slightly stricter deterministic-target rule. All four shapes work natively. |
| `LIMIT n` | `FETCH FIRST n ROWS ONLY` | `oracle.py` already uses the ISO form everywhere |
| `OFFSET n ROWS FETCH NEXT m ROWS ONLY` | identical | ISO SQL, Db2 native |
| `TO_VECTOR(:q)` | `VECTOR(?, dim, FLOAT32)` | Db2 12.1.x native vector constructor. **`dim` must be embedded in the SQL string** — it is not bindable on Db2. |
| `VECTOR_DISTANCE(a, b, COSINE)` / `EUCLIDEAN` | identical | native both sides |
| `FETCH APPROX FIRST n ROWS ONLY` | Db2-only | Required to engage DiskANN; used by `semantic_search` |
| `DEFAULT NEXT VALUE FOR seq` | `DEFAULT NEXT VALUE FOR seq` or `GENERATED ALWAYS AS IDENTITY` | MNEMOS uses application-generated string IDs — no sequences |

---

## 5. Test coverage

The risk a native port carries is that one query's semantics change silently —
wrong row, wrong order, wrong NULL handling — while the suite passes because it
covered the row shape and not the exact predicate. What guards against that:

- `tests/test_db2_dialect_parity.py` — compat-vs-native parity on emitted SQL.
- `tests/test_db2_native_cursor.py` — the `MNEMOS_DB2_DIALECT` selector and the
  pass-through cursor.
- `tests/test_db2_semantic_search_dialect.py` — `semantic_search` emits the
  documented native shape.
- `tests/test_db2_translation_string_safety.py` — the compat translation layer
  stays safe on string literals while it remains in tree.
- `tests/test_db2_migration_syntax.py` — migration DDL syntax, including the
  terminator rule in §2.4.
- `tests/test_db2_live.py` — integration against a live container.
- `tests/test_db2_oauth_sessions_consultations.py`,
  `tests/test_db2_session_ownership.py`,
  `tests/test_db2_webhook_repository.py` — per-surface behaviour.
- `tests/test_persistence_interface.py` — ABC conformance for both Db2 backends.
- `tests/test_persistence_parity.py` — cross-backend parity.
- `tests/test_persistence_helpers.py` — `_validate_and_format_vector` and
  `_render_visibility` semantics.

**Gap:** there is no dedicated native-vs-compat *latency* benchmark. The plan
called for a `scripts/bench_v5.py`; it was never written and the file does not
exist. `scripts/bench_three_backends.py` and `scripts/bench_four_backends.py`
cover cross-backend comparison but not the native/compat A/B on one engine.
This is the one genuinely outstanding item from the original plan — it matters
only for quantifying the translation-layer overhead described in §1, not for
correctness.

---

## 6. z/OS portability scope — honest answer

**DRDA is the network protocol.** A native LUW dialect does **not**
automatically work on z/OS — DRDA tells the client how to talk to a remote Db2,
not what SQL surface that Db2 accepts. z/OS Db2 has its own dialect quirks:

- z/OS does not support `MERGE INTO … USING (… FROM SYSIBM.SYSDUMMY1)` the same
  way LUW does — there are restrictions on the source table expression.
- The z/OS `VECTOR` data type is on a different release train from LUW 12.1.x.
- z/OS reserved-word and identifier limits differ slightly.
- z/OS has no LUW `SYSIBMADM.REG_VARIABLES` view, which is what the startup
  probe reads.
- z/OS catalog views live under `SYSIBM` directly, not `SYSIBMADM`.

**What native LUW SQL does buy:** closer-to-portable code for **Db2 Warehouse**
and **Db2 Cloud (DPF mode)**, both LUW-derived. Incremental z/OS compatibility
comes free (CURRENT TIMESTAMP, COALESCE, FETCH FIRST, MERGE shape), but the
VECTOR layer and the startup probe are LUW-only and would need a separate
adapter.

**Realistic posture:** a z/OS adapter is out of scope for the v6.x line. It
would require z/OS Db2 with native VECTOR, z/OS-specific catalog probes, and an
ANN index strategy review. The native dialect port is a **prerequisite** for
that work, not a delivery of it.

---

## 7. Remaining work — Phase 3 (compat deprecation)

Phases 1 and 2 of the original plan (native repository overrides, native
migration DDL, `Db2BackendNative`, the dialect selector, parity tests) are
delivered. Phase 3 is partially delivered: the container entrypoint already
defaults `ENABLE_ORACLE_COMPATIBILITY=false`. What remains:

- Flip the `MNEMOS_DB2_DIALECT` lifecycle default from `compat` to `native`.
- Emit a `DeprecationWarning` on the compat path, with a one-release window.
- Remove `_adapt_oracle_to_db2`, `_translate_sql_cached`, the mask/unmask
  helpers, `_ORA_TO_DB2_PAIRS` and `_NVL_LITERAL_RE` from `db2.py`, and drop
  `tests/test_db2_translation_string_safety.py` with them.
- Optionally strip the Oracle subclassing from each `Db2*Repository` so they
  inherit the base ABC directly. This is cosmetic once every method is
  overridden, and it costs the free inheritance of any *new* Oracle method that
  happens to be dialect-neutral — weigh that before doing it.
- Write the native-vs-compat latency A/B bench that §5 records as missing, if
  the overhead number is wanted before the compat path is deleted.

Sequencing note for whoever picks this up: flipping the default is the
reversible step and should land first, with the removal following only after a
release where `native` has been the default and nobody has needed the escape
hatch.

---

## 8. Risks and open questions

Resolved during the port:

- **`NUMBER` → `BIGINT` vs `DECFLOAT(34)`.** Settled by rewriting the migration
  DDL to native types. Existing ORA-compat deployments still need a one-shot
  column-type migration (`ALTER TABLE memories ALTER COLUMN recall_count SET
  DATA TYPE BIGINT`); Db2 supports inline ALTER for numeric widening when no
  value overflows.
- **Positional `?` bind ordering.** Native code requires params in textual
  order, which is fragile under predicate reordering. Mitigated by passing
  tuples (not dicts) in native overrides, so a mismatch fails loudly at the
  driver rather than binding the wrong column.
- **SQL PL guard idioms.** See §2.5 — `BEGIN` not `BEGIN ATOMIC`, and `42703`
  not `42704`.

Still open:

- **MERGE concurrency under load.** Db2 MERGE has slightly different
  deterministic-target semantics from Oracle; under heavy contention on
  `state.set` or `federation_peers.upsert_peer`, Db2 may raise `SQL0913N`
  (deadlock) where Oracle returns silently. Needs a concurrent-writer stress
  run.
- **Db2 Text Search availability for `fts_search`.** `db2ts ENABLE FOR TEXT` is
  a separate optional install. If a deployment lacks it, `fts_search` runs the
  substring fallback. A feature probe plus a documented operator step would be
  better than the current implicit fallback.
- **`set_suppress_version_snapshot`.** The session-scoped suppression flag has
  no direct Db2 equivalent to Oracle's session context; verify the current
  implementation's semantics under connection pooling, where a session register
  may outlive the intended scope.
- **`FETCH APPROX FIRST` syntax stability.** The DiskANN clause was pinned
  against the 12.1.5 EAP surface. Confirm it against GA before treating
  `semantic_search` as syntax-stable.

---

## 9. What this port does NOT change

The port is implementation-only. The user-visible surface is unchanged:

- **Backend factory API** — `mnemos.persistence.db2.Db2Backend`,
  `create_db2_pool(...)`: unchanged signatures. `Db2BackendNative` and
  `create_db2_native_pool` are additions, not replacements.
- **MCP tool list** — identical; no tool added or removed.
- **REST endpoints** — identical.
- **DSN format** — `db2://user:pass@host:port/database`, unchanged.
- **Environment variables** — `MNEMOS_DB2_VECTOR_INDEX={approx|exact}`
  unchanged. `MNEMOS_DB2_DIALECT={compat|native}` is the transition selector,
  removed or hardcoded to `native` when Phase 3 lands.
- **Vector index name** — `idx_memories_emb_diskann`, unchanged.
- **Operator workflow** — `db2set DB2_VECTOR_INDEXING=YES`, `db2start`, point
  MNEMOS at the DSN. The ORA_COMPATIBILITY container default is now off; the
  toggle remains for fallback.
