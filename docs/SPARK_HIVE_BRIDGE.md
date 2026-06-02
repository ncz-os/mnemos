# Spark Hive Bridge — network-isolated GB10 worker integration

**Decided:** 2026-06-02 · GRAEAE consult (8 muses, winner gemini, cost $0) + operator refinement · directive #2 (GRAEAE-first) + #10 (triple-persist).
**Target:** integrate the NVIDIA DGX Spark `spark-0c53` (GB10, host-locked NGC models qwen3-coder-480b + nemotron) as a hive worker for `nvidia`-eligible jobs.

## Hard constraint
The Spark (`10.110.25.164`, NVIDIA corp net) **cannot reach the home fleet** (192.168.207.x — PYTHIA hive bus `:5005`, ARGONAS, MNEMOS). It **can** reach GitHub/internal GitLab. Host **`.4`** (jperlow-mlt) bridges both: home LAN (192.168.207.4 → PYTHIA) + corp GlobalProtect VPN (→ Spark). `.4 → Spark` SSH works; `Spark → .4` may not. So **`.4` is the active orchestrator** — the Spark never initiates to home.

## Architecture: Bridge-Driven Spooling (`.4` = Bridge Agent)

```
PYTHIA hive (:5005, Oracle)  ──claim(lease)──>  .4 Bridge Agent  ──scp job.json──>  Spark /var/spool/hive/pending/
        ^                                          | bridge_state.db (SQLite WAL)         | inotify daemon: atomic mv -> processing/
        |                                          |                                       | run on GPU (NGC qwen3-coder-480b/nemotron)
        └──POST /jobs/<id>/complete────────────────┘ <──ssh pull result.json── /completed/  | write completed/job_<uuid>_result.json
                                                                                            v
   GIT: Spark commits locally ──> .4 rsyncs commits ──> .4 commits to ARGONAS (home canonical)   [+ Spark->GitHub for public repos]
                                                          ARGONAS fan-out by commit_sha
```

### (a) Transport — SSH-spooling
`.4` pushes `job_<uuid>.json` into the Spark's `/var/spool/hive/{pending,processing,completed,failed}/` via scp; a local Spark daemon (inotify) watches `pending/`. `.4` polls `completed/` over ssh, pulls result JSON back, deletes from Spark. No inbound path to home is ever opened.

### (b) Atomic claim — lease semantics + filesystem atomicity
Jobs destined for the Spark carry routing tag `target: spark-ngc` (eligible_hosts includes the Spark; nvidia host-lock). `.4` claims on PYTHIA: `status=CLAIMED, worker=bridge-4, lease_expires_at=NOW()+2h` (prevents a home worker also taking it). Idempotency key = job UUID = filename. Spark daemon does atomic `mv pending/→processing/`; if the job already exists in `processing/` or `completed/`, it **ignores the duplicate** (double-run guard).

### (c) Result + git flow (operator-refined)
Spark executes, commits locally, writes `completed/job_<uuid>_result.json` = `{status, commit_sha, branch, metrics}`. **`.4` rsyncs the Spark's commits and commits them to ARGONAS** (home canonical — keeps home repos off public GitHub); ARGONAS fans out by `commit_sha`. (GRAEAE alt for public-OSS repos: Spark pushes the branch straight to GitHub, ARGONAS pulls by sha — heavy payload bypasses home net.) `.4` then `POST /jobs/<uuid>/complete` to PYTHIA.

### (d) Failure / recovery
- **Spark offline** → `.4` SSH fails → jobs stay safe in `bridge_state.db` + PYTHIA; exponential-backoff retry.
- **`.4` offline** → PYTHIA `lease_expires_at` fires → job reverts to PENDING; `.4` re-claims on recovery.
- **Stuck on Spark** (OOM/crash) → daemon times a `processing/` job out → `failed/`; `.4` reports failure to PYTHIA.
- **Partial sync / double-run** → `.4` SQLite WAL resumes from last state (`CLAIMED_FROM_PYTHIA → PUSHED_TO_SPARK → PULLED_FROM_SPARK → SYNCED_TO_PYTHIA`); Spark dedups by UUID.

### (e) Relay store — purpose-built, NOT an Oracle mirror
`bridge_state.db` on `.4` is a thin state-machine / write-ahead log, not a copy of `hive_jobs`:
```sql
CREATE TABLE relay_jobs (
  job_uuid TEXT PRIMARY KEY,
  pythia_payload TEXT,
  state TEXT,                 -- CLAIMED_FROM_PYTHIA | PUSHED_TO_SPARK | PULLED_FROM_SPARK | SYNCED_TO_PYTHIA
  spark_result_payload TEXT,
  updated_at TIMESTAMP
);
```

### (f) MNEMOS mismatch → ISOLATE + Context Pre-Packaging
Spark runs a stale MNEMOS 6.0.0rc1 Postgres with **768-dim nomic** embeddings; the fleet is Oracle 23ai **1024-dim bge-m3** — incompatible. **Decision: do NOT federate, do NOT upgrade the Spark's mnemos** (avoid operational risk on host-locked corp hardware); **disable the Spark's local mnemos daemon.** Instead, **retrieval happens at home**: before queuing, the home fleet queries Oracle MNEMOS (1024-dim) for relevant context and **injects snippets into the job payload `context` array**; the Spark is stateless and feeds the pre-packaged context straight into the qwen3/nemotron prompt window.

> **Operator note vs earlier "bring Spark mnemos up to date":** GRAEAE recommends ISOLATE-and-bypass over upgrade. The Spark's own MNEMOS-CUDA stays as the LLM-Wiki store; it is NOT part of the hive path. Revisit only if the Spark needs independent semantic retrieval.

## Build sequence (each gated on adversarial review, directive 4)
1. Spark spool daemon (`/var/spool/hive/*`, inotify, atomic mv, dedup, NGC-model exec, result JSON, stuck-timeout).
2. `.4` Bridge Agent (claim-lease on PYTHIA, `bridge_state.db` state machine, scp push, ssh pull, rsync→ARGONAS, `/complete` reconcile, backoff).
3. PYTHIA: `target=spark-ngc` claim path + lease columns + home-side context pre-packaging on enqueue.
4. End-to-end smoke: a `target=spark-ngc` job → Spark qwen3-coder-480b → commit → rsync→ARGONAS → PYTHIA done.
