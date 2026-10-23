# MNEMOS compression — operator-batched doctrine

Compression is opt-in and operator-batched through the admin endpoints:

- `POST /admin/compression/enqueue`
- `POST /admin/compression/enqueue-all`
- `GET /admin/compression/status` — read-only introspection (see below)

Memory creation does not auto-enqueue contest jobs. This is deliberate:
compression is expensive work. It can involve LLM-judged contests and GPU
execution, and pervasive auto-compression on every create would saturate the
GPU pool while increasing cost-per-memory without an operator-controlled bound.

The compression flow is:

1. An operator enqueues specific memories or a bounded batch through the admin
   API.
2. Contest workers drain `memory_compression_queue`.
3. Eligible engines produce candidates and the judge selects winners.
4. `memory_compression_candidates` records the full contest audit trail.
5. `memory_compressed_variants` stores the current winning artifact.
6. `RehydrationResponse.compression_ratio` and
   `StatsResponse.average_compression_ratio` surface real ratios from those
   compression artifacts.

The admin endpoint request models and queue inserts live in
[`mnemos/api/routes/admin.py`](../mnemos/api/routes/admin.py).

## Read-only status endpoint (backlog F-15)

`GET /admin/compression/status` (admin scope, same as the other
`/admin` routes) reports compression/contest state without mutating
anything:

- `contest_enabled` / `apollo_enabled` — the effective
  `MNEMOS_CONTEST_ENABLED` / `MNEMOS_APOLLO_ENABLED` settings;
- `engines.apollo` / `engines.artemis` — `{installed, enabled}`.
  `installed` is an import probe of the engine module (never an engine
  construction); `enabled` is installed AND the contest gate the
  distillation worker checks is on — Apollo additionally gates on
  `MNEMOS_APOLLO_ENABLED`, Artemis (the default contest engine) has no
  separate toggle and rides the contest gate. A not-importable engine
  reports `installed: false` with a `*_reason`;
- `queue.stats` — the persistence ABC's `get_queue_stats()` snapshot
  (`total` / `pending` / `running` / `done` / `failed` / `variants`),
  non-negative ints;
- `variant_count` — the ABC's `get_compressed_variants_count()` read;
  `null` with a `variant_count_reason` when the backend cannot supply
  it (e.g. the `memory_compressed_variants` table is absent on a
  pre-v3.1 schema) — never a fabricated zero.

The route is strictly read-only: it issues aggregate SELECTs only and
never calls the enqueue routes. It reads through the persistence ABC
(`CompressionQueueRepository.get_compression_queue_status`), so it
works on every backend; a missing persistence backend yields the same
503 the other admin routes use.

Session messages and session memory injections do not carry compression tags.
Slice 12 dropped the always-NULL `compression_ratio` columns from
`session_messages` and `session_memory_injections`. Session memory injection
still slices `doc["content"][:500]` as a prompt-budget control. That is
truncation, not compression, and no ratio is recorded for it.

The Slice 12 migration drops are idempotent and safe: the removed session-layer
columns were always NULL fiction columns, not the real compression store.
