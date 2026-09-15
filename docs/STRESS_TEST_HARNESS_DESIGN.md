# Stress-test harness design (not yet built)

**Status: design only, deliberately not executed.** This documents a scale
benchmark scoped during the 2026-09-14 adversarial-review remediation session.
It is not a near-term priority — no current deployment or subscriber is
realistically close to the scales described here. This exists so the harness
doesn't have to be re-designed from scratch when it's actually needed (a real
subscriber approaching these numbers, or a pre-launch capacity-planning pass
before a hosted offering).

Origin: the 2026-09-14 adversarial review's own "Scalability and performance"
section called for exactly this ("The next benchmark should include 10k/100k/1M
memories... Report p50/p95/p99, saturation throughput, queue/pool wait,
CPU/RSS, query plans, recall@K, stale/deleted-hit rate, and cost per successful
task"). The one existing artifact
([`docs/proof/bench-four-20260525T231430Z.json`](proof/bench-four-20260525T231430Z.json))
is a 4-backend, 5,000-record result with no git revision recorded — not
current, not at the scale that matters.

## Two distinct questions, not one

The deployment model matters more than it first appears. MNEMOS's hosted
target is one MNEMOS instance per subscriber (isolated container + isolated
database), not one giant multi-tenant corpus serving many customers out of a
shared instance. That reframes what "scale" means and splits this into two
separate benchmarks with different shapes:

1. **Single-instance growth** — how does ONE subscriber's own corpus perform
   as it grows over years of real use toward 100k, then 1M, memories. This is
   the benchmark the review's own language most directly describes.
2. **Packing density (the actually load-bearing number for a hosted product)**
   — how many separate, isolated per-subscriber MNEMOS instances fit on one
   physical host before CPU/RAM/disk contention degrades service for everyone
   sharing it. This is a capacity-planning question ("how many subscribers per
   host") that a single-corpus-at-scale benchmark cannot answer at all.

Both are documented below. Build #2 first if resources are limited — it is
the more novel and more directly actionable number for a hosted architecture,
and #1 is comparatively well-trodden ground for any Postgres-vector-search
service.

## Hardware — measured 2026-09-14, no Postgres currently running on either

| | CERBERUS (192.168.207.96) | TYDEUS (192.168.207.73) |
|---|---|---|
| CPU | 24 cores | 14 cores |
| RAM | 125GB | 122GB |
| Disk | 1.8TB NVMe (515GB free) + 2×10.9TB HDD | 950GB NVMe (308GB free) |
| Normal role | GPU inference (RTX 4500 ADA), vLLM/llama.cpp narration | Local model slots (coder/reviewer), fleet zoder routing |

Neither host has anything else load-bearing running on it that a multi-hour
stress test would meaningfully disturb, but both ARE in active use for their
normal roles (inference/routing) — coordinate before a real run, don't just
launch one unannounced.

**Topology: server on CERBERUS, load generator on TYDEUS.** Keeping the
benchmark client on a separate physical host from the system under test
avoids the client competing with Postgres/MNEMOS for the same CPU/RAM/disk
cache, which would otherwise silently understate real latency. CERBERUS also
has real HDD alongside NVMe, which lets a real run compare I/O-bound behavior
on spinning disk against NVMe — useful since not every real deployment target
runs on NVMe.

**The hardware-size caveat, stated up front so it isn't rediscovered later:**
at 125GB RAM, even a 1M-row corpus (roughly 5-10GB including 768-dim vector
columns) stays fully page-cache-resident. A benchmark run at full hardware
size measures CPU and query-plan cost, not the I/O pressure a real, smaller
deployment target would feel. Run a SECOND pass with Postgres's
`shared_buffers`/`work_mem` (and ideally a cgroup memory limit on the
container) constrained to a realistic target deployment size — 4-8GB is a
reasonable per-subscriber ceiling to start from — alongside the generous-
hardware run. Report both. Extrapolating from only the generous-hardware
numbers would produce a confidently wrong answer for anyone running on
smaller/cheaper infrastructure, which is most of the actual deployment
target.

## Benchmark #1 — single-instance growth curve

**Corpus sizes:** 1k (sanity baseline), 10k, 100k, 1M memories, real pgvector
HNSW index (this repo already treats HNSW as the only supported index type —
see `mnemos/persistence/schema.py`'s enforcement of it).

**Embeddings: real, not mocked.** Embedding cost is part of the real request
path (in-process llama-cpp-python as of the 2026-05-21 architectural decision,
`mem_1779334716543_f8ebd4` — see the corrected `docs/SEARCH_LATENCY_NOTES.md`).
Mocking it would hide exactly the phase most likely to dominate latency.
Corpus GENERATION (bulk-inserting synthetic memories to reach the target
count) can use a separate, faster batch-embedding path if the harness needs
one to make 1M-row corpus construction tractable in reasonable wall-clock
time — but the actual BENCHMARKED search/write requests must go through the
real request path unmodified.

**Client load shape:** ramp concurrent simulated clients (start at 1, step up
— e.g. 1/5/10/25/50/100 concurrent) issuing a realistic mix of operations, not
search alone:

- semantic search (the primary path the review's SQLite/MySQL scalability
  findings concerned — this exercises the F-series fixes from 2026-09-14:
  the SQLite vec0 candidate-path rework, the MySQL Python-fallback bounding,
  the remote-embedding bounded semaphore)
- writes/ingest (memory creation) at a realistic ratio to reads — a pure-read
  benchmark misses write-amplification and index-maintenance cost under load
- a light mix of version history / DAG reads, since those are real traffic
  in this codebase, not synthetic filler

**Metrics to capture per corpus size × concurrency level:**

- p50 / p95 / p99 / saturation throughput (requests/sec at the concurrency
  level where p99 starts degrading materially)
- queue/pool wait time (Postgres connection pool acquisition — `PG_POOL_MIN`/
  `PG_POOL_MAX`/`MNEMOS_POOL_ACQUIRE_TIMEOUT` per `docs/SCALING.md`)
- CPU and RSS on the server host, sampled during the run, not just before/
  after
- actual Postgres query plans (`EXPLAIN ANALYZE, BUFFERS`) for the semantic
  search query at each corpus size — confirms the HNSW index is actually
  being used and how its behavior changes with corpus growth, not just wall
  time
- recall@K — with a real HNSW index (approximate, not exact), confirm result
  quality doesn't silently degrade at scale; this needs a small held-out set
  of known-correct nearest-neighbor pairs to check against
- stale/deleted-hit rate — a corpus this large should include some fraction
  of soft-deleted/archived memories; confirm the eligibility/visibility
  filtering (the F01/F02/F07 fixes from 2026-09-14 all touch this exact
  surface) doesn't let a stale or deleted row leak into results under load,
  and doesn't become the dominant cost as the deleted fraction grows
- cost per successful task — real dollar cost if any paid embedding/LLM path
  is in the loop; $0 is a valid and expected answer given the in-process
  embedding architecture, but state it explicitly rather than omitting the
  line

## Benchmark #2 — packing density (per-subscriber isolated instances)

**The actual question:** spin up N separate, fully isolated MNEMOS instances
(each its own container, own SQLite-or-small-Postgres database, own
credentials — matching the real single-tenant-per-subscriber deployment
shape) on one host. Each instance gets a realistic per-subscriber corpus size
— NOT 1M; a real subscriber's own memory corpus is far more likely to sit in
the 1k-50k range even for a heavy long-term user, so size each simulated
subscriber instance there, not at benchmark #1's stress ceiling.

**Ramp N (instance count) upward** — start at a small number, double or step
up — while a load generator drives light, realistic per-subscriber traffic
against ALL running instances concurrently (each subscriber's traffic stays
isolated to their own instance, matching real usage — no cross-instance
queries). Find the knee: the instance count at which p95 latency, error rate,
or resource exhaustion (CPU/RAM/disk) starts degrading service for
subscribers whose instances were previously fine.

**This produces the actual capacity-planning number**: "N subscriber
instances per host of this class" — which single-instance benchmark #1
cannot answer at all, no matter how large a corpus it tests, because it never
models resource CONTENTION between isolated tenants sharing a physical host.

**Metrics:** same latency percentiles as #1, but per-instance AND aggregate
across all running instances; host-level CPU/RAM/disk saturation as the
primary signal for where the knee is; time-to-first-degraded-response after
crossing the knee (does it fail gracefully — one subscriber's instance
degrading without cascading to others — or does resource exhaustion take
down instances that were otherwise fine).

## Theoretical packing-density calculation (2026-09-14, NOT measured)

Benchmark #2 above is the empirical version of this question. This section
is a napkin-math estimate to size expectations before spending the
multi-hour effort to build and run it — grounded in one real measurement,
the rest are stated estimates, not measurements. Do not treat this as a
capacity commitment; treat it as "is this roughly 20 subscribers per host,
or roughly 2,000" before deciding whether benchmark #2 is worth building
soon.

**One real data point:** PYTHIA's actual running `mnemos serve --host 0.0.0.0
--port 5002` process (PID 3055047, measured 2026-09-14) is **130MB RSS**.
That is base FastAPI/uvicorn/Python process overhead for this codebase, real
and current, not a guess.

**Everything else below is an estimate, stated as such:**

| Component | Estimate | Basis |
|---|---|---|
| Process base | 130MB | measured (PYTHIA, above) |
| Embedding model resident in RAM | ~150MB | nomic-embed-text-v1.5 is a 137M-parameter model; Q8_0 GGUF quantization is ~1 byte/weight, so a Q8_0 file (and its resident memory footprint via llama-cpp-python) lands close to the parameter count in bytes plus vocab/embedding-table overhead. Not independently measured this session — PYTHIA's running instance does not have the Q8_0 file at its documented default path (`/opt/mnemos/models/`), so its actual current RSS may not yet include a loaded model; treat this line as an upper-bound estimate, not observed. |
| Per-subscriber corpus (SQLite, ~30k memories) | ~150MB | rough: 30k rows × ~5KB/row (content + a 768-dim float32 vector at 3,072 bytes alone + metadata + index overhead) |
| Container/cgroup overhead | ~25MB | typical for a lightweight isolated container's namespace + network stack |
| **Total per instance** | **~455MB → round to 500MB** | sum of the above |

**Architecture note that matters here:** embedding generation is
in-process only by deliberate operator-locked decision (2026-05-21,
`mem_1779334716543_f8ebd4`) — there is no shared/pooled embedding service
option today. That means the ~150MB embedding-model line item above
multiplies linearly with instance count; it is the largest per-instance
cost after the measured process base. **A shared embedding service across
instances (if that architectural constraint were ever revisited) is the
single biggest lever on packing density** — it would remove that line item
entirely from the per-instance cost and only pay it once per host. That is
a real architectural trade worth naming even though changing it is out of
scope here.

**RAM-bound estimate (CERBERUS, 125GB):**

Reserve ~20GB for host OS, existing GPU-inference workload, and headroom.
`(125GB - 20GB) / 500MB per instance ≈ 210 resident instances.`

**Disk-bound estimate, for comparison (not the binding constraint):**

NVMe (515GB free): `515,000MB / 150MB corpus ≈ 3,400+ instances` worth of
storage capacity — an order of magnitude more than the RAM estimate allows.
Disk is not the constraint; RAM is, by a wide margin.

**CPU is a DIFFERENT question from resident packing density:** 24 cores
does not limit how many mostly-idle instances can sit resident in RAM — it
limits how many can be ACTIVELY serving a request (embedding inference,
search) at the same instant before queueing. A single CPU-bound llama-cpp
embedding call on a short text typically holds one core for tens of
milliseconds; with 24 cores, roughly 24 concurrent ACTIVE embedding calls
before contention, regardless of how many hundreds of instances are
resident and idle. This is exactly the distinction benchmark #2's ramp
should measure empirically — "resident capacity" and "concurrent active-
load capacity" are two different numbers, and this calculation only
estimates the first one.

**Headline estimate: roughly 200-250 resident per-subscriber instances on
a CERBERUS-class host (24c/125GB), assuming ~500MB/instance and a ~30k-
memory average subscriber corpus** — driven almost entirely by RAM, with
the in-process embedding model as the single biggest addressable cost.
TYDEUS (122GB RAM, similar order) lands at a similar resident-count
estimate but roughly 58% of CERBERUS's concurrent active-load capacity
(14 cores vs. 24). Treat the "200-250" figure as order-of-magnitude, not
precise — it rests on one real measurement and several stated estimates;
benchmark #2 is what would replace the estimated rows above with real
numbers.

## What this design deliberately does NOT include

- A distributed/multi-node MNEMOS deployment benchmark. Both benchmarks above
  are single-host. Multi-node is a different, larger question for whenever
  the hosted product's actual scale gets there.
- Any claim about what "good" numbers look like. This is a measurement
  design, not a target-setting exercise — SLOs should come from real
  subscriber requirements once any exist, not from guessing at this design
  stage.
- Competitive benchmarking against other memory services (Mem0, Zep,
  Cognee, etc.) — see the adversarial review's own "Competitive positioning"
  section for that separate, larger undertaking (LoCoMo/LongMemEval-style
  task-quality comparison), which is a different kind of benchmark
  (correctness/quality, not throughput/latency) and out of scope here.

## Before running this for real

- Confirm CERBERUS/TYDEUS availability with whoever/whatever else is using
  them for inference at the time — this is a multi-hour, resource-heavy run
  on hosts with an existing normal role.
- Re-verify the hardware table above hasn't drifted (fleet specs do change).
- Decide corpus-generation embedding strategy concretely (real in-process
  embedding for 1M rows will take real wall-clock time — measure that cost
  itself before committing to a generation approach).
