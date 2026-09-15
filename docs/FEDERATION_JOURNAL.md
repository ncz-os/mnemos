# Durable federation changes

Migration `0062_federation_journal.sql` adds a content-free journal to PostgreSQL, SQLite, MySQL/MariaDB, Oracle and Db2. Memory triggers record old/new namespace, category and eligibility in an identity-keyed outbox. A feed publisher assigns the source sequence after the mutation commits. They cover ordinary writes, raw lifecycle updates, archive, consolidation and physical deletion. Journal rows have no foreign key to memories and survive deletion.

A short publisher transaction locks the source clock and assigns sequences to a bounded batch of committed outbox rows. Source mutation transactions do not acquire this lock, so slow provider work in one transaction cannot block unrelated writes through the journal. A transaction that commits late receives a later published sequence even if its identity was allocated earlier. Rolled-back mutations leave no outbox event. Feed/get-by-ID publication and reading happen in the same transaction. Each publisher pass assigns at most 256 events. A sparse filtered page can be empty while unpublished events remain: its content-free checkpoint cursor advances and `has_more` remains true, so consumers must follow that cursor even without memory payloads.

The existing opaque cursor format now carries `journal:<sequence>` in its ID component. Its timestamp remains diagnostic. Old timestamp cursors bootstrap by replaying current state; new peers persist the full opaque cursor with the page transaction. Do not implement a new consumer by saving only the timestamp.

Each page resolves current memory data and its latest published source sequence in one statement snapshot, after selecting a bounded page of matching events. Old scopes receive a withdrawal when current authorization no longer matches. A subscriber allowed both the old and new scopes receives the current memory. A current snapshot may already include a newer committed mutation whose outbox row will be published in the next batch; that later publication emits the state again with a higher sequence. A source change can therefore produce repeated current-state payloads; consumers use `federation_sequence` to reject replay and stale transport delivery.

Receivers persist last-seen versions, including withdrawals, separately from memories. A delayed delete cannot remove a newer replica; a delayed upsert cannot resurrect a withdrawn one. Changes sharing a timestamp are ordered by sequence. NATS notifications are nudges: the production consumer fetches the current authorized item or retained withdrawal over HTTP before applying it. Existing unsequenced peers retain timestamp ordering; unsequenced events cannot overwrite a replica after it has accepted a source sequence.

Deployment considerations:

- Apply migrations before serving the new feed. SQLite/PostgreSQL migration and mutation behavior have live integration tests. MySQL/MariaDB/Oracle/Db2 definitions require validation on those engines before deployment.
- Bootstrap covers rows present at migration. Deletions that happened before journaling cannot be reconstructed from missing rows. Rebuild an old replica once if it may already contain historical stale copies.
- Journal and receiver tombstone retention is deliberately unbounded. Do not prune them without a peer acknowledgement/retention protocol. Monitor journal size and index growth.
- Treat changes to the private-export posture, peer scope filters, source identity, or restoration of an older source database as replication reconfiguration. Rebuild affected replicas and reset their cursor/version state; ordinary incremental polling is not a safe full-membership reconciliation for these changes.
- The journal retains identifiers and scope metadata, never deleted content or credentials. It is a replication mechanism, separate from the signed audit chain.

PostgreSQL migration `0063_version_recreation.sql` allows a stable federated ID to be withdrawn and re-created without colliding with its retained version history. New incarnations extend the previous version counter and predecessor.
