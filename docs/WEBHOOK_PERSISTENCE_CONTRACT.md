# Webhook persistence contract

Status: canonical target for the ABC-compliance sequence, item 2. This document
defines the state and repository semantics that every persistence backend must
implement. It does not claim that non-PostgreSQL delivery is available yet;
`supports_webhooks` and the `webhooks` capability remain the runtime truth.

## Decision

MNEMOS standardizes on the PostgreSQL/SQLite **row-per-attempt** model rather
than Oracle/Db2's current mutable `state`/`attempt_count` model.

That choice follows the working delivery code rather than treating the schemas
as equally complete. The PostgreSQL implementation already depends on a durable
payload digest, one-based attempt rows, lease fencing, writer-version fencing,
per-attempt response auditing, and uniqueness across a retry chain. SQLite
already mirrors that shape. Those fields are what make crash recovery and
multi-worker convergence explicit and testable. Oracle/Db2's single mutable
row has no lease owner, payload hash, response audit, superseded marker, or
writer revision, so adopting it would discard invariants that the live worker
currently relies on.

The canonical retry-chain key is:

```text
(subscription_id, event_type, payload_hash)
```

Each delivery attempt is a separate row. `attempt_num` starts at 1 and
increases by exactly one for each successor. Historical attempts are retained
for audit.

## Canonical records

### `webhook_subscriptions`

| Field | Contract |
| --- | --- |
| `id` | Caller-generated string UUID; primary key. |
| `url` | Validated destination URL. Validation remains above persistence. |
| `events` | Non-empty ordered collection of event names. |
| `secret` | HMAC secret. Required internally and never returned by read/list methods. |
| `description` | Optional operator description. |
| `owner_id` | Required tenant owner. |
| `namespace` | Required tenant namespace. |
| `created` | Database-generated UTC creation time. |
| `revoked` | Soft-delete flag. |
| `revoked_at` | UTC revocation time, or null while active. |

Revocation preserves the subscription and all delivery history. Scoped reads
match both owner and namespace for non-root callers. Supplying no owner and no
namespace is the explicit root/operator view. Supplying exactly one scope field
is invalid and repository implementations raise `ValueError`; partial scope is
never a wildcard.

### `webhook_deliveries`

| Field | Contract |
| --- | --- |
| `id` | String UUID; primary key. |
| `subscription_id` | Subscription foreign key with `ON DELETE CASCADE`; normal removal is soft revocation so audit rows remain. |
| `event_type` | Event name copied at enqueue time. |
| `payload` | Exact canonical JSON text to send. |
| `payload_hash` | Lowercase SHA-256 hex of the UTF-8 payload bytes. |
| `attempt_num` | One-based position within the retry chain. |
| `status` | `pending`, `retrying`, `succeeded`, or `abandoned`. |
| `response_status` | HTTP status, when response headers were received. |
| `response_body` | Size-bounded audit text, optionally written after finalization. |
| `error` | Bounded failure detail, or null on canonical success. |
| `scheduled_at` | UTC time at which the attempt becomes claimable. |
| `delivered_at` | UTC terminalization time for final success/failure/revocation. |
| `created` | Database-generated UTC creation time. |
| `status_updated_at` | UTC time of the latest status transition. |
| `superseded` | True only when an abandoned row was displaced by chain progress. |
| `lease_token` | Opaque worker fencing token, or null when unowned. |
| `lease_expires_at` | Database-clock UTC lease expiry, or null when unowned. |
| `writer_revision` | Delivery-writer compatibility fence. |

Repository adapters normalize backend-native UUID, boolean, event-array, LOB,
and timestamp representations into the dataclasses exported from
`mnemos.persistence.base`. Timestamps crossing the repository boundary are
timezone-aware UTC values.

## State machine

```text
dispatch                    claim
   |                          |
   v                          v
pending -----------------> retrying -----------------> succeeded
   |                          |                           terminal
   |                          |
   |                          +---- final failure ----> abandoned
   |                          |                           superseded=false
   |                          |
   |                          +---- retry -----------> abandoned
   |                                                      superseded=true
   |                                                           |
   +-------------------------- successor pending <--------------+
```

`pending` and `retrying` are live states. `succeeded` and `abandoned` are
terminal. An expired lease does not itself consume an attempt or create a new
row: the same live attempt becomes reclaimable. A `retrying` row remains live
after a worker crash only when it has no newer attempt and its lease is absent
or expired.

Within a chain:

- at most one non-superseded live row may exist for an `attempt_num`;
- at most one row may be `succeeded`;
- a retryable failure atomically abandons/supersedes the current attempt and
  inserts at most one successor;
- the successor is scheduled using the configured backoff entry for the failed
  attempt, and no successor is created past `max_attempts`;
- a success atomically wins the chain and abandons any free live successors;
- a revoked subscription or exhausted chain ends as `abandoned` with
  `superseded=false`;
- a periodic idempotent repair sweep terminalizes unleased live rows already
  made obsolete by a successor or a succeeded peer.

`max_attempts` must be at least 1. `backoff_schedule` contains positive seconds
and must have at least `max_attempts - 1` entries, one for every transition that
can create a successor. Repositories validate these policy inputs before making
any finalization mutation.

## Lease and finalization rules

Claims use the database clock for both `claim_db_now` and `lease_expires_at`.
The worker uses that returned window, minus its monotonic elapsed time and a
finalization buffer, as the HTTP deadline. No transaction or connection is held
during DNS or HTTP I/O.

`lease_token` is a fencing token. Claim, pre-send guard, release, and
finalization compare it on every ownership-sensitive mutation. Failure
finalization additionally requires the lease to remain unexpired, because a
stale worker must not advance the retry chain. After a 2xx response, success
may finalize with the still-matching token even if the time window expired; the
chain lock and unique-success invariant then converge races while retaining the
real acknowledgement. This is the existing delivery behavior and bounds the
remaining risk to a duplicate POST after a crash between remote acknowledgement
and durable finalization.

Response headers (`response_status`) and errors are part of atomic
finalization. The size-bounded response body may be stored afterward as an
audit-only update so a slow stream cannot hold a lease or reverse a terminal
result. That update may change only `response_body`.

## Repository operations

`WebhookRepository` exposes these backend-neutral groups:

- subscription CRUD: `create_subscription`, `list_subscriptions`,
  `get_subscription`, `revoke_subscription`;
- audit reads: `list_deliveries`;
- transactional outbox fan-out: `dispatch_event`;
- direct and recovery claims: `claim_delivery`, `claim_due_deliveries`;
- lease fencing: `guard_delivery_claim`, `release_delivery_claim`;
- result and retry convergence: `finalize_delivery`;
- post-finalization audit: `store_delivery_response_body`;
- reconciliation: `repair_delivery_chains`.

All repository methods are database-only and operate inside the supplied
`Transaction`. HTTP sends, NATS publication, sleeps, task scheduling, and
response streaming stay above the repository boundary. NATS is a best-effort
nudge; durable delivery rows and polling recovery are authoritative.

The newly declared methods use non-abstract `NotImplementedError` defaults for
the staged rollout. Making them abstract in this item would prevent every
existing backend class from being instantiated before its implementation item.
Later items replace those defaults backend by backend; capability advertising
must stay false until the entire end-to-end path is wired and verified.

## Existing-schema mapping

PostgreSQL and SQLite already supply most or all canonical delivery fields.
Later backend items must close their remaining schema/adapter gaps without
changing the contract.

Oracle and Db2 currently map only approximately:

| Oracle/Db2 field | Canonical meaning |
| --- | --- |
| `state` | Rename/map to `status`; values must use the four canonical states. |
| `attempt_count` | Convert from the current zero-based insertion shape to one-based `attempt_num`. |
| `next_attempt_at` | Rename/map to `scheduled_at`. |
| `last_error` | Rename/map to `error`. |
| `created_at` | Normalize to `created`. |
| `updated_at` | Use as migration input for `status_updated_at`, then maintain on status transitions. |
| delivery `owner_id` / `namespace` | Not authoritative; authorization follows the referenced subscription. |

Those schemas still need `payload_hash`, `response_status`, `response_body`,
`delivered_at`, `superseded`, `lease_token`, `lease_expires_at`, and
`writer_revision`, plus chain uniqueness and terminal-success enforcement.
That migration and each concrete repository implementation are intentionally
outside item 2.

MySQL/MariaDB also remain outside this item. Their eventual implementation must
materialize this same model rather than defining a third state vocabulary.

The current `PostgresWebhookRepository.dispatch_event` still publishes NATS
inline. That is known staging drift from the target database-only contract,
not evidence that this item migrated runtime behavior. Item 7 must remove the
inline publication when the webhook subsystem is switched to this repository
surface; post-commit scheduling remains the orchestration layer's job.

## Explicit non-goals for item 2

- No concrete PostgreSQL, SQLite, Oracle, Db2, MySQL, or MariaDB repository
  implementation changes.
- No changes to the nine `mnemos.webhooks` delivery modules.
- No API route migration to the repository.
- No capability-advertising change and no claim of non-PostgreSQL runtime
  support.
- No schema migration; this document is the target for the later backend MRs.
