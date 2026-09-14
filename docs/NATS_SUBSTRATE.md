# NATS Substrate

Design reference for the MNEMOS NATS/JetStream substrate. For broker
configuration, stream inventory, ACLs and runbooks, see
[`docs/NATS_OPERATIONS.md`](NATS_OPERATIONS.md).

## Design posture

NATS is an **additive fast path**, never a source of truth. Every event
on the bus has a durable counterpart in the database, and every consumer
has a polling or HTTP fallback that keeps working when the broker is
down. Three invariants follow from that and hold for every producer
below:

* **Publishing is best-effort.** A publish failure or timeout is logged
  and never fails the originating request. `MNEMOS_NATS_PUBLISH_TIMEOUT`
  (default 1s) bounds how long a request can wait on the broker.
* **Delivery is at-least-once.** JetStream's 2-minute `duplicate_window`
  suppresses same-`msg_id` republishes inside the window only; consumers
  are responsible for idempotency on the receive side.
* **Events are addressed, not authoritative.** Nudge-shaped subjects
  carry identifiers and classification metadata; consumers fetch bodies
  through the authorized HTTP or database path. The federation
  direct-upsert contract is the one deliberate exception, and it carries
  an explicit `schema_version` on the wire.

The substrate is disabled entirely when `MNEMOS_NATS_URL` is unset, and
each producer/consumer pair has its own independent enable flag.

## Producer / consumer pairs

### Memory nudges — `mnemos.memory.>` (`MNEMOS_MEMORY`)

Route-level memory writes publish `mnemos.memory.created.<namespace>`,
`mnemos.memory.updated.<namespace>` and `mnemos.memory.deleted.<namespace>`
nudges. `mnemos/federation/nats_consumer.py:consumer_loop` subscribes per
configured peer (`MNEMOS_FEDERATION_NATS_PEERS`) and backfills the body
over the peer's authenticated `/v1/federation/*` HTTP feed, so per-peer
`namespace_filter` / `category_filter` / `auth_token` authorization runs
server-side at fetch time rather than at subscribe time. HTTP federation
pull remains the canonical backfill route.

### Webhook delivery trigger — `mnemos.webhook.>` (`MNEMOS_WEBHOOK`)

Enqueuing a delivery publishes `mnemos.webhook.delivery.queued.<namespace>`;
subscription writes publish `mnemos.webhook.subscription.created.<namespace>`.
`mnemos/webhooks/nats_trigger.py:consumer_loop` turns the queued nudge into
an immediate delivery attempt. The Postgres `webhook_deliveries` outbox is
authoritative and the polling recovery worker delivers everything the bus
misses; NATS only removes the polling latency.

### Webhook outbox dispatch — `mnemos.webhooks.outbox.>` (`MNEMOS_WEBHOOKS_OUTBOX`)

Enabled by `MNEMOS_NATS_WEBHOOKS_ENABLED`. The persistence layer publishes
`mnemos.webhooks.outbox.<tenant>.<event_type>` on outbox insert
(`mnemos/persistence/nats_events.py:publish_webhook_outbox_insert`), carrying
`event_id`, `delivery_id`, `subscription_id`, `event_type`, target URL and
payload hash — never the payload body.
`mnemos/workers/webhooks_dispatch_nats_consumer.py:consumer_loop` consumes it
on durable `mnemos_webhooks_outbox_dispatch`, records the event in
`nats_dispatch_log` before acting, and schedules the delivery attempt.

### Federation memory upsert — `mnemos.federation.memory.>` (`MNEMOS_FEDERATION`)

Enabled by `MNEMOS_NATS_FEDERATION_ENABLED`. Repository-level memory writes
publish a full upsert event on `mnemos.federation.memory.<namespace>` with
`schema_version`, the memory body, and provenance
(`mnemos/persistence/nats_events.py:federation_memory_upsert_event`). The
export predicate excludes federated-in rows, deleted/archived/consolidated
rows, the secret vault namespace unconditionally, and — unless
`federation_feed_include_private` is set for a trusted fleet — anything that
is not world-readable.
`mnemos/workers/federation_memory_nats_consumer.py:consumer_loop` consumes
it on durable `mnemos_federation_memory_upsert`, writing the dedupe row and
the memory upsert inside one transaction so a failed write rolls the dedupe
row back with it. This path requires a Postgres backend; other backends keep
the HTTP-backed federation consumers.

### PANTHEON routing audit — `mnemos.pantheon.routing` (`MNEMOS_PANTHEON`)

When `MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING=1`, each PANTHEON routing-log
write also publishes the routing decision to `mnemos.pantheon.routing`. The
event carries `metadata.schema_version`; the existing `pantheon_routing`
memory write is unaffected.

When `MNEMOS_NATS_AUDIT_CONSUMER_ENABLED=1`, the
`mnemos.workers.pantheon_routing_audit_consumer` worker subscribes and mirrors
events into the `pantheon_routing_audit` table.

**That worker ships in the `mnemos-pantheon` add-on distribution, not in
mnemos-core.** Install the `pantheon` extra (`pip install mnemos-core[pantheon]`)
to get it; startup checks `is_extra_installed("pantheon")` and logs a skip
rather than failing when the add-on is absent
(`mnemos/api/lifecycle_hooks.py:_pantheon_routing_audit_post_db_hook`). The
`mnemos.pantheon.>` stream itself is declared by core regardless, so an
operator can run the producer and the consumer on different nodes.

The `pantheon_routing_audit` table is created by
`mnemos/db_migrations/migrations_v4_2_pantheon_routing_audit.sql`, which is in
the installer's canonical migration list (scoped to the `pantheon` migration
group), with SQLite / MySQL / MariaDB / Oracle / Db2 mirrors alongside it. A
normal install or upgrade applies it; there is no manual step before enabling
the worker.

### GRAEAE consultation fan-out — `mnemos.consultation.>` (`MNEMOS_CONSULTATION`)

Behind `MNEMOS_GRAEAE_NATS_FANOUT`, shipped by the `mnemos-graeae`
distribution. Core declares the stream so the subject family exists on any
broker MNEMOS talks to. Events carry the consultation `id`, task type and
model selection; prompt and response bodies are fetched via
`/v1/consultations/{id}`.

## Idempotency

`nats_dispatch_log`, keyed on `(event_id, subject)`, is the dedupe primitive
for the outbox-dispatch and federation-upsert consumers. It is reached through
the backend-neutral `NatsDispatchLogRepository.record_if_new` ABC
(`mnemos/persistence/base.py`), which performs check-and-insert inside the
caller's transaction so the dedupe row and the side effect commit or roll back
together. Table shape and the per-backend migrations are documented on that
ABC.

The older paths keep their own receive-side guards: federation memory writes
rely on the memory `id` primary key with `ON CONFLICT DO NOTHING`, and webhook
delivery relies on the outbox `delivery_id` UUID plus the Postgres
`SKIP LOCKED` claim.

## Loop-back suppression

Every publish stamps `source_node = get_node_name()`, and consumers drop
events whose `source_node` matches the local node so a federated echo is not
re-applied. `get_node_name()` falls back to the hostname when
`MNEMOS_NODE_NAME` is unset, which collides when replicas share a container
hostname — set it explicitly wherever federation peers are configured.

## Resilience

All four consumer loops share `mnemos/nats/backoff.py:ReconnectBackoff`
(exponential growth, full jitter, reset only after every subscription
succeeds) and each drains its partial subscription set before retrying, so a
broker that accepts connections but rejects subscribes backs off instead of
leaking a socket per attempt. Handler-scope errors stay local to the message
and leave the subscription alive; receive- and ack-scope errors escape to
trigger reconnect. See
[`docs/NATS_OPERATIONS.md`](NATS_OPERATIONS.md) for the full failure-scope
matrix, queue-group deployment guidance and live-broker test coverage.
