# MORPHEUS queued execution

`POST /admin/morpheus/runs` returns **202 Accepted** with a persisted run whose
phase is `queued`. Poll `GET /v1/morpheus/runs/{id}` for completion. The API
requires root access, the MORPHEUS extra and PostgreSQL. It no longer keeps
the request open while executing the pipeline.

Admission allows at most 16 outstanding queued/running API jobs, with one
outstanding job per namespace. An all-namespace job conflicts with every
namespace. Full or conflicting admission returns 429 with `Retry-After: 30`.
All server workers coordinate through PostgreSQL; only one queued job executes
at a time across processes. The executor reserves one pool connection for a
session advisory lock, so the pool must allow at least two connections. An
undersized pool returns 503 at admission. Phase transactions use other pool
slots; size the pool for foreground traffic as well.

The execution limit is 600 seconds. Graceful cancellation and timeout mark
the run failed. Already committed phase work can remain and must be inspected
before retrying. Queued rows survive restarts, but a process killed mid-phase
leaves a claimed run for operator recovery. The queue does **not** silently
replay destructive partial work. Its claimed row blocks later jobs until an
operator inspects and rolls it back using the existing run rollback endpoint.
Rollback returns 409 while the execution session lock is held, preventing a
live worker from writing after rollback. PostgreSQL releases that lock when
the worker connection dies. This is durable admission with explicit recovery,
not automatic exactly-once completion.

This worker and queue apply to API-triggered jobs. Existing external scripts
that call the phase runner directly must be scheduled separately; they do not
participate in the queue's admission contract. The standalone PERSEPHONE
archival worker uses the configured persistence backend and always closes it.

Compression fidelity work also has a resource boundary: the deterministic
judge admits four running/queued jobs on one worker thread. Inputs over 20,000
characters or one million edit-distance cells, and saturated admission, cause
that judge to abstain. Existing contest fallback scoring then applies; an
abstention is not proof of lossless fidelity. Cancellation does not free a
running computation's permit early. This limits event-loop blocking and
queued memory, without claiming additional CPU throughput from Python threads.
