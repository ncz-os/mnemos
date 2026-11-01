# Durable federation changes

Migration `0062_federation_journal.sql` adds a content-free journal to PostgreSQL, SQLite, MySQL/MariaDB, Oracle and Db2. Memory triggers record old/new namespace, category and eligibility in an identity-keyed outbox. A feed publisher assigns the source sequence after the mutation commits. They cover ordinary writes, raw lifecycle updates, archive, consolidation and physical deletion. Journal rows have no foreign key to memories and survive deletion.

A short publisher transaction locks the source clock and assigns sequences to a bounded batch of committed outbox rows. Source mutation transactions do not acquire this lock, so slow provider work in one transaction cannot block unrelated writes through the journal. A transaction that commits late receives a later published sequence even if its identity was allocated earlier. Rolled-back mutations leave no outbox event. Feed/get-by-ID publication and reading happen in the same transaction. Each publisher pass assigns at most 256 events. A sparse filtered page can be empty while unpublished events remain: its content-free checkpoint cursor advances and `has_more` remains true, so consumers must follow that cursor even without memory payloads.

The existing opaque cursor format now carries `journal:<sequence>` in its ID component. Its timestamp remains diagnostic. Old timestamp cursors bootstrap by replaying current state; new peers persist the full opaque cursor with the page transaction. Do not implement a new consumer by saving only the timestamp.

Each page resolves current memory data and its latest published source sequence in one statement snapshot, after selecting a bounded page of matching events. Old scopes receive a withdrawal when current authorization no longer matches. A subscriber allowed both the old and new scopes receives the current memory. A current snapshot may already include a newer committed mutation whose outbox row will be published in the next batch; that later publication emits the state again with a higher sequence. A source change can therefore produce repeated current-state payloads; consumers use `federation_sequence` to reject replay and stale transport delivery.

Receivers persist last-seen versions, including withdrawals, separately from memories. A delayed delete cannot remove a newer replica; a delayed upsert cannot resurrect a withdrawn one. Changes sharing a timestamp are ordered by sequence. NATS notifications are nudges: the production consumer fetches the current authorized item or retained withdrawal over HTTP before applying it. Existing unsequenced peers retain timestamp ordering; unsequenced events cannot overwrite a replica after it has accepted a source sequence.

Deployment considerations:

- Apply migrations before serving the new feed. SQLite/PostgreSQL/MySQL/MariaDB migration and mutation behavior have live integration tests (`test:journal-sql` runs `test_federation_journal.py` against real MySQL 9.1 and MariaDB 12.3 containers on every branch). Oracle/Db2 definitions require their own live engine validation before deployment — no standing CI job covers them yet.
- Bootstrap covers rows present at migration. Deletions that happened before journaling cannot be reconstructed from missing rows. Rebuild an old replica once if it may already contain historical stale copies.
- Journal and receiver tombstone retention is deliberately unbounded. Do not prune them without a peer acknowledgement/retention protocol. Monitor journal size and index growth.
- Treat changes to the private-export posture, peer scope filters, source identity, or restoration of an older source database as replication reconfiguration. Rebuild affected replicas and reset their cursor/version state; ordinary incremental polling is not a safe full-membership reconciliation for these changes.
- The journal retains identifiers and scope metadata, never deleted content or credentials. It is a replication mechanism, separate from the signed audit chain.

PostgreSQL migration `0063_version_recreation.sql` allows a stable federated ID to be withdrawn and re-created without colliding with its retained version history. New incarnations extend the previous version counter and predecessor.

## Replica parity and reconcile (F-15)

A replica can lag or lose rows without the journal ever noticing: the cursor
fenced past them, someone deleted a `fed:<peer>:*` row locally, or a row was
pulled without its vector. `mnemos.domain.federation.parity` compares a
replica with its source and offers a dry-run-first repair.

Both sides are read by keyset page — `fetch_source_export_keyset_page` on the
source and `fetch_parity_page` on the replica — so the sweep is bounded in
memory and never loads the whole table. Every id is classified exactly once:

| population | meaning |
| --- | --- |
| `missing` | exportable on the source, absent from the replica |
| `extra` | `fed:<peer>:*` on the replica, no longer exportable upstream |
| `stale` | present on both sides with a different content hash |
| `null_embedding` | present on the replica without a stored vector |
| `unreachable` | exportable by the source but never on its feed (see below) |

`sample_truncated` is computed against the same cap that bounds `sample`, so a
truncated sample never reports as complete.

### The eligibility gap behind the by-id 404

CHARON's export predicate (`eligible_for_federation_export`) and the feed's
(`eligible_for_federation`) are deliberately not identical:

- The export counts any live, in-scope, non-vault row. It has **no**
  `federation_source` term.
- The feed also excludes rows with `federation_source` / `archived_at` /
  `consolidated_into` set, and the journal triggers only fire for rows whose
  `federation_source` is NULL.

For archived and consolidated rows the difference is correct: those are
revocations, not live content. For a row that itself arrived by federation
there is no publishable event at all, so a replica of a replica can never
receive it. `replica_parity` reports such ids as `unreachable` rather than as a
permanent `missing` deficit, because the two have different fixes.
`tests/test_federation_parity_reconcile.py` pins the gap with
`test_export_definition_is_a_superset_of_the_feed`, and closing it for the
`federation_source` case would need a trigger change on all six backends.

### Reconcile

`reconcile_peer(..., apply=False)` is the default and touches neither side.
With `apply=True` it asks the **source** to re-advertise the missing ids
(`POST /v1/federation/redispatch`) and then runs an ordinary pull. Only the
source can do this: the journal is its own, and the receiver's durable
sequence fence means the original event can never be re-applied. The
redispatch endpoint publishes a new event per still-exportable id and
reserves its sequence immediately, so the event lands strictly after every
sequence already handed out; an event that reused a cursor position would be
invisible to the replica. Reconcile never deletes or overwrites a replica
row.

## Locally soft-deleted replicas (`skipped_deleted`, F-2)

A replica an operator has soft-deleted on the receiving node is a lifecycle
decision, not a stale copy. A sync must not quietly overwrite it, and must not
quietly drop the event either: before migration 0069 the write was a no-op on
every backend and the sync still reported a clean run, so the divergence had no
number anywhere in the system.

The receiver now accounts for it:

- `fetch_federated_memory_marker` returns the row **including** `deleted_at`
  on all six backends (previously the Postgres-family lookups filtered
  `deleted_at IS NULL`, so a deleted replica read as *absent*: the
  insert-or-ignore wrote nothing because the row still existed, and nothing was
  counted — SQLite, which had no such filter, behaved differently).
- `_store_memories` inspects that field, increments `skipped_deleted`, and
  skips the write. It does **not** undelete: reviving a replica is a policy
  decision. Same rule on the journaled path (after the versioned-update
  prepare, which clears `deleted_at`) and the unjournaled one.
- Migration `0069_federation_sync_skipped_deleted.sql` adds the
  `skipped_deleted` column to `federation_sync_log` on all five dialect
  directories; `finish_sync_log` takes it as a per-sync **delta** and
  `fetch_sync_log` / `GET .../sync-log` return it.
- One capped WARNING per sync carries the count, not one per row.

`skipped_deleted` is a delta for that sync; `rows_without_embedding` next to it
is the per-peer total. Both are reported in the same summary line.

Test coverage: `tests/test_federation_soft_deleted_marker.py` (real SQLite,
including that the skip count accumulates across pages of a single sync),
`tests/test_federation_marker_postgres_live.py` (a live Postgres, gated on
`MNEMOS_TEST_DB`), and `tests/test_federation_counter_backend_parity.py`
(pins the SQL and the `finish_sync_log` bind count for all six dialects).

**The report this work came from cites `mnemos/persistence/postgres.py:5976-5991`
against commit `0073bb4`; on current master those lookups live at `:5980` and
`:5986` and no longer carry the filter.** File:line leads from that report are
against a 48-commit-older tree — re-read before trusting them.
