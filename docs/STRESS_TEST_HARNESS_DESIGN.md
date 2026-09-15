# Scale benchmark requirements

The historical 5,000-record artifact in `docs/proof/` has no source revision
and is not evidence for the current release or a hosted capacity commitment.
No million-memory or subscriber-packing benchmark has been completed.

Measure two workloads separately: one growing corpus, and multiple isolated
subscriber instances sharing a host. For each run record source and add-on
SHAs, dependency versions, backend/index settings, hardware, memory limits,
embedding model/backend, warm-up, random seed, workload and error counts.

For corpus growth, use 1k, 10k, 100k and 1M memories, realistic content sizes,
restrictive ownership/namespace/tag filters, archived/deleted records and
concurrent writes. Ramp concurrency through 1, 5, 10, 25, 50 and 100 clients.
Record p50/p95/p99, throughput, queue and pool wait, CPU, peak RSS, database
size, query plans, recall@K against exact ground truth, stale/deleted-hit rate
and cost per successful task. Synthetic vectors isolate retrieval cost;
separate end-to-end runs must include real embedding latency. Never describe
synthetic-vector repository tests as HTTP or model-performance benchmarks.

For packing density, create separate databases, credentials and instances.
Measure idle and active RSS, model residency and shared page-cache behavior,
then increase instance count while holding per-instance load constant. Report
the first sustained latency/error threshold and recovery behavior. Do not
convert disk footprint to RAM or extrapolate a single idle process to a
subscriber-per-host claim. Use a separate load generator and reserved test
hardware; impose realistic memory and I/O limits as well as testing generous
resources.

Embedding generation supports local backends and an explicit HTTP backend
(`MNEMOS_EMBED_BACKEND=http`). The HTTP implementation reuses its client and
bounds concurrency. A shared embedding service is therefore already possible;
its availability, model compatibility, network latency and contention must be
measured. Local models have per-worker memory and CPU costs. Neither mode is
free of infrastructure cost, even when there is no metered provider charge.

SQLite vec0 computes native exact nearest neighbors; reducing Python cosine
calls does not make total search logarithmic. Restrictive filters can trigger
an authoritative scan. MySQL's Python cosine fallback now pages through rows
and retains only the best K, bounding application memory while still doing
O(N * dimension) work. Benchmark these cases explicitly against native indexed
backends. A performance test that counts Python calls is a mechanism check,
not a latency SLA.

Task-quality comparisons with other memory systems require a separate,
reproducible LoCoMo/LongMemEval-style evaluation with the same models,
retrieval budgets and datasets. Throughput alone does not establish a quality
or cost advantage.
