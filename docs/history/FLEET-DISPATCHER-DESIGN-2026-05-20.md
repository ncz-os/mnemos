# Fleet Job Dispatcher — Design (2026-05-20)

**Author:** Claude (dev-workstation session)
**Trigger:** 2026-05-20 21:49 OOM on docker-host during RiskyEats render — 4
concurrent heavy jobs from ad-hoc cron tabulation collided, kernel
killed render python3 + sshd starved for ~25 min.
**Operator directive:** "We need job re-prioritization with a real
CPU-aware scheduler. Use pg-host for this. Dispatcher should have
visibility of docker-host, gpu-host-2, oracle-host, and gpu-host and be able to use
them for pipelines when available."

---

## Problem Statement

Today every pipeline runs as its own crontab entry on a specific host.
Eight RiskyEats-related cron jobs on docker-host, two on gpu-host-2, plus
sunbiz / TSE / FRI / news / closure-predictor / orsm overlays. The
hosts have no awareness of each other; cron just fires regardless of
load + memory + concurrent siblings.

**Failure modes observed today:**

- docker-host render OOM-killed by kernel at 64 min into a 90 min job because
  gpu-host-2's 19:30 evening cron started writing to the shared NFS
  `/mnt/argonas/datapool/projects/riskyeats/` at the same time
  (`oom_reaper: reaped process 772449 (python3)` confirmed in syslog).
- docker-host load 25 → 46 → 43 (1/5/15min averages), sshd unresponsive for
  ~25 minutes (banner-exchange timeout).
- 3 prior render failures earlier the same day (12:23, 12:41, 16:29 EDT)
  with the same OOM symptom — chronic, not one-off.
- Per-host lockfile shipped in `c6afc49` (today's RiskyEats fix)
  serializes within a host but **does nothing cross-host**.

The fleet has plenty of capacity in aggregate (docker-host 20c/61GB +
gpu-host-2 8c/? + oracle-host 8c/60GB + gpu-host 24c/125GB+GPU). The
oversubscription is purely a coordination failure.

---

## Goals

1. **No host-local crons.** pg-host owns the schedule. All recurring
   work is declared centrally; host crontabs are emptied. (Operator
   directive 2026-05-20: "eliminate all crons; pythia runs scheduler
   and dispatches to a pool.")
2. **Single fleet pool.** docker-host + gpu-host-2 + oracle-host + gpu-host are
   the worker pool. Dispatcher picks the host per job based on
   declared candidates + live load.
3. **docker-host + gpu-host-2 minimum 60-minute separation per exclusive
   group.** No two heavy RiskyEats / FRI / sunbiz runs may start on
   docker-host and gpu-host-2 within 1 hour of each other, regardless of load.
   (Operator directive 2026-05-20: "docker-host and gpu-host-2 should not be
   running within an hour of each other.") Enforced at scheduler-tick
   time via run-history lookup.
4. **Cross-host serialization.** Only one job per `exclusive_group`
   runs in the fleet at a time.
5. **Load-gated dispatch.** A heavy job that lands when target host is
   already saturated waits in queue rather than starting blind.
6. **Per-host capacity policy.** Each host has a declared budget
   (max-concurrent, max-load, min-free-mem). Dispatcher respects it.
7. **Hosted on pg-host** alongside MNEMOS + GRAEAE.
8. **Observable.** Every dispatched job has a run record (start, end,
   exit code, host, peak load, peak mem) queryable for postmortem.

## Non-Goals (Phase 1)

- Real-time preemption (kill a low-pri job to make room for high-pri).
- Container orchestration (no Kubernetes — too much for our 4-host fleet).
- Cross-job dependency graph (run B after A completes). Pure queue +
  capacity is enough for current workload.
- Auto-recovery from killed jobs (no automatic re-queue on OOM-kill;
  operator decision).

---

## Architecture

```
                      ┌─────────────────────────────────┐
                      │  pg-host :5003 fleet-dispatcher  │
                      │  ──────────────────────────────  │
                      │  ┌──────────────┐                │
                      │  │ HTTP REST    │ POST /jobs     │
                      │  │ (Bearer auth)│ GET  /jobs     │
                      │  └──────┬───────┘ POST /telemetry│
                      │         │         GET  /hosts    │
                      │  ┌──────▼───────┐                │
                      │  │ SQLite       │ queue + jobs   │
                      │  │ (WAL)        │ + run history  │
                      │  └──────┬───────┘                │
                      │  ┌──────▼───────┐                │
                      │  │ Scheduler    │ tick every 5s  │
                      │  │ loop         │                │
                      │  └──────┬───────┘                │
                      └─────────┼────────────────────────┘
                                │ ssh exec (via key
                                │ already in fleet auth)
                  ┌─────────────┼─────────────┐
                  │             │             │
              ┌───▼───┐   ┌─────▼───┐   ┌─────▼──┐   ┌────────┐
              │ docker-host │   │ gpu-host-2  │   │oracle-host │   │gpu-host│
              │ agent │   │ agent   │   │ agent  │   │ agent  │
              └───────┘   └─────────┘   └────────┘   └────────┘
```

### Components

**1. Dispatcher service (pg-host `fleet-dispatcher.service`)**

Python 3.13 + FastAPI + SQLAlchemy + APScheduler. Single process,
backed by Postgres (`fleet_dispatcher` schema on pg-host's existing
MNEMOS instance — see Revision R2). Dedicated bearer token, NOT the
MNEMOS one (least-privilege; leaked MNEMOS token must not be able to
trigger `ssh exec` on workers).

**Endpoints:**

```
POST   /jobs              submit a job
GET    /jobs              list jobs (filter by status / host / name)
GET    /jobs/{id}         job detail + log tail
DELETE /jobs/{id}         cancel queued job
POST   /jobs/{id}/cancel  request running job cancellation (SIGTERM)
POST   /telemetry         agent posts host metrics
GET    /hosts             current host states (load/mem/disk/jobs)
GET    /health            liveness
```

**Job model:**

```python
class Job(Base):
    id: str                       # uuid4
    name: str                     # canonical job name, e.g. "riskyeats.pipeline"
    cmd: str                      # bash command to execute
    workdir: str                  # absolute path on target host
    env: dict[str, str]           # env overlay
    candidate_hosts: list[str]    # hosts this job can run on
    required_cpu_cores: int       # capacity request
    required_mem_gb: int          # capacity request
    exclusive_group: str | None   # mutex key (e.g. "riskyeats")
    priority: int                 # 0 = critical, 5 = normal, 9 = batch
    max_runtime_sec: int          # SIGTERM after this; SIGKILL +30s
    submitted_at: datetime
    status: Literal[
        "queued", "dispatched", "running",
        "completed", "failed", "cancelled", "deferred"
    ]
    host: str | None              # set when dispatched
    pid: int | None
    exit_code: int | None
    started_at: datetime | None
    ended_at: datetime | None
    peak_load_1m: float | None    # populated by telemetry during run
    peak_mem_gb: float | None
    log_path: str | None
```

**2. Scheduler loop (tick = 5s)**

```python
while running:
    refresh_host_states()        # from telemetry table; reject if >60s stale
    queued = list_queued_jobs_by_priority_then_age()
    for job in queued:
        if has_exclusive_conflict(job):
            mark_deferred(job, reason="exclusive group busy")
            continue
        host = pick_host(job)
        if host is None:
            mark_deferred(job, reason="no candidate has capacity")
            continue
        dispatch(job, host)
    reap_finished_jobs()
    sleep(5)
```

`pick_host(job)` filters `job.candidate_hosts` to those where:

- telemetry < 60 sec old
- load_1m + projected_load < host.max_load (default 0.8 × cores)
- free_mem_gb > job.required_mem_gb + host.mem_reserve (default 4 GB)
- running_jobs_count < host.max_concurrent (default = cores / 4)
- exclusive_group not held by another running job on any host
- **60-minute docker-host↔gpu-host-2 separation:** if the candidate is docker-host or
  gpu-host-2 AND another worker in the {argos, typhon} pair started a job
  in the same `exclusive_group` within the last 60 minutes, this
  candidate is rejected. (Operator rule 2026-05-20.) The constraint
  ignores `cancelled` and `completed-in-under-5-min` jobs so retries
  of tiny smoke commands don't accidentally lock out the fleet.

Among survivors, pick lowest `load_1m / cores` (most relatively idle).

**3. Host agent (`fleet-dispatcher-agent.service` on each host)**

Tiny systemd unit, posts telemetry every 30s:

```json
POST /telemetry
{
  "host": "argos",
  "ts": "2026-05-20T21:49:43-04:00",
  "load_1m": 4.2, "load_5m": 6.1, "load_15m": 8.3,
  "cores": 20,
  "mem_total_gb": 61, "mem_free_gb": 38,
  "disk_root_free_gb": 280, "disk_data_free_gb": 1840,
  "uptime_sec": 28400
}
```

Bearer auth (one shared fleet token in `/etc/nclawzero/agent-env`).
No execution authority — agent is read-only. Dispatcher ssh-execs
jobs directly using existing fleet-auth keys (no new key
distribution needed; docker-host+gpu-host-2+oracle-host+gpu-host already accept
pg-host's pubkey per fleet-auth-sync.timer).

**4. Execution path**

```
dispatcher → ssh -o BatchMode=yes <host> -- bash -c \
    'cd $workdir && exec env $env $cmd' \
  >  /var/lib/fleet-dispatcher/logs/$job_id.log 2>&1 &
```

PID captured locally on pg-host via `wait`. Cancellation = `ssh <host>
kill -TERM <pid>`. Log file lives on pg-host so postmortem doesn't
require ssh into the target host.

### Capacity policy file (`/etc/fleet-dispatcher/hosts.toml`)

```toml
[argos]
cores              = 20
max_load           = 12.0     # hard ceiling per CLAUDE.md gotcha
mem_reserve_gb     = 8        # leave headroom for sshd + nfs + agents
max_concurrent     = 2        # never run 3 heavy jobs concurrently
exclusive_groups   = ["riskyeats", "sunbiz", "fri", "tse"]

[typhon]
cores              = 8
max_load           = 6.0
mem_reserve_gb     = 8
max_concurrent     = 1        # smaller RAM, more conservative

[proteus]
cores              = 8
max_load           = 6.0
mem_reserve_gb     = 4
max_concurrent     = 2
exclusive_groups   = ["batch"]

[cerberus]
cores              = 24
max_load           = 16.0
mem_reserve_gb     = 16       # GPU work eats CPU too
max_concurrent     = 3
gpu_reserved       = true     # most CPU jobs should avoid by default
```

Per-job placement gating: if a job declares `candidate_hosts=["argos",
"typhon"]` and oracle-host is the only host with capacity, dispatcher
keeps the job queued + emits `deferred` (does NOT relocate).

---

## Phased Delivery

### Phase 1 — Telemetry-only (1-2 days)

- [ ] pg-host daemon stub with `/telemetry` + `/hosts` endpoints + sqlite.
- [ ] Agent script (`/usr/local/sbin/fleet-dispatcher-agent`) +
      systemd unit deployed on docker-host+gpu-host-2+oracle-host+gpu-host via
      fleet-auth-sync's existing apt push mechanism.
- [ ] `/hosts` returns a snapshot per `agent` POST.
- [ ] No execution yet. Just visibility — operator can `curl
      pythia:5003/hosts | jq` and see live load+mem.
- [ ] Telemetry feeds GRAEAE / MNEMOS so historical queries work
      (anomaly detection later).

**Deliverable:** Dashboard view of fleet load. Catches "docker-host load 46"
before sshd timeouts.

### Phase 2 — Manual job submission (3-5 days)

- [ ] `POST /jobs` accepts payload above. Persists to queue.
- [ ] Scheduler loop dispatches when host has capacity.
- [ ] ssh-exec wiring; log capture on pg-host.
- [ ] CLI helper (`fleetctl run --name riskyeats.pipeline --candidate-hosts argos,typhon --priority 5 -- 'bash scripts/cron_publish.sh evening'`).
- [ ] Exclusive group serialization (cross-host mutex).
- [ ] Run-history record per CLAUDE.md "every commit is a learning"
      pattern.

**Deliverable:** Replace ONE pipeline (RiskyEats evening) with a
dispatcher submission. Validate end-to-end. Keep the cron entry as
a fallback safety net.

### Phase 3 — Schedule definitions on pg-host, ALL host crons emptied (1 week)

pg-host owns the schedule. Define recurring jobs in
`/etc/fleet-dispatcher/schedules.toml`:

```toml
[[schedule]]
name              = "riskyeats.morning"
cron              = "0 10 * * *"
cmd               = "bash scripts/cron_publish.sh morning"
workdir           = "/mnt/argonas/datapool/projects/riskyeats"
candidate_hosts   = ["argos", "typhon"]
required_mem_gb   = 24
exclusive_group   = "riskyeats"
priority          = 5
max_runtime_sec   = 7200

[[schedule]]
name              = "riskyeats.evening"
cron              = "0 18 * * *"
cmd               = "bash scripts/cron_publish.sh evening"
workdir           = "/mnt/argonas/datapool/projects/riskyeats"
candidate_hosts   = ["argos", "typhon"]
required_mem_gb   = 24
exclusive_group   = "riskyeats"
priority          = 5
max_runtime_sec   = 7200
```

Dispatcher fires the cron expression on tick, enqueues a job, runs
through Phase-2 placement (load + capacity + exclusive-group +
60min-docker-host-gpu-host-2-separation gates) and dispatches.

- [ ] **Empty `crontab -r`** on docker-host + gpu-host-2 + oracle-host + gpu-host
      once Phase 3 ships. The dispatcher is the only schedule
      authority going forward.
- [ ] Migrate every entry from today's audit:
      - riskyeats.morning / .evening (docker-host 10/18, gpu-host-2 11:30/19:30)
      - riskyeats.fri_publish (M/W/F 11)
      - riskyeats.sunbiz_pipeline (2:30 — 3 jobs serialized)
      - riskyeats.tse_nightly (1:30)
      - riskyeats.closure_predictor / tse_everything / orsm_baseline
        (Mon 11/11:15/11:30 — chain into a 3-step exclusive_group)
      - riskyeats.news_check_daily (2:45)
      - riskyeats.news_check_weekly (Sun 3:30)
      - riskyeats.google_recheck (9:00)
      - riskyeats.backup_to_argonas (1:00)
      - investorclaw.eod_mailer (17:00 weekday)
      - investorclaw.massive_surface (5/18 specific schedule)
- [ ] Document "to schedule something, edit schedules.toml + push"
      pattern in CLAUDE.md.
- [ ] Audit every host's `crontab -l` and `systemctl list-timers`
      monthly to catch drift.

**Deliverable:** `crontab -l` on every fleet host returns empty
output. Zero observed cross-host collisions over 1 week.

### Phase 4 — Smart placement (after Phase 3 stable)

- [ ] NPU/GPU-aware routing: embeddings → cixmini; LLM inference →
      gpu-host; bulk CPU → docker-host/oracle-host; multi-arch build → gpu-host-2.
- [ ] Preemption for priority-0 critical jobs (rare; needs careful
      design to avoid OOM-on-evict).
- [ ] Hybrid local/dispatcher (job is dispatched to a host that then
      uses its OWN cron to schedule sub-steps).

---

## Open Decisions

1. **Submission auth scope.** Same bearer as MNEMOS (simple) or a
   separate dispatcher token (least-privilege)? Recommend separate
   so MNEMOS doesn't trigger arbitrary `ssh exec` if it ever leaks.
2. **Where do dispatcher logs live?** pg-host local disk vs NFS to
   nas-host. pg-host local is faster; nas-host gives durability across
   pg-host reinstall. Recommend nas-host via NFS mount with pg-host-local
   cache.
3. **What happens when dispatcher itself crashes?** SQLite is durable;
   on restart, re-read queue + reconcile running jobs by sshing to
   hosts + checking pid. Define "running" reconciliation rules.
4. **Web UI?** Phase 5 nice-to-have. JSON REST is enough for
   `fleetctl` + `jq` operators today.

---

## Migration Plan for Today's Pain Points

| Today | Phase 3 |
|---|---|
| docker-host `0 10/18 * * *` riskyeats cron | `POST /jobs name=riskyeats.morning|evening priority=5 candidate_hosts=[argos,typhon] exclusive_group=riskyeats` |
| gpu-host-2 `30 11/19 * * *` riskyeats cron | same submission; dispatcher picks host based on load |
| docker-host `0 11 * * 1,3,5` fri_publish | `priority=6 exclusive_group=riskyeats` so it queues behind morning pipeline |
| docker-host `30 2` sunbiz + merge + fic_merge (3 back-to-back) | three jobs with `priority=7 exclusive_group=sunbiz` |
| InvestorClaw EOD mailer 17:00 weekday | small enough to keep on local cron |

After Phase 3, the FAILURE we hit today becomes:

- gpu-host-2 19:30 cron fires → dispatcher queues `riskyeats.evening`
- docker-host still mid-render → exclusive_group=riskyeats blocks → gpu-host-2 job stays queued
- docker-host finishes (or OOMs cleanly with no concurrent siblings) →
  dispatcher releases lock → gpu-host-2 job starts
- Net result: same throughput, zero OOM, zero sshd outage.

---

## Cross-References

- `~/.claude/CLAUDE.md` Gotcha: "NEVER oversubscribe docker-host" — this
  doc operationalizes the rule.
- `~/.claude/rules/fleet-roles-canonical-2026-05-06.md` — host roles
  inform `candidate_hosts` defaults per job.
- RiskyEats `c6afc49` lockfile fix — per-host serialization that
  this dispatcher generalizes to cross-host.
- mnemos-prod-working `mnemos_os.egg-info` — possible sibling for
  the new service.

---

## Revision Round 1 — GRAEAE Consultation 2026-05-20 22:40 EDT

**Consultation:** `195c0198-5fd0-4159-a3d2-e905a34efcff` (pg-host
`/v1/consultations`, mode=majority). 3 muses returned:

| Muse | Verdict | Score |
|---|---|---|
| gemini-3.1-pro-preview | NEEDS REVISION (lean REPLACE) | 0.922 |
| claude-opus-4-6 | APPROVE WITH REVISIONS | 0.877 |
| gpt-5.4 | NEEDS REVISION | 0.808 |

### Consensus Revisions Adopted

**R1. `fleet-exec` shim wraps ssh-exec for Phases 1-3.** All 3 muses
identified `ssh-exec` as the architecturally weakest link. When pg-host
crashes / network partitions / TCP times out, the ssh session severs
but the render python on the worker keeps running — orphaned,
consuming cores + memory, holding the exclusive_group lock invisibly.
This recreates today's OOM under a different trigger.

**Shim contract** (`/usr/local/sbin/fleet-exec` deployed to every
worker by Phase 1 of the rollout):

```bash
fleet-exec --job-id <uuid> --pidfile <path> --statefile <path> \
           --heartbeat-url http://pythia:5003/jobs/<uuid>/heartbeat \
           --max-runtime-sec 7200 \
           -- bash scripts/cron_publish.sh evening
```

- Writes `<pidfile>` atomically (`open O_CREAT|O_EXCL` + rename).
- POSTs heartbeat every 15s with pid + elapsed + exit-status-so-far.
- On signal (SIGTERM from dispatcher → killed worker → its parent
  fleet-exec catches signal), it SIGTERMs the inferior + sweeps
  process group + waits 30s + SIGKILL + posts final state.
- Worker boot-time sweep checks every `<pidfile>` for live pid; if
  dead but no final-state posted, posts `lost` immediately.

Worker daemon migration is deferred to Phase 4 — `fleet-exec` covers
the 90% safety case with 10% of the operational surface.

**R2. Switch SQLite → Postgres on pg-host (operator override
2026-05-20 22:43).** Fleet already runs Postgres on pg-host for MNEMOS
(pgvector backend). Reusing that instance is net-zero operational
cost — one daily backup story, one connection-secret rotation, one
monitoring path. Postgres also unlocks Phase 4 worker daemons that
write directly to the queue from off-pg-host without ssh-tunnel
plumbing.

Settings:

```
dsn               = postgresql://fleet_dispatcher@127.0.0.1:5432/fleet
schema            = fleet_dispatcher
default_isolation = REPEATABLE READ
statement_timeout = 30s
idle_in_xact_timeout = 60s
```

Schema names prefixed `fd_` (`fd_jobs`, `fd_telemetry`,
`fd_planned_schedule`, `fd_audit`) to share the same database as
MNEMOS without table collisions. Use a dedicated role
`fleet_dispatcher` with grants only on the `fleet_dispatcher` schema
— MNEMOS keeps its existing role + schema untouched.

Oracle + DB2 are also fleet-available (per
`mnemos-prod-working/docs/db2-port-handoff.md`) but are reserved for
the MNEMOS port-out work; Postgres is closer-to-hand for the
dispatcher.

Backup: MNEMOS already does `pg_dump` + nas-host-durable archive
nightly — fleet-dispatcher schema rides the same job. Add to the
existing backup script's `--schema fleet_dispatcher` list.

**R3. State machine** — explicit terminal states:

```
queued     - in DB, not yet picked by scheduler tick
planned    - scheduled to fire at a future cron-instant
dispatching- ssh handshake in flight (transient, <10s)
running    - confirmed live via heartbeat
succeeded  - terminal, fleet-exec posted exit_code=0
failed     - terminal, fleet-exec posted exit_code!=0
lost       - terminal, no heartbeat for 3× interval (45s)
cancelled  - terminal, operator-cancelled before/during
```

`lost` is distinct from `failed` because the dispatcher doesn't know
the outcome — the inferior may have completed but pg-host never heard
back (pg-host crash, network partition, worker reboot). Operator must
investigate before clearing the exclusive_group lock manually.

**R4. Planned-schedule artifact.** Persist 24-72 hours of upcoming
fires as `(job_name, planned_fire_time, deterministic_id, status)`
rows. Two consequences:

- On pg-host cold start, replay any `planned` rows in the past with
  configurable catchup policy per job (`policy=catchup_one` vs
  `policy=skip_stale` — see schedule TOML below).
- On worker reboot, the planned-schedule rows are the source of
  truth for "should this slot have run?"

**R5. Telemetry freshness rules.** Placement filter requires
`telemetry.age < 15s` (was 60s in original). After 3 consecutive
missed posts (90s with 30s interval), host transitions to
`UNREACHABLE` — NEVER dispatch to UNREACHABLE. Re-eligible after 2
consecutive fresh posts.

**R6. NFS-health as placement signal.** Today's OOM was triggered by
NFS contention between docker-host render + gpu-host-2 parquet writes to the
same `/mnt/argonas/datapool/projects/riskyeats/` tree, not pure
CPU/mem saturation. New telemetry field:

```json
{
  ...,
  "nfs": {
    "mount":      "/mnt/argonas/datapool",
    "write_p99_ms": 280,
    "read_p99_ms":   45,
    "outstanding":   12,
    "stale_handle_count": 0
  }
}
```

Probe = `dd if=/dev/zero of=/mnt/argonas/datapool/.fleet-probe-<host>
bs=1M count=4 oflag=direct conv=fsync` every 30s. Hosts with
`nfs.write_p99_ms > 500` or `stale_handle_count > 0` are flagged
`NFS_DEGRADED` and lose eligibility for NFS-writing jobs (declared
via per-job `nfs_writer=true` in schedules.toml).

**R7. Clock authority.** pg-host-local `time.monotonic_ns()` for all
interval math. Workers report `heartbeat_age_monotonic_ns` (their
local clock delta since the previous heartbeat); pg-host never trusts
worker wall-clock for scheduling decisions. The 60-min docker-host↔gpu-host-2
separation is computed against pg-host's local dispatch timestamps,
not anything the worker reports.

**R8. Boot-time sweep.** Each telemetry agent, on systemd start,
posts a special bootstrap message:

```
POST /telemetry/bootstrap
{ "host": "argos", "boot_id": "<systemd boot id>",
  "active_pidfiles": [...] }
```

Dispatcher compares `active_pidfiles` to its own running-job table
and reconciles: any row in `running` whose pidfile is missing →
mark `lost`. Catches "worker rebooted mid-job" cleanly.

### Schedule TOML — updated example

```toml
[[job]]
name              = "riskyeats.morning"
cron              = "0 7 * * *"                   # 12min after DBPR
                                                   # publish (mem_1779330980948_043435)
cmd               = "bash scripts/cron_publish.sh morning"
workdir           = "/mnt/argonas/datapool/projects/riskyeats"
candidate_hosts   = ["argos", "typhon"]
required_mem_gb   = 24
exclusive_group   = "riskyeats"
priority          = 5
max_runtime_sec   = 7200
nfs_writer        = true                          # gates on NFS_DEGRADED
catchup_policy    = "skip_stale"                  # don't fire if missed
heartbeat_interval_sec = 15
```

### Stack confirmed (with revisions)

FastAPI + SQLAlchemy + APScheduler + `fleet-exec` shim. Postgres on
pg-host (shared instance, dedicated `fleet_dispatcher` schema). TOML
schedules git-tracked in `gitlab.com/ncz-os/fleet-dispatcher` (new
repo). Read-only REST surface (`GET /schedules`, `GET /hosts`,
`GET /jobs`). Write endpoints only for one-off ad-hoc submissions
(which still must declare exclusive_group + candidate_hosts).

Prefect / Dagster reserved as a Phase 5+ graduation path if fleet
scale crosses 500 jobs/day or workflows become DAG-shaped.

### Open decisions still outstanding

- Repo: new `gitlab.com/ncz-os/fleet-dispatcher` or sibling under
  `mnemos-prod-working/services/dispatcher/`?
- Bearer-token scope: minimal new token, distributed via
  `fleet-auth-sync.timer` (already deploying keys today).
- Web UI for Phase 5: Datasette over the SQLite file is the
  cheapest credible answer; Phase 2 ships REST + jq operator
  pattern.

---

*Revisions adopted; implementation can proceed to Phase 1 telemetry
+ NFS-health prototype on operator greenlight.*
