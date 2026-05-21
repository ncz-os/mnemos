# Handoff to OpenCode — v6.0 ship-blockers (13 HIGH findings)

**Source:** Codex adversarial review 2026-05-20 (`docs/codex-adversarial-review-2026-05-20.md`)
**Branch:** `feat/oracle-port`
**Baseline:** Oracle 13/13 + Db2 12/12 proofs green. 3 CRITICAL bugs already fixed in this session.
**Effort:** ~2-3 days total. Three independent workstreams — landable in parallel.

---

## Workstream A — SQL translation layer correctness (6 items, ~1 day)

The translation layer in `mnemos/persistence/db2.py` does blind `str.replace` and `re.sub` against the full SQL string. Both ignore the fact that SQL contains string literals and comments. Today's tests don't catch this because no Oracle SQL emitter happens to embed problematic substrings — but federation-pulled content, user-supplied data, and any future SQL change can corrupt at write time.

### A1. `_ORA_TO_DB2_PAIRS` rewrites tokens inside SQL literals + comments

**File:** `mnemos/persistence/db2.py:112` (the loop `for oracle_tok, db2_tok in _ORA_TO_DB2_PAIRS: adapted = adapted.replace(oracle_tok, db2_tok)`)

**Problem:** User content containing the strings `TIMESTAMP WITH TIME ZONE`, `SYSTIMESTAMP`, `SYSDATE`, or `TO_VECTOR` (in a `?` bound string, in an `--comment`, or in a `'literal'`) gets rewritten when the SQL is parsed for translation. Currently invisible because Oracle's repo SQL emitters don't embed user content as literals, but `set_state` / `insert_kg_triple` and federation pulls send arbitrary bytes through `:value` binds.

**Fix:** Two acceptable forms:
- **Tokenizer:** small SQL-aware scanner that yields `(kind, span)` tuples for keywords / identifiers / literals / comments. Apply translations only to keyword spans.
- **Rope-style protection:** before translation, mask `'...'` literals + `--...\n` comments + `/* ... */` blocks by replacing with `\x00<idx>\x00`, run replacements, restore masked spans.

The mask approach is ~30 lines + zero new deps. Prefer it.

**Verification:**
- Add unit test `tests/test_db2_translation_string_safety.py` that feeds `INSERT INTO state (value) VALUES (:v)` with `v = "SYSTIMESTAMP demo"` through the adapter, asserts the bound positional value still equals `"SYSTIMESTAMP demo"`.
- Add test for `-- SYSTIMESTAMP` comment preservation.
- Both proofs must still pass.

### A2. `_BIND_RE` scans `:name` inside SQL string literals → false-positive KeyError

**File:** `mnemos/persistence/db2.py:126`

**Problem:** Same root cause as A1. If any SQL contains a string like `'pattern_with_:colon_inside'`, the regex grabs `:colon_inside` as a required bind and raises `KeyError` from `params[name]`.

**Fix:** Bind extraction must run on a literal-stripped SQL view. Reuse the mask from A1.

### A3. `TO_VECTOR` substring rewrite false-positive

**File:** `mnemos/persistence/db2.py:53` (`_ORA_TO_DB2_PAIRS`)

**Problem:** `TO_VECTOR` → `VECTOR` is a plain string replace. Future SQL using `TO_VECTOR_DISTANCE` or similar identifier will be mangled to `VECTOR_DISTANCE` (incorrect).

**Fix:** Tighten to a word-boundary regex: `re.compile(r'\bTO_VECTOR\b')`. Apply ONLY in the keyword-span pass (depends on A1 tokenizer).

### A4. Pool `_lock` held during physical connect

**File:** `mnemos/persistence/db2.py:235`

**Problem:** `async with self._lock: ... conn = await self._open()` — connection opening (slow, blocking-IO under `to_thread`) happens while the lock is held. Other coroutines trying to release connections back to the pool block on the same lock. Under burst load this serializes all acquisitions.

**Fix:**
```python
async def acquire(self):
    async with self._lock:
        if self._idle:
            conn = self._idle.pop()
        elif len(self._in_use) < self._max_size:
            slot_reserved = True  # increment in-use counter under lock
            self._in_use_count += 1
        else:
            # See A5 — wait on Condition, not raise
            await self._wait_for_release()
            return await self.acquire()
    if not conn:  # we reserved a slot, open outside the lock
        conn = await self._open()
    async with self._lock:
        self._in_use.add(conn)
    ...
```

Use a separate `_in_use_count` int to reserve without yet having a connection object.

### A5. Pool exhaustion raises immediately instead of waiting

**File:** `mnemos/persistence/db2.py:237` (`raise RuntimeError("DB2 pool exhausted")`)

**Problem:** Every well-behaved async pool (asyncpg, oracledb, redis-py) waits with timeout on exhaustion. Db2 pool just throws → caller sees 503 under burst load that would have succeeded with 100ms wait.

**Fix:** Replace the raise with an `asyncio.Condition` wait. Match oracledb's behavior:

```python
async def acquire(self, timeout: float = 30.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        async with self._lock:
            if self._idle:
                ...
                return conn
            if len(self._in_use) + self._reserving < self._max_size:
                self._reserving += 1
                break  # open outside lock
        async with self._not_full:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError("Db2 pool exhausted (timeout)")
            await asyncio.wait_for(self._not_full.wait(), timeout=remaining)
    # ... open + register conn
```

On release: `self._not_full.notify()`.

### A6. Unique-violation handling Oracle-only

**Files:** `mnemos/persistence/oracle.py:107` (`_is_unique_violation`) + `oracle.py:2324` (`insert_federated_memory`)

**Problem:** Helper checks only for `ORA-00001`. Db2 raises SQLSTATE `23505` (and emits `SQL0803N`) for the same condition. Federation pulls onto Db2 that hit duplicate `(owner_id, namespace, content_hash)` will raise instead of silently dedupe.

**Fix:** Make `_is_unique_violation` dialect-aware:

```python
def _is_unique_violation(exc, dialect: str = "oracle") -> bool:
    msg = str(exc)
    if dialect == "oracle":
        return "ORA-00001" in msg
    elif dialect == "db2":
        return "SQLSTATE=23505" in msg or "SQL0803N" in msg
    return False
```

Pass `self._dialect` from the repository class. Db2 repos override `self._dialect = "db2"` via the mixin (revive `_Db2OraCompatMixin` for this single purpose instead of deleting it per L1).

**Verification:** Add proof probe that inserts the same federated memory twice and asserts the second call returns False instead of raising. Add to both `oracle_proof_run.py` and `db2_proof_run.py`.

---

## Workstream B — Schema parity + Db2 migration framework (3 items, ~1 day)

### B1. Db2 `embedding VECTOR(768, FLOAT32)` is hardcoded

**File:** `db/migrations_db2/0001_core_schema.sql:49`

**Problem:** Deployments using 384-dim (nomic-embed-text), 1536-dim (OpenAI text-embedding-3-small), or 3072-dim (text-embedding-3-large) embeddings fail at write time on Db2 because Db2's `VECTOR(<dim>, FLOAT32)` enforces declared dimension at insert.

Oracle's `VECTOR(*, FLOAT32)` is dimension-agnostic which is why this hasn't bitten Oracle.

**Fix paths:**

| Option | Approach | Pro | Con |
|---|---|---|---|
| **B1a** | Template the SQL with `{{embedding_dim}}` placeholder; `db2_apply_migration.py` substitutes from `settings.embedding_dim` | One migration file, dim picked at apply time | Requires migration runner enhancement |
| **B1b** | Ship 4 migration files (`0001_core_schema_384.sql`, `_768.sql`, `_1536.sql`, `_3072.sql`); apply script picks one | Simplest, no runtime template | 4-way maintenance burden |
| **B1c** | Use `VECTOR(0, FLOAT32)` for unbounded if Db2 12.1.5 EAP supports it (Oracle equivalent) | Single dim-free file | EAP-only; not portable to 12.1.4 GA |

**Recommended:** B1a. Plumb `embedding_dim` into `db2_apply_migration.py` via `--dim 768` flag, default from `settings.embedding_dim`. Template substitution is one f-string.

### B2. Db2 migration not actually idempotent

**File:** `db/migrations_db2/0001_core_schema.sql:10` (comment claim) vs raw `CREATE TABLE` body

**Problem:** Comment says "wrapped for idempotency" but the file emits unwrapped `CREATE TABLE`. Re-apply gets SQL0601N (object exists). Current `db2_apply_migration.py` masks this by treating SQLSTATE `42710` as benign — but that hides actual semantic conflicts AND prevents column additions/removals.

**Fix:** Two parts:

1. **DDL guards:** wrap each `CREATE TABLE` in a PL/SQL-style anonymous block (Db2 supports compound SQL) that catches SQL0601N + treats it as a signal to run `ALTER TABLE ADD COLUMN IF NOT EXISTS` for any new columns vs existing schema:
   ```sql
   BEGIN
     EXECUTE IMMEDIATE 'CREATE TABLE memories (...) ';
   EXCEPTION
     WHEN SQLSTATE '42710' THEN NULL;
   END@
   ```

2. **Real migration framework (deferred to v6.1):** versioned migration table tracking applied SHAs. For v6.0, the guarded-CREATE approach is sufficient.

**Tighten apply script (M12):** Stop treating SQLSTATE `42704` (undefined name) as benign — that masks real broken dependencies. Only allow `42710` for create-replay.

### B3. Db2 schema missing 5 Oracle tables

**File:** `db/migrations_db2/0001_core_schema.sql:19`

**Problem:** Db2 schema omits `users`, `sessions`, `session_messages`, `deletion_log`, `compression_manifest`. Routes that hit any of these (session API, deletion ledger, compression admin) fail on Db2 with `SQL0204N TABLE_DOES_NOT_EXIST`. Federation export/import + GDPR-delete trail break completely.

**Fix:** Port the 5 tables from `db/migrations_oracle/0001_core_schema.sql`. Translation rules:
- `TIMESTAMP WITH TIME ZONE` → `TIMESTAMP`
- `SYSTIMESTAMP` → `CURRENT TIMESTAMP`
- `SYSDATE` → `CURRENT DATE`
- `NUMBER` → `DECIMAL(38,0)` (or `INTEGER` / `BIGINT` depending on use)
- `VARCHAR2(N)` → keep as-is (Db2 ORA-compat handles)
- `CLOB` → keep as-is

Add the migration **idempotently** per B2's pattern.

**Verification:** Re-run `db2_apply_migration.py` → assert 21 + 5 + new_indexes statements all OK.

Also fix M11: add `extracted_by_run_id VARCHAR2(100)` column to Db2 `kg_triples` for schema parity.

Also fix M10: port the dropped secondary indexes (`memory_versions`, webhook, compression, federation) — document any deliberate omissions with a `-- DB2: SKIPPED because X` comment.

---

## Workstream C — EE proof + secret hygiene (4 items, ~half-day)

### C1. EE proof HMAC keys hardcoded in source

**Files:**
- `scripts/oracle_ee_hnsw_bench.py:36` (`HMAC_KEY = b"mnemos-oracle-proof-v1"`)
- `scripts/oracle_ee_duality_proof.py:27` (same)
- `scripts/oracle_ee_pgq_proof.py:30` (same)
- `scripts/oracle_ee_tde_proof.py:23` (same)
- `scripts/oracle_proof_run.py` (look for HMAC_KEY)
- `scripts/db2_proof_run.py` (look for HMAC_KEY)

**Problem:** Anyone with read access to the repo can forge a "signed" proof artifact identical in shape to what we send to Larry Ellison / IBM CEO. Signature value adds zero authentication.

**Fix:** Required env var:

```python
HMAC_KEY = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
if not HMAC_KEY:
    sys.stderr.write("ERROR: MNEMOS_PROOF_HMAC_KEY env var required for signed artifacts.\n")
    sys.exit(2)
HMAC_KEY = HMAC_KEY.encode()
```

Generate the key once:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Store in `~/.api_keys_master.json` under `mnemos.proof_hmac_key` per the global-policy directive #8. Add to `~/.zshrc`:

```bash
export MNEMOS_PROOF_HMAC_KEY=$(jq -r .mnemos.proof_hmac_key ~/.api_keys_master.json)
```

Re-run **all** EE proofs with the real key. Old artifacts (HMAC `fab6dc8022fff2bc`) get marked `pre-key-rotation` and excluded from external distribution.

### C2. TDE proof readiness loop always reports pass

**File:** `scripts/oracle_ee_tde_proof.py:100` (the readiness loop `for _ in range(60): ... time.sleep(5)`)

**Problem:** The loop polls for SQL*Plus readiness but `probe("container.restart_ready", True, {"waited_seconds": "<=300"})` records pass unconditionally. If the container never came back, every downstream TDE step still runs against a non-existent service.

**Fix:**

```python
ready = False
for _ in range(60):
    chk = subprocess.run([...], ...)
    if "1" in chk.stdout and "ORA-" not in chk.stdout:
        ready = True
        break
    time.sleep(5)
probe("container.restart_ready", ready, {"ready": ready, "elapsed_seconds": ...})
if not ready:
    return 1  # abort before destructive TDE steps
```

### C3. TDE proof ignores `docker restart` return code

**File:** `scripts/oracle_ee_tde_proof.py:81`

**Problem:** `subprocess.run(["ssh", ..., "sudo docker restart ..."], ...)` does not check `returncode`. If `docker restart` itself failed (container not found, daemon down), the readiness loop will spin pointlessly and then C2's bug compounds.

**Fix:**

```python
result = subprocess.run(["ssh", ..., "sudo docker restart ..."], capture_output=True, text=True, timeout=60)
if result.returncode != 0:
    probe("container.restart", False, {"stderr": result.stderr[:500]}, "docker restart failed")
    return 1
```

### C4. TDE proof hardcodes secrets

**File:** `scripts/oracle_ee_tde_proof.py:23` (`DOCKER_HOST = "<host>"`, `DOCKER_CONTAINER = "mnemos-oracle-ee"`, `WALLET_PWD = "Welcome1Wallet!"`)

**Problem:** Host IP, SSH user, container name, SYS password, wallet password all baked into source. Anyone with repo access has full DB control. Wallet password specifically is THE key to the master encryption key.

**Fix:** All five must come from env or CLI args:

```python
parser.add_argument("--host", default=os.environ.get("MNEMOS_TDE_HOST"))
parser.add_argument("--container", default=os.environ.get("MNEMOS_TDE_CONTAINER", "mnemos-oracle-ee"))
parser.add_argument("--sys-pwd", default=os.environ.get("ORACLE_SYS_PWD"))
parser.add_argument("--wallet-pwd", default=os.environ.get("MNEMOS_TDE_WALLET_PWD"))
parser.add_argument("--ssh-user", default=os.environ.get("SSH_USER", "jasonperlow"))

if not all([args.host, args.sys_pwd, args.wallet_pwd]):
    sys.exit("required env: MNEMOS_TDE_HOST, ORACLE_SYS_PWD, MNEMOS_TDE_WALLET_PWD")
```

**Redact from artifacts:** the proof JSON currently contains `wallet_root` path (OK) but should NOT contain the wallet password or sys password. Audit + strip.

---

## Sequencing + dependencies

| Item | Depends on | Parallel-safe with |
|---|---|---|
| A1 (literal masking) | — | B*, C* |
| A2 (bind extraction) | A1 | B*, C* |
| A3 (TO_VECTOR word boundary) | A1 | B*, C* |
| A4 (pool lock fix) | — | A1/2/3, B*, C* |
| A5 (pool wait) | A4 | B*, C* |
| A6 (unique violation) | — | A*, B*, C* |
| B1 (VECTOR dim template) | — | A*, B2, B3, C* |
| B2 (idempotent DDL) | — | A*, B1, B3, C* |
| B3 (5 missing tables) | B2 (uses same pattern) | A*, B1, C* |
| C1 (HMAC env key) | — | A*, B*, C2/3/4 |
| C2 (TDE readiness) | — | A*, B*, C1/3/4 |
| C3 (TDE restart rc) | C2 (same file) | A*, B*, C1/4 |
| C4 (TDE secrets) | C1 (env pattern) | A*, B*, C2/3 |

**Parallelism:** OpenCode can land A1-A3 + B1 + C1 in one focused day. A4-A5 + B2-B3 + C2-C4 in the second day. Reserve day 3 for proof verification + regression sweep.

---

## Acceptance gates

**Each fix lands with:**
1. Code change in the cited file
2. Test (unit OR proof probe) that fails without the fix and passes with it
3. Updated commit message citing the finding ID (A1, B3, etc.)

**Workstream gate:** all 13 HIGH items resolved → re-run both proofs:

```bash
.venv/bin/python scripts/oracle_proof_run.py --dsn 'oracle://MNEMOS:mnemos_dev@<host>:1521/ORCLPDB1'
.venv/bin/python scripts/db2_proof_run.py --dsn 'db2://db2inst1:mnemos_dev@<host>:50001/MNEMOS'
```

Expected: Oracle 13/13 + Db2 12/12 (or higher if proof harness expanded for federated-duplicate handling in A6).

**Final v6.0 gate:** new test file `tests/test_db2_translation_string_safety.py` covers A1+A2 corner cases.

---

## Cross-references

- `docs/codex-adversarial-review-2026-05-20.md` — full 33-finding triage with severities
- `docs/db2-translation-handoff-2026-05-20.md` — DeepSeek's translation-layer architectural notes
- `docs/db2-eap-recipe-2026-05-20.md` — container recipe + June 6 GA repackage steps
- `docs/v6.1-roadmap.md` — 15 MEDIUM + 4 LOW + extra features for v6.1
- `mnemos/persistence/db2.py` — translation layer (already has C1-C3 fixes from this session: NVL widening + null-cast fold removal)
- `mnemos/persistence/oracle.py` — Oracle backend (no changes needed for A6 except dialect param)
- nas-host mirror: `/mnt/datapool/projects/container-backups/` (all handoff docs synced via root SSH bypass since docker-host still rebooting)

---

## What's NOT in this handoff (out of scope for OpenCode)

| Item | Why excluded |
|---|---|
| 15 MEDIUM findings (M1-M15) | v6.1 backlog — already in `v6.1-roadmap.md` |
| 4 LOW findings (L1-L4) | Nice-to-haves; ship v6.0 without |
| Data Guard / HADR standby setup | v6.1 P0 (5h work, separate sprint) |
| NATS resilience replacement | v6.1 P1 (Gemini-inspired infra cleanup) |
| EE feature scripts DRY (M14) | v6.1 — wait until 6th EE proof exists to amortize the refactor |
| Db2 12.1.5 GA June 6 repackage | Single-day swap when GA tarball ships; recipe in `db2-eap-recipe-*.md` |

---

*Handoff written 2026-05-20 ~16:00 EDT from dev-workstation. 13 HIGH ship-blockers for v6.0 closeout. ~2-3 day focused sprint. Oracle 13/13 + Db2 12/12 baseline preserved across all changes.*
