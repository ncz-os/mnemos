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

---

# REVISION 2026-06-02 — transport = cloud-object relay (E2EE), .4 eliminated

GRAEAE re-consult (8 muses, winner gemini, cost $0) on transport + on the embedding-dim question. Two changes supersede the original .4-SSH-spooling design:

## A. Mnemos: FEDERATE (dims now match)
The 768-dim was a deployment choice, not a constraint. Rebuild the Spark's mnemos to current master with **bge-m3 1024-dim** (GB10 GPU) → the dim-mismatch that forced "isolate" is gone → the Spark mnemos becomes an **asynchronous bridged federation peer** (like ACHILLES/MEDUSA), queried by the home fleet through the relay. **Stay Postgres/pgvector ARM64** (Storage ABC supports it; don't add Oracle to an edge node). **Re-embed in place** (don't restart): `pg_dump` → `ADD COLUMN embedding_1024 vector(1024)` → batch re-embed each row's stored text via bge-m3 on the GB10 → switch config → drop the old 768-dim column. LLM-Wiki text preserved.

## B. Transport: cloud-object relay with E2EE (NOT .4 SSH, NOT Drive, NOT NGC)
Both Spark and home reach the internet; only the home LAN is unreachable from the Spark. So route the bridge through a **cloud object store (GCS `ifGenerationMatch=0` or S3 `If-None-Match: *`)** with **client-side AES-GCM E2EE** (shared symmetric key; the cloud sees only ciphertext → private MNEMOS context never exposed). This **beats the .4 SSH-spool on reliability** (.4 was a single point of failure; the bucket is 99.99% and decouples both fleets — jobs queue safely if either side is down) while matching its security (E2EE) and atomicity (conditional writes give exactly-once claim). **Google Drive rejected** (eventual consistency / weak locking). **NGC rejected** (inference+registry, no queue — message-broker-on-registry is an anti-pattern).

**`.4` is eliminated:** PYTHIA reaches the internet, so PYTHIA writes encrypted job objects to `prefix/pending/<uuid>.json.enc` directly; the Spark polls `pending/`, **claims via atomic conditional-write** of `claimed/<uuid>` (loser backs off), runs NGC qwen3-coder-480b, pushes code to GitHub, writes `results/<uuid>.json.enc`; PYTHIA polls `results/`, decrypts, reconciles to the hive + ARGONAS fan-out. Two stateless pollers, one bucket, no on-prem bridge host.

## Build sequence (revised)
1. Provision the bucket (S3 — fleet has AWS creds — or GCS) + the shared E2EE key (in ~/.api_keys_master.json + each endpoint, never in the bucket).
2. Shared `relay_crypto.py` (AES-GCM seal/open) + `relay_client.py` (put/list/claim-conditional/get).
3. PYTHIA enqueuer: nvidia-eligible job → context-prepackage (home Oracle bge-m3 retrieval) → seal → put pending/.
4. Spark poller: list pending/ → conditional-claim → NGC exec → push GitHub → seal result → put results/.
5. PYTHIA reconciler: poll results/ → open → PATCH hive done + ARGONAS fan-out.
6. Smoke: target=spark-ngc job round-trips through the bucket.

---

# AS BUILT 2026-06-02 — `spark_relay/` (GCS, codex-approved)

Implemented in [`../spark_relay/`](../spark_relay/) on GCS. Deviations from the
sketch above, all hardening surfaced by adversarial review:

- **GCS, not S3.** Project `spark-hive-relay`, bucket `gs://spark-hive-relay-bkt`
  (us-east1, uniform access, public-access-prevention, lifecycle delete >7d). SA
  `relay-rw` with `roles/storage.objectAdmin`. Atomic claim = `ifGenerationMatch=0`.
- **Single `terminal/<uuid>` object**, not split `results/`+`failed/`. One
  create-only object is the exactly-once terminal gate (status in the sealed
  payload) so two workers can never record conflicting done/failed for one job.
- **Leased claims.** `claimed/<uuid>` holds `{owner, claimed_at}`; a claim older
  than `DEFAULT_LEASE_SECONDS` (2h) is taken over via generation CAS (loaded with
  `get_blob`, bails if no generation). A dead worker no longer strands a job; an
  empty/unparseable marker is treated as expired.
- **AAD binding.** Every blob is sealed with `aad_for(kind, uuid)` (kind ∈
  pending/terminal) so a ciphertext can't be replayed across prefix or job.
- **Quarantine + purge-after-ack.** Undecryptable/uuid-mismatch pending → durable
  `terminal` failure (claim never stranded). Reconciler purges ONLY after the
  hive PATCH returns success — never deletes evidence the hive hasn't acked.

Bucket prefixes as built: `pending/<uuid>.json.enc`, `claimed/<uuid>`,
`terminal/<uuid>.json.enc`.
