# Opus adversarial review — Oracle/Db2 layer (2026-05-20, post-OpenCode)

**Reviewer:** Claude Opus 4.7, fresh read of the current working tree
**Scope:** same as Codex round earlier today (mnemos/persistence/{db2,oracle,base}.py, both migration SQL files, both proof harnesses, 4 EE feature scripts, lifecycle dispatcher, DB2 container recipe)
**State going in:** Oracle 13/13 + Db2 12/12 (both proofs green with rotated HMAC key)
**Goal:** find things Codex AND OpenCode missed. Adversarial, no praise.

---

## CRITICAL — must fix before any v6.0 deploy beyond dev box

### O1. `db/migrations_db2/0001_core_schema.sql:1-11` — `CREATE TABLE memories` statement is GONE 🔴

The file opens with header comments then jumps directly to:

```sql
    -- Db2 12.1.2+ ships VECTOR data type. Dimension is required.
    -- {{embedding_dim}} placeholder substituted by db2_apply_migration.py
    -- (default 768 for nomic-embed-text, supports 384/1536/3072).
    embedding VECTOR({{embedding_dim}}, FLOAT32)
)@
```

There is NO `CREATE TABLE memories (id VARCHAR2(100) PRIMARY KEY, content CLOB, ...` opening anywhere. Oracle's equivalent has 13 columns + 7 ALTER ADD COLUMN statements. The Db2 file has the **single `embedding` column** as a orphan fragment with a stray `)@` closer.

**Why proofs still pass:** the running `MEMORIES` table on PYTHIA `db2-eap-test` was created by a PREVIOUS version of the migration that had the full CREATE TABLE. Existing dev DB has the table; the FILE that ships would not create it on a fresh install.

**Verification of severity:**
```python
$ python3 -c "import ibm_db_dbi; c=ibm_db_dbi.connect(...); cur=c.cursor(); cur.execute(\"SELECT tabname FROM syscat.tables WHERE tabschema='DB2INST1'\"); print([r[0] for r in cur.fetchall()])"
['FEDERATION_*', 'KG_TRIPLES', 'MEMORIES', 'MEMORY_*', 'STATE', 'VEC_TEST', 'WEBHOOK_*']
```

Table exists at runtime. Now check the migration file content — only the closing `)@` fragment. Fresh install on any other host (cixmini, CERBERUS standby, PROTEUS rebuild) **will fail at the SQL0104N syntax error** on the first orphan statement.

**Fix:** port the full Oracle `CREATE TABLE memories` block + all 7 trailing `ALTER ADD COLUMN federation_source / federation_remote_updated / recall_count / last_recalled_at / content_hash / archived_at / verbatim_content` statements. Translate `TIMESTAMP WITH TIME ZONE` → `TIMESTAMP`, etc. (already covered by the cursor translator, but DDL is pre-translation so do it inline in the SQL file).

### O2. `scripts/oracle_ee_pgq_proof.py:34` + `oracle_ee_duality_proof.py:34` — `os.environ.get(...)` without `import os` 🔴

Both EE proof scripts have:

```python
HMAC_KEY = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
if not HMAC_KEY or HMAC_KEY == "mnemos-oracle-proof-v1":
    ...
```

at the top of the module — outside any function. But neither imports `os`:

```
$ .venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import oracle_ee_pgq_proof"
NameError: name 'os' is not defined
$ .venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import oracle_ee_duality_proof"
NameError: name 'os' is not defined
```

**Both scripts crash at module load.** EE features #5 (JSON Relational Duality) and #6 (Property Graph SQL/PGQ) **cannot be re-proven** under the rotated HMAC key. Their existing signed artifacts (<archived>, <archived>) still verify under the old leaked key `b"mnemos-oracle-proof-v1"` but cannot be regenerated.

This is a regression introduced by OpenCode's workstream C1 rollout. `hnsw_bench.py` + `tde_proof.py` got the `import os` correctly; `pgq` + `duality` did not.

**Fix:** add `import os` to lines 13-22 import block in both files. Then re-run both EE proofs.

### O3. `scripts/oracle_ee_tde_proof.py:42-44` — fallback `args=None` reintroduces hardcoded secrets 🔴

```python
def docker_sqlplus(sql: str, service: str = "ORCLCDB", sysdba: bool = True, args=None) -> tuple[int, str]:
    if args is None:
        args = type('obj', (object,), {'host': '192.168.207.25', 'container': 'mnemos-oracle-ee', 'sys_pwd': 'mnemos_dev'})()
    auth = f"sys/{args.sys_pwd}@localhost:1521/" + service + (" as sysdba" if sysdba else "")
```

The `if args is None` branch creates a SimpleNamespace-like object with:
- Host: hardcoded `192.168.207.25` (PROTEUS)
- Container: hardcoded `mnemos-oracle-ee`
- SYS password: hardcoded `mnemos_dev`

**The first three calls in `main()` (lines 71, 80, 84) pass NO `args` parameter** — `docker_sqlplus(sql)` / `docker_sqlplus(set_sql)` etc. So the fallback fires and hardcoded creds are used. Workstream C4 only fixed the CLI surface; the actual call sites still hit the hardcoded path.

Even if all call sites passed args, defining the fallback at all means anyone reading the source can read the test credentials.

**Fix:** delete the `if args is None` block. Make `args` a required parameter. Update calls 71, 80, 84 to pass `args=args`.

---

## HIGH — open from prior reviews, still not fixed

These were flagged by Codex earlier today but OpenCode hasn't landed them yet.

### O4. `db2.py:306` — pool `_lock` STILL held during physical connect (A4 unaddressed) 🟠

```python
async def acquire(self):
    async with self._lock:                  # ← lock held
        if self._idle:
            conn = self._idle.pop()
        elif len(self._in_use) < self._max_size:
            conn = await self._open()       # ← slow blocking connect under lock
```

`_open()` does `await asyncio.to_thread(ibm_db_dbi.connect, ...)` — that's a real Db2 client handshake, can take 100ms-2s. All other coroutines (releases, acquires, closes) block on the lock during that window. Under burst load all calls serialize.

### O5. `db2.py:308` — pool exhaustion still raises immediately (A5 unaddressed) 🟠

```python
else:
    raise RuntimeError("DB2 pool exhausted")
```

Every well-behaved async pool (asyncpg, oracledb, redis-py) waits with timeout. Burst load triggers 503s that would have succeeded with 100ms wait.

### O6. `oracle.py:107` + `oracle.py:2324` — `_is_unique_violation` Oracle-only (A6 unaddressed) 🟠

Helper checks only for `ORA-00001`. Db2 uses SQLSTATE `23505` + `SQL0803N`. Federation duplicate pulls onto Db2 raise hard instead of silently dedupe.

### O7. `db/migrations_db2/0001_core_schema.sql` — missing 5 Oracle tables (B3 unaddressed) 🟠

`users`, `sessions`, `session_messages`, `deletion_log`, `compression_manifest` all absent from Db2. APIs hitting these die with SQL0204N on Db2 deployments.

### O8. `db/migrations_db2/0001_core_schema.sql` — not actually idempotent (B2 unaddressed) 🟠

Comment claims "wrapped for idempotency" but file emits raw `CREATE TABLE`. Replay only works because `db2_apply_migration.py` treats SQLSTATE 42710 as benign.

---

## HIGH — NEW (Opus, post-OpenCode)

### O9. `db2.py:284-291` — pool `_min_size` declared but never read 🟠

```python
def __init__(self, dsn_kwargs: dict, *, min_size: int = 1, max_size: int = 8):
    ...
    self._min_size = min_size  # stored
    ...
    # never read again anywhere in the class
```

Configured warm pool size is fiction. Pool never pre-opens connections — first N acquires pay full connect latency. Either implement warmup (open `min_size` connections in a startup task) or delete the parameter.

### O10. `db2.py:296` — DSN attribute injection via `;` in UID/PWD 🟠

```python
dsn_string = ";".join(f"{k}={v}" for k, v in self._dsn_kwargs.items()) + ";"
```

If `PWD` contains a literal `;`, the DSN becomes `UID=user;PWD=p;INJECTED=attr;DATABASE=...`. Db2 CLI parses extra attributes happily. Combined with `unquote(parsed.password)` (line 339) which URL-decodes `%3B` → `;`, this means a URL-encoded password can inject arbitrary CLI attributes (e.g. `AUTHENTICATION=SERVER_ENCRYPT_AES` or worse).

Real attack surface: federation peer auth_token, env var override, malformed config.

**Fix:** validate that none of UID/PWD/DATABASE contain `;` or `=`. Raise `ValueError` if so. Optionally use ibm_db's structured `connect(database=, user=, password=, host=, port=)` keyword form which avoids the DSN-string layer entirely.

### O11. `db2.py:140-211` — `_adapt_oracle_to_db2` runs full mask + 4-pass regex on every cursor execute 🟠

Per-call cost on a hot path:
1. `_NVL_LITERAL_RE.sub` on raw SQL
2. `_mask_sql_literals_and_comments` — 4 sequential `re.sub` passes (block-comment, line-comment, single-quote, double-quote)
3. `_ORA_TO_DB2_PAIRS` — 3 `str.replace` passes
4. `_TO_VECTOR_RE.sub`
5. `_BIND_RE.findall`
6. `_BIND_RE.sub("?")`
7. `_VECTOR_CALL_RE.sub`
8. `_unmask_sql` (with sorted reverse iteration)

For 100 ops/sec sustained this is invisible. For batched bulk insert (e.g. `bulk_create_memories` from `mnemos/api/v1/memories.py:bulk`) running thousands of identical SQL statements per minute, it's pure overhead — same SQL text translates to the same output every time.

**Fix:** memoize on `sql` identity. SQL strings in repo methods are module-level constants, so `id(sql)` is stable. Use `functools.lru_cache(maxsize=256)` with `(sql, type(params))` as key, return `(adapted_sql, name_list)`; positional binding still happens per-call.

### O12. `db2.py:69-124` — mask doesn't handle Oracle's `q'[...]'` / `q'{...}'` alternate-quote literals 🟠

```python
sql = re.sub(r"'(?:''|[^'])*'", replacer, sql)
```

Oracle 23ai supports `q'[any string with ' inside]'`, `q'{...}'`, `q'<...>'`, `q'(...)'`. The mask only handles standard `'...''...'`. Any future Oracle SQL emitter using `q'[...]'` to embed user content with embedded quotes will leak through the mask:

```sql
INSERT INTO x VALUES (q'[don't worry SYSTIMESTAMP]')
```

Bytes after `q'[` are unmasked. `SYSTIMESTAMP` rewrite would corrupt user content.

Not exploited today because no repository uses `q'[]'`. But any future Oracle codebase addition can silently corrupt without a test failure (we'd need to write user content with embedded `'` to a `VARCHAR2`).

**Fix:** add patterns for the 4 Oracle alternate-quote forms. Or document explicit prohibition in `oracle.py` against `q'[]'` syntax (more realistic given Codex won't translate them anyway).

---

## MEDIUM — NEW

### O13. `db2.py:127 _unmask_sql` — reverse-order replace is cargo cult 🟡

Reverse iteration documented "for safety" but mathematically impossible to collide. Placeholders are `\x00<digits>\x00` with NUL byte delimiters at both ends — these can never substring-match each other because the NUL bytes don't overlap (each placeholder consumes 2 NULs that no other placeholder can share). Simplify to forward iteration; cite invariant in docstring.

Minor — just clarity.

### O14. `db2.py:140` — `_adapt_oracle_to_db2` is 70 lines doing 7 things 🟡

Hard to test corner cases in isolation. Split:
- `_pre_mask_nvl_widen(sql) -> sql`
- `_mask(sql) -> (masked, restore_map)`
- `_translate_keywords(masked) -> masked`
- `_translate_to_vector(masked) -> masked`
- `_extract_binds(masked, params) -> (positional, masked_with_qm)`
- `_expand_vector_call(masked, positional) -> masked`
- `_unmask(masked, restore_map) -> sql`

Each unit-testable. Today no test exists for the literal-mask round-trip — a missed regression in any stage goes undetected until end-to-end proof.

### O15. `db2.py:434-465` — `Db2Backend.__init__` skips `super().__init__()` 🟡

```python
def __init__(self, pool: Any, settings: Any):
    # Re-implement OracleBackend __init__ without calling super so
    # we can install Db2-typed repos cleanly.
    self._pool = pool
    self._settings = settings
    self._closed = False
    self._memories_repo = Db2MemoryRepository()
    ...
```

If `OracleBackend.__init__` ever adds a new `_X_repo` attribute, `Db2Backend` silently drops it. No error, no warning — just a missing attribute that fails at first access. Future-proof:

```python
def __init__(self, pool, settings):
    super().__init__(pool, settings)
    # Override with Db2 subclass instances
    self._memories_repo = Db2MemoryRepository()
    ...
```

(Codex M8 also flagged this; OpenCode hasn't landed yet.)

### O16. `db2.py:284-291` — no `acquire_timeout` param 🟡

When A5 lands and the pool starts waiting on exhaustion, callers will want to override timeout (long-running batch jobs vs interactive). Add `acquire_timeout: float = 30.0` to `__init__` and `acquire()`.

### O17. `scripts/db2_apply_migration.py:34` — SQLSTATE `42704` still benign 🟡

```python
BENIGN_SQLSTATES = {"42710", "42704", "42P07"}
```

`42704` = undefined name referenced. Treating this as benign masks real broken dependencies. Should only allow `42710` (duplicate object) for create-replay. Codex M12 unaddressed.

### O18. `scripts/oracle_ee_tde_proof.py:174` — fragile pre/post-rotation note 🟡

```python
"note": "pre-key-rotation-artifact" if "mnemos-oracle-proof-v1" in str(HMAC_KEY) else "post-key-rotation",
```

`HMAC_KEY` is now `bytes` (from `os.environ.get(...).encode()`). `str(bytes)` returns `b'<hex>...'`. The literal `"mnemos-oracle-proof-v1"` will never appear inside that representation now that the key is rotated. **Always returns "post-key-rotation" regardless of actual provenance.** Misleading at audit time.

Either remove the note, or compute key fingerprint comparison properly: `("post-key-rotation" if sha256(HMAC_KEY).hexdigest()[:16] != "fab6dc8022fff2bc" else "pre-key-rotation")`.

### O19. `db2.py:308` — pool exhaust error message has zero diagnostic value 🟡

```python
raise RuntimeError("DB2 pool exhausted")
```

Should be:

```python
raise RuntimeError(
    f"DB2 pool exhausted: in_use={len(self._in_use)} idle={len(self._idle)} "
    f"max_size={self._max_size} closed={self._closed}"
)
```

(Goes away when A5 lands and the code starts waiting instead.)

### O20. `migrations_db2/0001_core_schema.sql` — no version/migration ID header 🟡

Future migration framework (per B2's full versioning fix) needs identity. Add at top:

```sql
-- migration_id: db2/0001_core_schema
-- depends_on: (none)
-- created: 2026-05-20
```

Not load-bearing for v6.0 but cheap to add now.

---

## LOW — NEW

### O21. `db2.py:343 create_db2_pool` — no warmup task 🟢

`min_size` parameter exists but no startup task pre-opens that many connections. First N requests pay full connect latency. (Goes away with O9.)

### O22. `scripts/oracle_ee_pgq_proof.py + duality_proof.py` — print/exit pattern inconsistent with hnsw/tde 🟢

`hnsw_bench.py` and `tde_proof.py` use `print(..., file=sys.stderr)` + `sys.exit(1)`. `pgq` + `duality` use the same shape but the HMAC check is duplicated literally 4 times across 4 files. Codex M14 (shared module) unaddressed. Now that 4 scripts repeat the exact 5-line block, factor:

```python
# scripts/_ee_common.py
def require_hmac_key() -> bytes: ...
def hmac_sign(key: bytes, payload: dict) -> str: ...
```

---

## Counter-positive: things OpenCode got RIGHT under adversarial scrutiny

For balance, what survived this pass:

✓ Mask-first architecture (A1/A2/A3) — the regex order issue (NVL widening pre-mask vs post-mask) was a real bug now fixed; the underlying mask is sound for SQL not using `q'[]'`
✓ `_NVL_LITERAL_RE` widening correctly targets only our SQL emitter's defaults, not user content
✓ Removing the buggy null-cast fold + relying on Db2 native NULL-bind handling — proven by 12/12 passing
✓ HMAC env-var fail-closed pattern — correct in 4 of 6 scripts (the 2 broken ones are TODAY's regression, not earlier design)
✓ Cursor-level translation pattern (DeepSeek's design) holds up — no Oracle SQL refactoring needed for Db2
✓ `_Db2OraCompatMixin` is documented + the constants WOULD be useful for future overrides; not dead weight now that there's a stated future-override use case

---

## Summary

**33 findings in Codex round** → 3 CRITICAL fixed in-session by Opus, 13 HIGH triaged to OpenCode workstreams A/B/C.

**Opus round adds:**

| Tier | Count | Items |
|---|---|---|
| 🔴 CRITICAL | 3 | O1 (migration corrupt), O2 (NameError os import), O3 (TDE hardcoded fallback) |
| 🟠 HIGH | 9 | O4-O8 (open from Codex still not done), O9-O12 (Opus-new) |
| 🟡 MEDIUM | 7 | O13-O19 |
| 🟢 LOW | 2 | O20-O22 |

**3 CRITICAL bugs introduced or surviving since Codex review** — none caught by current proof harness because:
- O1: existing DB has table from prior migration
- O2: scripts never re-run after C1 edit
- O3: hardcoded path still resolves to working creds in dev env

**Counter-finding (the most important meta-observation):** the proof harness as a gate is INSUFFICIENT for catching ship-blockers. Both proofs green doesn't mean the migration works on a fresh install, the EE scripts can run, or the secret-hygiene fix actually removed all secrets.

**Recommended additions to acceptance gate for v6.0:**

1. Fresh-install test: `docker run` a brand-new Db2 container, apply migration from `0001_core_schema.sql` template, assert all 17 tables present.
2. Import-time test: `python -c "import scripts.oracle_ee_pgq_proof; import scripts.oracle_ee_duality_proof"` — must not raise.
3. Grep test: `grep -rE "mnemos_dev|192.168.207|Welcome1Wallet" scripts/oracle_ee_*.py` must return 0 matches (all secrets via env).
4. Pool stress test: 100 concurrent acquires with `max_size=8` — assert no `RuntimeError` raised, all 100 eventually succeed (A5 acceptance).

---

## Cross-references

- `docs/codex-adversarial-review-2026-05-20.md` — earlier 33-finding round
- `docs/handoff-opencode-v6.0-ship-blockers-2026-05-20.md` — Workstream A/B/C briefing (incomplete: O1 + O2 + O3 are new regressions, not in the original handoff)
- `mnemos/persistence/db2.py` — current state post-OpenCode mask-arch + Opus NVL-order fix
- `mnemos/persistence/oracle.py` — unchanged, awaiting A6 dialect-aware unique-violation fix
- `db/migrations_db2/0001_core_schema.sql` — **CORRUPT** per O1, immediate fix required
- Latest proofs: removed (see history scrub 2026-05-21).

---

*Opus adversarial round 2026-05-20 ~17:00 EDT. 21 findings. 3 CRITICAL block any v6.0 deploy past the current dev box. Acceptance gate needs new fresh-install + import + secret-grep + pool-stress probes before ship.*
