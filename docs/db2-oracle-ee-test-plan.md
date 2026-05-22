# MNEMOS v6.0 — Db2 12.1 + Oracle Database 26ai Enterprise Feature Test Plan

**Goal:** Prove + sign HMAC artifacts for every enterprise feature MNEMOS uses or depends on, across both backends.

**Audience:** Larry Ellison / IBM CEO inbox · v6.0 blog ammunition · operational deployment confidence.

**Status reference:** [PROVEN] = capability verified [PENDING] = test plan written below.

---

## 0. Already proven

| # | Feature | Oracle | Db2 | Artifact |
|---|---|---|---|---|
| - | Baseline 13 ABC probes | [PROVEN] 13/13 | [PROVEN] 2/6 (gap = SQL overrides) | |
| - | VECTOR datatype + VECTOR_DISTANCE COSINE | [PROVEN] | [PROVEN] | both |
| 4 | HNSW VECTOR index | [PROVEN] 4.46x p50 speedup, 20K rows | [PENDING] (DiskANN — EAP 12.1.5+) | |
| 5 | JSON Relational Duality View | [PROVEN] 6/6 | [PENDING] (Db2 lacks Duality, has JSON_TABLE) | |
| 6 | Property Graph SQL/PGQ | [PROVEN] 6/6 | [PENDING] (RDF Graph in Db2, different semantics) | |
| 3 | TDE on USERS tablespace | [PROVEN] AES256 KV1 | [PENDING] (native encryption in Db2) | |

---

## 1. Feature matrix — Oracle EE ↔ Db2 AESE pairs

For each pair, MNEMOS use case + priority.

| Capability area | Oracle EE | Db2 AESE | MNEMOS use | Pri |
|---|---|---|---|---|
| **HA / DR replication** | Data Guard physical standby | HADR (sync/async) | federation primary↔standby for v6 HA story | **P0** |
| **Read-replica offload** | Active Data Guard | HADR ROS (Read on Standby) | scale read load on shared memory backend | **P0** |
| **TDE / encryption at rest** | TDE tablespace AES256 | NATIVE_ENCRYPTION (`db2 update db cfg ... ENCRLIB`) | "memories encrypted at rest" | **P0** |
| **Vector ANN index** | HNSW INMEMORY NEIGHBOR GRAPH | DiskANN (12.1.5 GA) / nearest neighbor scan (12.1.4) | semantic_search perf | **P0** |
| **Partitioning** | range/list/hash/composite | range/multi-dimensional clustering (MDC) | partition MEMORIES by owner_id or created_at | P1 |
| **In-memory columnar** | Database In-Memory | BLU Acceleration | analytical scans on mem stats | P1 |
| **Row-level security** | VPD / Label Security | RCAC (Row + Column Access Control) | per-owner visibility filter | P1 |
| **Audit** | Unified Audit + Audit Vault | DB2 AUDIT facility + Audit Policies | compliance posture | P2 |
| **Temporal / time travel** | Flashback Query / Database | System-Period Temporal Tables (SYSTEM_TIME) | rollback bad agent writes | P2 |
| **Online schema redefinition** | DBMS_REDEFINITION | ADMIN_MOVE_TABLE | zero-downtime DDL during v6.x→v6.y upgrades | P2 |
| **Compression** | HCC + OLTP | Adaptive Compression + Index Compression | storage cost | P2 |
| **Workload management** | Resource Manager | WLM (Workload Manager) | foreground vs background priority | P3 |
| **Replication / CDC** | GoldenGate | Q Replication / InfoSphere CDC | cross-backend sync | P3 |
| **JSON columns** | native (since 21c) | JSON datatype + JSON_TABLE | sidecar metadata | covered by Duality on Oracle |
| **Native sharding** | Globally Distributed Database | DPF (Database Partitioning Feature) | multi-region — N/A for v6.0 | skip |
| **Multi-node cluster** | RAC | pureScale | N/A for v6.0 — single-node container | skip |
| **Graph (RDF)** | RDF Knowledge Graph | RDF Triple Store + SPARQL | KG layer alternative | skip (P6/PGQ covers) |
| **OLAP cubes** | Analytic Workspace | OLAP Server (deprecated) | N/A — MNEMOS not OLAP-shaped | skip |

---

## 2. Oracle EE — per-feature test plan

### 2.1 EE#1 Data Guard physical standby [PENDING] — P0

**Goal:** oracle-host primary ↔ gpu-host standby. Redo transport + MRP apply. Failover proven.

**Setup:**
1. oracle-host primary already at 23.26 EE. Enable: `FORCE LOGGING`, `ARCHIVELOG` mode, `db_unique_name=oracle-host_PRI`, `log_archive_dest_1='LOCATION=USE_DB_RECOVERY_FILE_DEST'`, `log_archive_dest_2='SERVICE=gpu-host_STBY ASYNC'`, `fal_server='gpu-host_STBY'`, `log_archive_config='DG_CONFIG=(oracle-host_PRI,gpu-host_STBY)'`, `standby_file_management=AUTO`, `dg_broker_start=TRUE`.
2. gpu-host: start `mnemos-oracle-ee-standby` container with `oradata` volume mounted but empty. Listener config with static reg for `gpu-host_STBY_DGMGRL`.
3. Copy SYS password file from oracle-host → gpu-host oradata.
4. RMAN duplicate: `DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE NOFILENAMECHECK;`
5. Set up Data Guard broker config: `CREATE CONFIGURATION mnemos_dg AS PRIMARY DATABASE IS 'oracle-host_PRI' CONNECT IDENTIFIER IS 'oracle-host_PRI'; ADD DATABASE 'gpu-host_STBY' AS CONNECT IDENTIFIER IS 'gpu-host_STBY' MAINTAINED AS PHYSICAL; ENABLE CONFIGURATION;`

**Probes:**
- INSERT 1000 rows on primary
- Wait ≤30s → SELECT same rows on standby (should match via MRP apply)
- Switchover: `SWITCHOVER TO gpu-host_STBY` → gpu-host becomes primary
- Switch back
- Failover: kill primary container → `FAILOVER TO gpu-host_STBY` → gpu-host opens for writes
- Reinstate: bring primary back, `REINSTATE DATABASE oracle-host_PRI` → resync via reverse log shipping



**Pass criteria:** MRP lag ≤30s p50, switchover RTO ≤2min, failover RTO ≤5min, reinstate succeeds without RMAN duplicate.

**Effort:** 60-90 min.

---

### 2.2 EE#2 Active Data Guard read-only [PENDING] — P0

**Goal:** Standby answers `SELECT` while primary takes writes.

**Setup (depends on 2.1):** `ALTER DATABASE OPEN READ ONLY` on standby. MRP keeps applying redo in-place (real-time apply).

**Probes:**
- Primary INSERT memory rows
- Standby `SELECT COUNT(*) FROM memories` reflects new rows within MRP lag window
- Cobol-style mnemos read load: run `scripts/oracle_proof_run.py` read-only probes against standby DSN



**Pass criteria:** Standby returns query results, MRP lag stays bounded under continuous read load.

**Effort:** 5 min once 2.1 done.

---

### 2.3 EE#7 Partitioning [PENDING] — P1

**Goal:** Partition `memories` by `owner_id` (LIST) — show 10x scan perf on owner-scoped queries.

**Setup:**
- Migrate `memories` to partitioned form via `DBMS_REDEFINITION` (online):
  ```sql
  CREATE TABLE memories_part (...) PARTITION BY LIST (owner_id) AUTOMATIC (PARTITION p_default VALUES (DEFAULT));
  DBMS_REDEFINITION.START_REDEF_TABLE(...);
  DBMS_REDEFINITION.COPY_TABLE_DEPENDENTS(...);
  DBMS_REDEFINITION.FINISH_REDEF_TABLE(...);
  ```

**Probes:**
- Bench: `SELECT * FROM memories WHERE owner_id = 'X' AND created_at > ...` — partition-pruned vs full-table scan
- Verify partition list grows automatically with new owner_ids



**Pass criteria:** ≥5x speedup at 100K rows / 100 owners, online redef completes without lock.

**Effort:** 45 min.

---

### 2.4 EE Database In-Memory [PENDING] — P1

**Goal:** Mark `memories` in-memory → analytic query speedup.

**Setup:**
- `ALTER SYSTEM SET inmemory_size=2G SCOPE=SPFILE;` (restart)
- `ALTER TABLE memories INMEMORY PRIORITY HIGH;`
- Wait for population (`V$IM_SEGMENTS`)

**Probes:**
- Bench: `SELECT category, COUNT(*), AVG(quality_rating) FROM memories GROUP BY category` row vs columnar
- `SELECT /*+ FULL(m) */` vs `SELECT /*+ INMEMORY(m) */`



**Pass criteria:** ≥10x on `GROUP BY category` aggregation at 100K rows.

**Effort:** 30 min.

---

### 2.5 EE Row-level Security (VPD) [PENDING] — P1

**Goal:** Per-owner visibility policy.

**Setup:**
- Create context: `CREATE CONTEXT mnemos_ctx USING mnemos_owner_pkg`
- Policy fn: `DBMS_RLS.ADD_POLICY('MNEMOS', 'MEMORIES', 'owner_filter', 'mnemos_owner_pkg', 'filter_predicate')`
- Filter returns `owner_id = SYS_CONTEXT('mnemos_ctx', 'current_owner')`

**Probes:**
- Set context to `owner_A`, query `memories` — only owner_A rows visible
- Switch to `owner_B`, query — only owner_B rows visible
- Without context: 0 rows
- Same logic enforced at JOIN time (no leak via memory_versions)



**Pass criteria:** zero cross-owner leak, perf impact ≤10% vs no-policy baseline.

**Effort:** 45 min.

---

### 2.6 EE Unified Audit [PENDING] — P2

**Goal:** Audit policy capturing all DML on `memories`.

**Setup:** `CREATE AUDIT POLICY mnemos_audit ACTIONS INSERT, UPDATE, DELETE ON MNEMOS.MEMORIES; AUDIT POLICY mnemos_audit;`

**Probes:**
- Run INSERT/UPDATE/DELETE
- `SELECT * FROM unified_audit_trail WHERE object_name='MEMORIES'` shows captured events with timestamp, user, action



**Effort:** 20 min.

---

### 2.7 EE Flashback Query [PENDING] — P2

**Goal:** Recover deleted memories via AS OF query.

**Setup:** `UNDO_RETENTION=900` minimum.

**Probes:**
- INSERT row, COMMIT, capture SCN
- DELETE row, COMMIT
- `SELECT * FROM memories AS OF SCN <captured_scn> WHERE id=...` → row visible
- `INSERT INTO memories SELECT * FROM memories AS OF SCN <scn> WHERE id=...` → restore



**Effort:** 15 min.

---

### 2.8 EE Advanced Compression [PENDING] — P2

**Goal:** OLTP compression on `memories`. Storage ratio.

**Setup:** `ALTER TABLE memories MOVE COMPRESS FOR OLTP;` (online via DBMS_REDEFINITION).

**Probes:**
- Pre-compress segment size vs post-compress
- INSERT 10K new rows, compression auto-applied
- Bench: read/write perf with vs without compression



**Effort:** 25 min.

---

### 2.9 EE AWR + ADDM report [PENDING] — P2

**Goal:** Generate AWR report covering the proof harness window. Use ADDM to find bottlenecks.

**Setup:** `EXEC DBMS_WORKLOAD_REPOSITORY.CREATE_SNAPSHOT();` before + after run.

**Probes:**
- Run 13-probe + HNSW + Duality + PGQ + TDE workload
- `SELECT report_html FROM TABLE(DBMS_WORKLOAD_REPOSITORY.AWR_REPORT_HTML(<dbid>, <inst>, <snap1>, <snap2>))`
- ADDM finding extraction

**Artifact:** `<future-bench-output>` + signed JSON metadata.

**Effort:** 20 min.

**Bonus narrative:** "Oracle's own perf-analysis machinery on the MNEMOS workload."

---

### 2.10 EE Globally Distributed Database / Sharding — SKIP for v6.0

Too heavy for single-tenant memory backend. Revisit at scale.

---

## 3. Db2 AESE — per-feature test plan

### 3.1 Db2 HADR (High Availability Disaster Recovery) [PENDING] — P0

**Goal:** Db2 primary ↔ standby with redo log shipping. Db2's Data Guard equivalent.

**Setup:**
1. pg-host primary (existing `db2-eap-test`).
2. Second EAP container on gpu-host — same image, fresh DB instance.
3. On primary:
   ```sql
   UPDATE DB CFG FOR MNEMOS USING HADR_DB_ROLE PRIMARY
                                    HADR_LOCAL_HOST pythia
                                    HADR_LOCAL_SVC  60000
                                    HADR_REMOTE_HOST cerberus
                                    HADR_REMOTE_SVC  60000
                                    HADR_REMOTE_INST db2inst1
                                    HADR_SYNCMODE SYNC
                                    HADR_PEER_WINDOW 120;
   ```
4. On standby:
   ```sql
   db2 restore db MNEMOS from <backup-image> taken at <timestamp>;
   db2 update db cfg ... HADR_DB_ROLE STANDBY ...
   db2 start hadr on db MNEMOS as standby;
   ```
5. Primary: `db2 start hadr on db MNEMOS as primary;`

**Probes:**
- `db2pd -db MNEMOS -hadr` shows PEER state on both sides
- INSERT rows on primary → query on standby (READ ON STANDBY mode required)
- Takeover: `db2 takeover hadr on db MNEMOS;` → standby becomes primary
- Failback symmetric

**Artifact:** `<future-bench-output>` — sync state, takeover RTO.

**Pass criteria:** `PEER` state achieved, takeover ≤30s, no data loss in SYNC mode.

**Effort:** 60-90 min.

---

### 3.2 Db2 Read on Standby (ROS) [PENDING] — P0

**Goal:** Db2's Active Data Guard equivalent. Standby answers reads while primary takes writes.

**Setup (depends on 3.1):** Set `DB2_HADR_ROS=ON` registry on standby. `db2set DB2_HADR_ROS=ON; db2stop force; db2start;` on standby.

**Probes:**
- Read MNEMOS schema from standby via ibm_db_dbi on port 60001
- Run a subset of proof harness read probes against standby DSN

**Artifact:** `<future-bench-output>`.

**Effort:** 10 min once 3.1 done.

---

### 3.3 Db2 Native Encryption (TDE equivalent) [PENDING] — P0

**Goal:** Db2 native encryption at rest.

**Setup:**
```bash
# Generate master key keystore
gsk8capicmd_64 -keydb -create -db "/database/keystore/mnemos.p12" -pw "Welcome1Wallet!" -type pkcs12 -stash
db2 update dbm cfg using KEYSTORE_TYPE PKCS12 KEYSTORE_LOCATION /database/keystore/mnemos.p12
db2 backup db MNEMOS encrypt encrlib libdb2encr.so encropts "Master Key Label=mnemos_mk"
db2 restore db MNEMOS from <enc-backup> encrlib libdb2encr.so encropts "..."
```
Or create with encryption from scratch:
```sql
db2 "CREATE DATABASE MNEMOS_ENC ENCRYPT CIPHER AES KEY LENGTH 256 MASTER KEY LABEL mnemos_mk"
```

**Probes:**
- `db2 get db cfg for MNEMOS_ENC | grep -i encrypt` → shows encryption ON
- Insert rows + dump page via `db2dart /DD` → ciphertext, not plaintext
- Query rows via app → plaintext (transparent)

**Artifact:** `<future-bench-output>` — algorithm, key label, ciphertext sample.

**Effort:** 30 min.

---

### 3.4 Db2 DiskANN VECTOR INDEX (12.1.5 EAP feature) [PENDING] — P0

**Goal:** Db2's HNSW-equivalent ANN index. EAP-only — this is the killer reason to be on EAP not GA.

**Setup:**
```sql
CREATE INDEX idx_memories_embed ON memories (embedding) ORGANIZE BY DISKANN
  (DISTANCE_METRIC COSINE, MAX_NEIGHBORS 64, EF_CONSTRUCTION 200);
```

**Probes:**
- Seed 20K rows with 384-dim embeddings (same shape as Oracle HNSW bench)
- Bench: top-K cosine similarity scan no-idx vs DiskANN-idx
- Expected speedup similar to Oracle HNSW (4-5x p50)

**Artifact:** `<future-bench-output>` — speedup ratio.

**Pass criteria:** ≥3x p50 speedup at 20K rows.

**Effort:** 30 min.

**Critical:** if DiskANN syntax not yet in EAP build, test what 12.1.5 ships — `TO_EMBEDDING` SQL function or `TEXT_GENERATION` (LLM-in-DB) may also be available. Inspect what 12.1.5 actually exposes via `SELECT * FROM SYSCAT.FUNCTIONS WHERE funcname LIKE '%VECTOR%' OR funcname LIKE '%EMBED%' OR funcname LIKE '%GENERAT%'`.

---

### 3.5 Db2 BLU Acceleration [PENDING] — P1

**Goal:** Column-organized table for analytic scans. Db2's Database In-Memory equivalent.

**Setup:**
```sql
db2set DB2_WORKLOAD=ANALYTICS  # auto-sizes BUFFERPOOL + SORTHEAP for BLU
CREATE TABLE memories_blu (LIKE memories) ORGANIZE BY COLUMN;
INSERT INTO memories_blu SELECT * FROM memories;
```

**Probes:**
- Bench: `SELECT category, COUNT(*), AVG(quality_rating) FROM memories_blu GROUP BY category` vs row-org
- Compression ratio of column-org segment

**Artifact:** `<future-bench-output>`.

**Pass criteria:** ≥5x on aggregation, ≥3x compression vs row-org.

**Effort:** 30 min.

---

### 3.6 Db2 Range Partitioning [PENDING] — P1

**Goal:** Partition `memories` by `created` (RANGE). Show pruning + DETACH for archival.

**Setup:**
```sql
CREATE TABLE memories_part (LIKE memories)
  PARTITION BY RANGE (created)
  (PARTITION p_2024 STARTING ('2024-01-01') ENDING ('2025-01-01') EXCLUSIVE,
   PARTITION p_2025 STARTING ('2025-01-01') ENDING ('2026-01-01') EXCLUSIVE,
   PARTITION p_2026 STARTING ('2026-01-01') ENDING ('2027-01-01') EXCLUSIVE);
```

**Probes:**
- Insert spread across years
- Query with `WHERE created BETWEEN ... AND ...` — partition elimination in explain plan
- `ALTER TABLE memories_part DETACH PARTITION p_2024 INTO archive_memories_2024` → old data moved to archive table in seconds

**Artifact:** `<future-bench-output>`.

**Effort:** 30 min.

---

### 3.7 Db2 Row + Column Access Control (RCAC) [PENDING] — P1

**Goal:** Per-owner row-level security.

**Setup:**
```sql
CREATE PERMISSION owner_filter ON memories FOR ROWS
  WHERE owner_id = SESSION_USER OR VERIFY_GROUP_FOR_USER(SESSION_USER, 'MNEMOS_ADMIN') = 1
  ENFORCED FOR ALL ACCESS ENABLE;
ALTER TABLE memories ACTIVATE ROW ACCESS CONTROL;
```

**Probes:** Identical matrix to Oracle 2.5.

**Artifact:** `<future-bench-output>`.

**Effort:** 30 min.

---

### 3.8 Db2 System-Period Temporal Tables (Flashback equivalent) [PENDING] — P2

**Goal:** Time travel queries.

**Setup:**
```sql
ALTER TABLE memories ADD COLUMN sys_start TIMESTAMP(12) NOT NULL GENERATED ALWAYS AS ROW BEGIN;
ALTER TABLE memories ADD COLUMN sys_end TIMESTAMP(12) NOT NULL GENERATED ALWAYS AS ROW END;
ALTER TABLE memories ADD COLUMN trans_start TIMESTAMP(12) GENERATED ALWAYS AS TRANSACTION START ID;
ALTER TABLE memories ADD PERIOD SYSTEM_TIME (sys_start, sys_end);
CREATE TABLE memories_history LIKE memories;
ALTER TABLE memories ADD VERSIONING USE HISTORY TABLE memories_history;
```

**Probes:**
- INSERT, capture timestamp T1
- UPDATE → row moves to history
- `SELECT * FROM memories FOR SYSTEM_TIME AS OF T1` → original row visible
- `SELECT * FROM memories FOR SYSTEM_TIME BETWEEN T1 AND CURRENT TIMESTAMP` → full change list

**Artifact:** `<future-bench-output>`.

**Effort:** 25 min.

---

### 3.9 Db2 ADMIN_MOVE_TABLE (online redefinition) [PENDING] — P2

**Goal:** Zero-downtime schema change.

**Setup:** Call `CALL SYSPROC.ADMIN_MOVE_TABLE(...)` with new schema definition while concurrent INSERTs run.

**Probes:**
- 1000 INSERTs/sec background load
- Trigger online move (e.g., add a column)
- Verify zero failed transactions during the move

**Artifact:** `<future-bench-output>`.

**Effort:** 30 min.

---

### 3.10 Db2 Adaptive Compression [PENDING] — P2

```sql
ALTER TABLE memories COMPRESS YES ADAPTIVE;
REORG TABLE memories;
```

Probes + artifact identical shape to Oracle 2.8.

**Effort:** 20 min.

---

### 3.11 Db2 Audit Facility [PENDING] — P2

**Goal:** Auditable DML stream on memories table.

**Setup:**
```sql
CREATE AUDIT POLICY mnemos_audit CATEGORIES OBJMAINT STATUS BOTH, EXECUTE STATUS SUCCESS ERROR DATA WITH CONTEXT;
AUDIT TABLE memories USING POLICY mnemos_audit;
```

Flush + extract: `db2audit flush; db2audit extract category execute file /tmp/audit.xml`

**Artifact:** `<future-bench-output>`.

**Effort:** 25 min.

---

### 3.12 Db2 Workload Manager [PENDING] — P3

**Goal:** Background distillation vs foreground query priority.

**Setup:**
```sql
CREATE SERVICE CLASS background_class;
CREATE WORKLOAD bg_workload SESSION_USER GROUP ('MNEMOS_BACKGROUND') SERVICE CLASS background_class;
ALTER SERVICE CLASS background_class AGENT PRIORITY LOW;
```

**Probes:** Concurrent foreground + background queries; verify foreground latency ≤2x baseline even under background load.

**Artifact:** `<future-bench-output>`.

**Effort:** 40 min.

---

### 3.13 Db2 INT8 vector quantization [PENDING] — P2

**Goal:** Test if Db2 12.1.5 EAP supports `VECTOR(<dim>, INT8)` or only FLOAT32. Quantization shrinks memory ~4x at modest recall cost.

**Setup:**
```sql
CREATE TABLE vec_int8 (id INTEGER, embedding VECTOR(384, INT8));
INSERT INTO vec_int8 ... ;
SELECT VECTOR_DISTANCE(embedding, ?, COSINE) FROM vec_int8 ORDER BY 1 FETCH FIRST 10 ROWS ONLY;
```

**Probes:**
- DDL succeeds (or fails — captures EAP support state)
- Compare bench p50 vs FLOAT32 baseline at 20K rows
- Recall@10 vs FLOAT32 ground truth

**Artifact:** `<future-bench-output>`.

**Effort:** 25 min.

---

### 2.11 Oracle INT8 + binary vector quantization [PENDING] — P2

**Goal:** Oracle Database 26ai supports `VECTOR(*, INT8)` + binary vectors. Test both.

**Setup:**
```sql
CREATE TABLE vec_int8 (id NUMBER, embedding VECTOR(384, INT8));
CREATE TABLE vec_bit (id NUMBER, embedding VECTOR(384, BINARY));
```

**Probes:** Same shape as 3.13 — DDL + bench + recall@10.

**Artifact:** `<future-bench-output>` — INT8 + binary side-by-side with FLOAT32 baseline.

**Effort:** 30 min.

---

## 4. Cross-cutting tests

### 4.1 Federation cross-backend pull [PENDING] — P0

**Goal:** PG primary → Oracle EE secondary → Db2 EAP tertiary all converge on same 5,846 memory rows via federation HA.

**Setup:**
- PG primary populated (re-seed from saved dump)
- Oracle EE peer pulls
- Db2 EAP peer pulls

**Probes:**
- All three backends report identical `SELECT COUNT(*), SUM(LENGTH(content)) FROM memories`
- Content hash match per memory_id

**Artifact:** `<future-bench-output>` — row-count + hash convergence proof.

**Effort:** depends on Db2 SQL overrides (handoff to OpenCode)

---

### 4.2 Equal-hardware perf bench [PENDING] — P1

**Goal:** Side-by-side benchmark on pg-host Meteor Lake hardware (Oracle vs PG vs Db2) for 13-probe workload + HNSW.

**Setup:** Already have `scripts/oracle_vs_postgres_bench.py`. Add Db2 leg.

**Artifact:** `<future-bench-output>`.

**Effort:** 45 min once Db2 6/6 lands.

---

### 4.3 GPU-accelerated embed throughput on gpu-host [PENDING] — P2

**Goal:** Run `scripts/embed_throughput_bench.py` on RTX 4500 ADA. Quote embeddings/sec for v6.0 blog.

**Effort:** 20 min, already scripted.

---

### 4.5 Resilience layer dependency reduction — replace Redis [PENDING] — P2

**Goal:** Prove MNEMOS resilience primitives (rate-limit, circuit breaker, concurrency limiter) survive without Redis by moving onto NATS JetStream KV OR Postgres UNLOGGED tables. Reduces customer-deployment infra footprint by 1 service.

**Why:** at ~100-200 ops/sec resilience load, sub-ms Redis latency is overkill. NATS already runs in fleet for federation — consolidating on one bus eliminates Redis as a deploy dep.

**Setup — Path C (NATS KV, preferred):**
```python
# replace RedisRateLimiterPool with NatsKVRateLimiter
# nats KV bucket with TTL + atomic INCR via subject store
import nats
js = nats.JetStream(nc)
kv = await js.create_key_value(bucket="mnemos-ratelimit", ttl=60)
# atomic increment via CAS loop on integer-typed value
```

**Setup — Path B (Postgres UNLOGGED, alternative):**
```sql
CREATE UNLOGGED TABLE rate_limit_counters (
    key TEXT PRIMARY KEY,
    count INT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_rl_expires ON rate_limit_counters (expires_at) WHERE expires_at > NOW();
-- atomic check+incr via UPDATE ... RETURNING; bg DELETE job every 10s
```

**Probes:**
- 1000 concurrent rate-limit checks, all 3 backends (Redis baseline / NATS / Postgres-unlogged): p50, p95, p99 latency
- Multi-worker correctness: 4 workers × 100 checks → no drift, total counter matches
- Circuit-breaker TTL-expiry semantics: open state auto-clears after window in all 3 backends
- LISTEN/NOTIFY (PG) and NATS watch broadcast: invalidation propagation latency

**Artifact:** `<future-bench-output>` — 3-way perf + correctness matrix.

**Pass criteria:** all 3 backends produce identical counter values under load; NATS/PG p99 ≤ 5ms.

**Effort:** 2-3 days (real refactor, not a one-off probe). Defer to v6.1 unless customer-deploy ask for Redis-free pipeline.

---

### 4.6 Native concurrency / runaway-query control [PENDING] — P2

**Goal:** Wire Oracle DBMS_RESOURCE_MANAGER + Db2 WLM as the per-query concurrency gate, with Redis only handling per-API-request rate-limit.

**Setup Oracle:**
```sql
EXEC DBMS_RESOURCE_MANAGER.CREATE_PLAN(PLAN => 'MNEMOS_PLAN', COMMENT => 'two-tier');
EXEC DBMS_RESOURCE_MANAGER.CREATE_CONSUMER_GROUP(CONSUMER_GROUP => 'BG_DISTILL', COMMENT => 'background distillation worker');
EXEC DBMS_RESOURCE_MANAGER.CREATE_PLAN_DIRECTIVE(
    PLAN => 'MNEMOS_PLAN',
    GROUP_OR_SUBPLAN => 'BG_DISTILL',
    MGMT_P1 => 10,                       -- 10% CPU
    SWITCH_TIME => 60,                   -- 60s max query
    SWITCH_GROUP => 'CANCEL_SQL');       -- runaway killer
```

**Setup Db2:**
```sql
CREATE SERVICE CLASS bg_distill;
ALTER SERVICE CLASS bg_distill AGENT PRIORITY LOW;
CREATE THRESHOLD bg_runaway FOR SERVICE CLASS bg_distill ACTIVITIES
  ENFORCEMENT DATABASE
  WHEN ACTIVITYTOTALTIME > 60 SECONDS STOP EXECUTION;
```

**Probes:**
- Inject runaway 5-min query under BG service class → killed at 60s, audit trail captured
- Foreground query latency stays stable while background load runs

**Artifact:** `<future-bench-output>`.

**Effort:** 45 min.

---

### 4.4 MCP-over-SSE proof on Db2 backend [PENDING] — P1

**Goal:** Same mcp server lifecycle.py serves MNEMOS over Db2.

**Setup:** Set `MNEMOS_DSN=db2://...` env, restart server, run 21 MCP tools.

**Artifact:** `<future-bench-output>` — identical tool surface as Oracle.

**Effort:** 30 min once Db2 6/6 lands.

---

## 5. Priority + effort summary

| Pri | Item | Backend | Effort | Cum |
|---|---|---|---|---|
| **P0** | 2.1 Data Guard primary↔standby | Ora | 90 min | 1.5h |
| **P0** | 2.2 Active Data Guard | Ora | 10 min | 1.7h |
| **P0** | 3.1 HADR | Db2 | 90 min | 3.2h |
| **P0** | 3.2 ROS | Db2 | 10 min | 3.4h |
| **P0** | 3.3 Db2 Native Encryption | Db2 | 30 min | 3.9h |
| **P0** | 3.4 Db2 DiskANN | Db2 | 30 min | 4.4h |
| **P0** | 4.1 Federation cross-backend | both | 45 min | 5.2h |
| **P1** | 2.3 Oracle partitioning | Ora | 45 min | 6.0h |
| **P1** | 2.4 Database In-Memory | Ora | 30 min | 6.5h |
| **P1** | 2.5 VPD row-level security | Ora | 45 min | 7.2h |
| **P1** | 3.5 Db2 BLU | Db2 | 30 min | 7.7h |
| **P1** | 3.6 Db2 partitioning | Db2 | 30 min | 8.2h |
| **P1** | 3.7 Db2 RCAC | Db2 | 30 min | 8.7h |
| **P1** | 4.2 Equal-hw 3-way bench | all | 45 min | 9.5h |
| **P1** | 4.4 MCP-on-Db2 | Db2 | 30 min | 10.0h |
| **P2** | 2.6 Unified Audit | Ora | 20 min | |
| **P2** | 2.7 Flashback | Ora | 15 min | |
| **P2** | 2.8 Compression | Ora | 25 min | |
| **P2** | 2.9 AWR + ADDM | Ora | 20 min | |
| **P2** | 3.8 Temporal tables | Db2 | 25 min | |
| **P2** | 3.9 ADMIN_MOVE_TABLE | Db2 | 30 min | |
| **P2** | 3.10 Adaptive Compression | Db2 | 20 min | |
| **P2** | 3.11 Audit Facility | Db2 | 25 min | |
| **P2** | 3.13 Db2 INT8 vector | Db2 | 25 min | |
| **P2** | 2.11 Oracle INT8 + binary vector | Ora | 30 min | |
| **P2** | 4.3 GPU embed bench | gpu-host | 20 min | |
| **P3** | 3.12 WLM | Db2 | 40 min | |

**P0 total: 5.2h. P0+P1: 10h.** Realistic 2-day sprint with parallelism (Oracle DG setup on oracle-host/gpu-host while Db2 SQL overrides land in parallel from OpenCode).

---

## 6. Artifact contract

Every proof emits HMAC-signed JSON with:

```json
{
  "evidence": {
    "schema": "mnemos-<backend>-<feature>/v1",
    "run_id": "<12-hex>",
    "started_utc": "...",
    "finished_utc": "...",
    "db_version": "...",
    "host": "<ip>",
    "feature_specific_fields": "...",
    "probes": [
      {"name": "...", "outcome": "pass|fail", "evidence": {...}, "error": "..."}
    ]
  },
  "hmac_key_id": "<16-hex of sha256(key)>",
  "hmac_sha256": "<64-hex>"
}
```

HMAC key: `b"mnemos-oracle-proof-v1"` (already in use; same for Db2).

Validator script `scripts/verify_proof.py` reads any artifact + re-computes HMAC + checks integrity.

---

## 7. Skipped items + rationale

| Skipped | Why |
|---|---|
| Oracle Globally Distributed Database / Sharding | Single-tenant memory backend — N/A at v6.0 |
| Oracle RAC | Single-node container architecture |
| Db2 pureScale | Same — clustering not part of v6.0 deployment |
| Db2 DPF | Same |
| Oracle Database Vault | Stronger than RLS but operational overhead too high for v6.0 |
| Db2 Q Replication | Federation HA via mnemos pull covers this need |
| Oracle GoldenGate | Same |
| OLAP / Analytic Workspace | MNEMOS workload not OLAP-shaped |
| RDF Triple Store (Db2) | PGQ Property Graph on Oracle covers KG use case |
| Spatial extender | No spatial data in MNEMOS |
| XML Type / XQuery | JSON Duality covers structured doc need |

---

## 8. Cross-references

- DB2 EAP recipe: `docs/db2-eap-recipe-2026-05-20.md`
- OpenCode SQL-override handoff: `docs/handoff-opencode-db2-sql-overrides-2026-05-20.md`
- nas-host backups: `/mnt/argonas/datapool/projects/container-backups/`
- Test scripts: `scripts/oracle_ee_*.py` (HNSW, Duality, PGQ, TDE templates to copy)

---

*Test plan written 2026-05-20. Update as features land. Each [PENDING] → [PROVEN] gets a signed artifact + table-row flip.*
