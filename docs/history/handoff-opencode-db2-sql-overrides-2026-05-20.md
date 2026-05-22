# Handoff to OpenCode — Db2 SQL overrides for 2/6 → 6/6 proof (2026-05-20)

**From:** Claude (Opus 4.7) on STUDIO
**To:** OpenCode / xAI agent picking up Db2 ABC port work
**Branch:** `feat/oracle-port`
**Status:** Container build problem **SOLVED**. Db2 12.1.5 EAP runs end-to-end. Only the SQL-override layer in `mnemos/persistence/db2.py` is incomplete — 4 of 6 proof probes hit ORA-compat shortfalls.

---

## TL;DR

- **Db2 12.1.5 EAP container** is live on PYTHIA :50001 (`db2-eap-test`) — `mnemos/db2-eap:vnext` image, 3.04 GB
- **Migration applies clean** (21/21 statements via `scripts/db2_apply_migration.py`)
- **Cross-host probes from STUDIO** confirm: `DB2 v12.1.5.0`, `NVL` works, `VECTOR(3,FLOAT32)`+`VECTOR_DISTANCE(...,COSINE)` works
- **Proof harness** `scripts/db2_proof_run.py` returns **2/6 passed** — same gap as 12.1.4 GA. Db2 ORA-compat does NOT cover `TIMESTAMP WITH TIME ZONE` casts, `SYSTIMESTAMP`, `:name` binds

Your job: write Db2-specific SQL overrides in `mnemos/persistence/db2.py` to bridge the 4 failing probes to passing.

---

## Connection info

```python
import ibm_db_dbi
conn = ibm_db_dbi.connect(
    "DATABASE=MNEMOS;HOSTNAME=192.168.207.67;PORT=50001;PROTOCOL=TCPIP;UID=db2inst1;PWD=mnemos_dev;",
    "", "",
)
```

Or via the project's pool factory:

```python
from mnemos.persistence.db2 import create_db2_pool
pool = await create_db2_pool("db2://db2inst1:mnemos_dev@192.168.207.67:50001/MNEMOS")
```

**SSH to PYTHIA to inspect container state:**

```bash
ssh jasonperlow@192.168.207.67
sudo docker ps --filter name=db2-eap-test
sudo docker logs db2-eap-test 2>&1 | tail -50
sudo docker exec db2-eap-test su - db2inst1 -c "db2 connect to MNEMOS; db2 ..."
```

---

## Container recipe (working)

```
docker/db2-eap/
  Dockerfile     # split-install (binary at build, instance at runtime)
  response.rsp   # 5 lines, binary-only
  entrypoint.sh  # users + db2icrt -nosharedgroup + db2start + CREATE DATABASE
```

**Key recipe points (so you don't regress them):**

1. `SHELL ["/bin/bash", "-c"]` — `/bin/sh` defaults to dash on Ubuntu and db2 scripts use `typeset` (ksh built-in).
2. `ln -sf /bin/bash /bin/sh` in same RUN — defensive, in case any sub-exec doesn't honour SHELL.
3. apt deps:
   `bash binutils ca-certificates file ksh libaio1 libaio1:i386 libcurl4 libcurl4:i386 libnsl2 libnsl2:i386 libnuma1 libnuma1:i386 libpam0g libpam0g:i386 libstdc++6 libstdc++6:i386 libxml2 libxml2:i386 locales passwd procps sudo tzdata`
   **All three i386 of libnuma1/libpam0g/libstdc++6 are mandatory** — db2prereqcheck looks for the 32-bit variants.
4. `response.rsp` has NO `INSTANCE = DB2_INST` block. db2setup at build-time only installs binaries.
5. `entrypoint.sh` does `db2icrt -nosharedgroup -u db2fenc1 db2inst1` at runtime when the hostname is stable.
6. Build command needs `--add-host buildkitsandbox:127.0.0.1` (db2prereqcheck calls `getent hosts $(hostname)`).
7. Run command needs `--privileged=true --ulimit memlock=-1:-1 --shm-size=2g`.

Saved image tarball lives on PYTHIA at `/data/pythia/db2-eap/mnemos-db2-eap-vnext.tar.gz` (3.4 GB compressed). `docker load -i <tarball>` to bring up on another host.

---

## The 4 failing probes + exact errors

Run baseline before you start:

```bash
.venv/bin/python scripts/db2_proof_run.py \
  --dsn 'db2://db2inst1:mnemos_dev@192.168.207.67:50001/MNEMOS'
```

Current result:

| Probe | Result | Root cause |
|---|---|---|
| `memory.gather_stats` | pass | — |
| `memory.list_memories` | **fail** | `SQL0313N` — `:name` bind count vs `?` placeholder mismatch when SQL hits ibm_db_dbi |
| `memory.insert+update+delete` | **fail** | `SQL0104N "WITH TIME ZONE"` — Db2 ORA-compat does NOT support `CAST(... AS TIMESTAMP WITH TIME ZONE)` |
| `memory.semantic_search` | **fail** | Same `WITH TIME ZONE` cast issue |
| `state.set_get_delete` | **fail** | `SQL0206N SYSTIMESTAMP` — Db2 ORA-compat does NOT bind `SYSTIMESTAMP` as a function; use `CURRENT TIMESTAMP` |
| `backend.transactional` | pass | — |

Exact error texts:

```
memory.list_memories     : SQL0313N number of variables in EXECUTE not equal to values required
                           → :name binds need conversion to ? positional binds for ibm_db_dbi
memory.insert+update+delete: SQL0104N unexpected token "WITH TIME ZONE" after "created AS TIMESTAMP"
                            → drop "WITH TIME ZONE" from CAST(... AS TIMESTAMP) clauses
memory.semantic_search   : same SQL0104N WITH TIME ZONE
state.set_get_delete     : SQL0206N "SYSTIMESTAMP" not valid
                           → SYSTIMESTAMP → CURRENT TIMESTAMP
```

---

## Where to add overrides

`mnemos/persistence/db2.py` currently has thin `pass` subclasses:

```python
class Db2MemoryRepository(OracleMemoryRepository):
    """Memory repo — Oracle SQL works under Db2 ORA-compat.

    Currently identical to Oracle. Override sites if 12.1.5 DiskANN
    needs different ORDER BY syntax for the semantic_search rank
    expression.
    """
class Db2KGRepository(OracleKGRepository): pass
class Db2VersionRepository(OracleVersionRepository): pass
class Db2BranchRepository(OracleBranchRepository): pass
class Db2CompressionRepository(OracleCompressionRepository): pass
class Db2StateRepository(OracleStateRepository): pass
class Db2FederationRepository(OracleFederationRepository): pass
class Db2ConsultationAuditRepository(OracleConsultationAuditRepository): pass
class Db2WebhookRepository(OracleWebhookRepository): pass
```

You need to override specific methods in `Db2MemoryRepository` and `Db2StateRepository` so the SQL emitted hits Db2-friendly forms.

**Strategy:**

1. **Don't fork the whole Oracle method.** Override at the SQL-template level: introduce a class-level dict of SQL fragments, override the dict in Db2 subclasses, and have the base Oracle methods consume those fragments. The Oracle repo classes in `mnemos/persistence/oracle.py` may need light refactoring to read from `self._sql` instead of inline `f"..."` SQL.

2. **Substitutions you'll need everywhere in Db2 forks:**
   - `CAST(? AS TIMESTAMP WITH TIME ZONE)` → `CAST(? AS TIMESTAMP)` *(drop the zone — Db2 ORA-compat doesn't bind it)*
   - `SYSTIMESTAMP` → `CURRENT TIMESTAMP`
   - `SYSDATE` → `CURRENT DATE`
   - `NVL(CAST(:p AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP)` → `NVL(CAST(? AS TIMESTAMP), CURRENT TIMESTAMP)` *(plus positional bind conversion)*
   - `:name` placeholders → `?` *(Db2's CLI driver uses positional binds only; `ibm_db_dbi` does NOT translate Oracle `:name` style)*

3. **Don't introduce a regex translator** — fragile. Use a per-method override sites pattern.

4. **Use the existing `_Db2AsyncCursor`** wrapper in `mnemos/persistence/db2.py` (lines 91-117) — its `execute(sql, params)` already wraps `ibm_db_dbi.cursor.execute` in `asyncio.to_thread`. Don't redo the async glue.

---

## Verification loop

After each Db2 SQL change:

```bash
# Rerun proof, expecting more probes to pass each iteration
.venv/bin/python scripts/db2_proof_run.py \
  --dsn 'db2://db2inst1:mnemos_dev@192.168.207.67:50001/MNEMOS'
```

Look at <archived bench artifact> — `evidence.probes` array tells you which probes pass / fail and the exact SQL error.

**Don't merge changes that regress Oracle probes.** After Db2 work, also run the Oracle proof to confirm you haven't broken the Oracle path:

```bash
.venv/bin/python scripts/oracle_proof_run.py \
  --dsn 'oracle://MNEMOS:mnemos_dev@192.168.207.25:1521/ORCLPDB1'
```

Should remain at 13/13.

---

## Working-tree state when this was written

```
On branch feat/oracle-port

Staged:
  M mnemos/persistence/db2.py          # cleanup from earlier OpenCode work, async wrappers + repo subclasses, parses cleanly

Modified (unstaged):
  M docs/db2-port-handoff.md            # earlier handoff doc, supersede with this one
  M docker/db2-eap/Dockerfile           # working R12 recipe
  M docker/db2-eap/entrypoint.sh        # with -nosharedgroup + correct license filenames
  M docker/db2-eap/response.rsp         # 5-line binary-only
  ?? <archived bench artifact>  # signed 2/6 baseline
  ?? docs/handoff-opencode-db2-sql-overrides-2026-05-20.md  # this file
```

Recommended commit sequence when SQL overrides done:

```bash
git add docker/db2-eap/ docs/db2-port-handoff.md docs/handoff-opencode-db2-sql-overrides-2026-05-20.md
git commit -m "feat(db2): EAP 12.1.5 container split-install recipe working

* Binary install at build, db2icrt -nosharedgroup at entrypoint runtime
* libnuma1:i386 + libcurl4:i386 + libpam0g:i386 prereqs
* SHELL [/bin/bash] to defeat dash typeset issue
* --add-host buildkitsandbox:127.0.0.1 at build time
* Image 3.04GB, MNEMOS database created with ORA-compat + VECTOR + VECTOR_DISTANCE COSINE working"

# Then a second commit for the SQL-override work once 6/6 lands:
git add mnemos/persistence/db2.py <archived bench artifact>
git commit -m "feat(db2): SQL overrides for ORA-compat gaps — 6/6 proof

* Override TIMESTAMP WITH TIME ZONE casts in MemoryRepository
* Override SYSTIMESTAMP → CURRENT TIMESTAMP in StateRepository
* Convert :name binds → ? positional binds for ibm_db_dbi
* All 6 proof probes pass against Db2 12.1.5 EAP"
```

---

## Out-of-scope (do not touch)

- `mnemos/persistence/oracle.py` — Oracle works at 13/13, leave alone
- `db/migrations_oracle/0001_core_schema.sql` — Oracle migration is 48-statement clean
- `db/migrations_db2/0001_core_schema.sql` — already applies 21/21 statements clean on EAP
- `docker/db2-eap/` files — recipe is working, leave alone
- DB2 12.1.4 GA container `pythia-db2` on PYTHIA :50000 — fallback, don't touch

---

## Cross-references

- Codex's R9 split-install reasoning (build-time vs runtime instance creation): `docs/db2-port-handoff.md` (R9 section)
- Oracle EE 23.26 proof artifacts for shape reference: <archived bench artifact>
- Handoff to ncz-claude on OpenClaw container missing auth profile: `~/.claude/rules/handoff-to-ncz-claude-openclaw-auth-profile-2026-05-08.md`

---

*Handoff written 2026-05-20 ~13:10 EDT from STUDIO Claude session after 12 build attempts cracked the EAP container. Container path now stable; SQL overrides next.*
