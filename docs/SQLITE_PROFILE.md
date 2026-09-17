# SQLite Profile

The SQLite persistence backend is the MNEMOS lite profile for laptops, edge
devices, single-user development, and small offline deployments. It implements
the same persistence interface as `PostgresBackend`, but uses SQLite storage,
FTS5, JSON1-compatible JSON text, and `sqlite-vec` when the extension is
available.

For profile selection and the server/edge/dev deployment matrix, start with
the "Choose Your Profile" section in [`DEPLOYMENT.md`](../DEPLOYMENT.md#choose-your-profile).

## When To Use It

Use SQLite when you want a local MNEMOS node with minimal operational surface:

- Developer laptop or CI smoke tests.
- Single-user personal memory store.
- Edge profile targets such as Pi-class systems or phone-adjacent sync tools.
- Offline-first testing before promoting data to a Postgres deployment.

Use Postgres when you need multi-user hard isolation, concurrent writers across
processes, LISTEN/NOTIFY, advisory locks, pgvector indexing, streaming
replication, or production HA.

## Storage Mapping

The SQLite migration chain lives in `mnemos/db_migrations/migrations_sqlite/`
and mirrors the canonical Postgres migration list.

- `UUID` -> `TEXT`
- `JSONB` -> JSON text, queried through SQLite JSON1 where needed
- `TIMESTAMPTZ` -> ISO-8601 `TEXT`
- `TEXT[]` / `UUID[]` -> JSON text arrays
- `pgvector vector(768)` -> `sqlite-vec` `vec0` when available, plus a JSON
  fallback table for portable tests
- PostgreSQL full-text search -> SQLite FTS5
- Partial unique indexes -> SQLite partial unique indexes

## Deliberate Differences

SQLite has no row-level security. The profile is single-user or
single-namespace by deployment convention; tenancy is enforced at the
application layer through the same visibility predicate used by non-RLS reads.

SQLite has no LISTEN/NOTIFY. Federation workers use polling.

SQLite has no advisory locks. The backend serializes transactions through one
connection mutex, matching SQLite's serialized-write model.

### Webhook delivery is not available on this profile

`SqliteBackend.supports_webhooks` is `False`, and the API lifecycle hooks refuse
to start the webhook delivery and NATS-trigger workers against any backend that
does not advertise the capability. The claim/send/finalize worker is
asyncpg-specific.

The write side is complete: the SQLite schema provisions the full canonical
`webhook_deliveries` shape described in
[`WEBHOOK_PERSISTENCE_CONTRACT.md`](WEBHOOK_PERSISTENCE_CONTRACT.md), and
`SqliteWebhookRepository` implements the repository surface, so subscriptions
can be created and outbox rows are appended durably and transactionally. Nothing
drains them. **Rows are written but never delivered on this profile.** Use
Postgres for any deployment that depends on webhooks firing.

Where the terminal-success invariant is enforced also differs: Postgres uses a
trigger, while the SQLite profile enforces it in `mnemos.webhooks.finalize`
because the profile does not rely on trigger functions for retry-chain safety.

## What Works On This Profile, And What Does Not

Two long-standing PostgreSQL-only paths were closed on `master` after the
v7.0.0 tag, so they are **not in the published `7.0.0` artifact**; see the
*Unreleased* section of [`CHANGELOG.md`](../CHANGELOG.md).

### API keys can now be minted here

Looking a key up had been backend-neutral since v6.3, but *creating* one was
PostgreSQL-only in both the admin route and the installer, so this profile could
authenticate with a key and never produce one. Both now go through
`OAuthRepository`. A second, quieter bug is fixed with it: PostgreSQL has seeded
the `default` root user since `migrations_v1_multiuser.sql` and SQLite never
did, so `api_keys.user_id` had nothing to join against and every minted key was
silently dropped by `lookup_api_key`'s INNER JOIN.
`migrations_v7_0_default_user_seed_sqlite.sql` seeds that row; seeding grants
nothing on its own.

### MPF export and import now run here

The CHARON `/v1/export` and `/v1/import` path reached the database through a
raw-asyncpg module and was gated behind a PostgreSQL pool check. It now uses the
persistence ABC throughout and runs on this profile. The export's consistency
guarantee is real here but is **reached by a different mechanism than on the
other five backends**, and that is worth understanding before relying on it:

- SQLite has no `READ COMMITTED` / `REPEATABLE READ` vocabulary — only
  `DEFERRED` / `IMMEDIATE` / `EXCLUSIVE` locking modes. In **WAL mode** (which
  this profile enables) a read transaction sees a snapshot taken at its first
  read and nothing committed afterwards, which is the property the export needs.
- A `BEGIN DEFERRED` does not take that snapshot until the first actual read, so
  the implementation issues a trivial `SELECT` immediately after `BEGIN` to pin
  it at transaction entry. Without that, rows committed in between would be
  visible and the guarantee would be quietly weaker.
- `readonly=True` is a server-enforced prohibition on the other five backends
  and only a snapshot/locking hint here. A write issued on a `readonly=True`
  SQLite transaction still succeeds; do not rely on SQLite to *reject* one.

Round-tripping (export, restore into a second empty database, idempotent
re-import, tenant/vault scoping, and a concurrent-write snapshot test) is
live-tested on this profile.

### Still returns 503 here

`POST`/`GET /admin/users`, the OAuth provider/identity admin routes, all five
`/v1/webhooks` routes, `/v1/kg/*`, the version and DAG routes,
`/v1/memories/{id}/compression-manifests`, `restore=true`,
`POST /v1/memories/rehydrate`, and the KRONOS routes still require a PostgreSQL
pool. The full list, and which of them are deliberate, is in
[`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md).

## Configuration

Install the optional dependencies:

```bash
pip install 'mnemos-core[sqlite]'
```

Prefer the profile flag:

```bash
mnemos serve --profile edge
mnemos serve --profile dev
```

Select SQLite explicitly when you need to override the profile default:

```bash
MNEMOS_PERSISTENCE_BACKEND=sqlite
MNEMOS_SQLITE_PATH=/var/lib/mnemos/mnemos.db
```

Or use URI auto-detection:

```bash
MNEMOS_PERSISTENCE_BACKEND=auto
MNEMOS_DATABASE_URL=sqlite:////var/lib/mnemos/mnemos.db
```

The legacy `personal` profile name resolves to `edge`.

## Operational Notes

Keep one MNEMOS process writing to the SQLite database. WAL mode is enabled on
open, and foreign keys are enabled for every connection.

Back up the `.sqlite3` file and its WAL/shm companions together, or checkpoint
before copying. For large multi-user deployments, migrate to Postgres instead
of stretching SQLite beyond its intended profile.
