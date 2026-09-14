# NATS / JetStream Operations

Operational reference for the NATS substrate that backs MNEMOS federation
push consumers and webhook delivery triggers. GRAEAE consultation fan-out is a
separate path behind the `MNEMOS_GRAEAE_NATS_FANOUT` flag, and ships with the
`mnemos-graeae` distribution. For the design rationale, see
[`docs/NATS_SUBSTRATE.md`](NATS_SUBSTRATE.md).

## Streams

`ensure_streams()` (in `mnemos/nats/client.py`) declares six streams
at startup. Declarations are idempotent — re-running against a broker
with a matching config is a no-op. Every stream uses the same shape:
`LIMITS` retention, `FILE` storage, 30-day `max_age`, 10 GB
`max_bytes`, 2-minute `duplicate_window`.

| Stream                  | Stream subject filter      | Subjects actually published                                                                     | Producer                          | Consumer                                            |
|-------------------------|----------------------------|-------------------------------------------------------------------------------------------------|-----------------------------------|-----------------------------------------------------|
| `MNEMOS_MEMORY`         | `mnemos.memory.>`          | `mnemos.memory.created.<ns>`, `.updated.<ns>`, `.deleted.<ns>`                                    | memory routes                     | `mnemos/federation/nats_consumer.py`                |
| `MNEMOS_CONSULTATION`   | `mnemos.consultation.>`    | consultation fan-out events                                                                       | `mnemos-graeae` add-on            | GRAEAE backends                                     |
| `MNEMOS_WEBHOOK`        | `mnemos.webhook.>`         | `mnemos.webhook.delivery.queued.<ns>`, `mnemos.webhook.subscription.created.<ns>`                  | webhook routes / dispatcher       | `mnemos/webhooks/nats_trigger.py`                   |
| `MNEMOS_PANTHEON`       | `mnemos.pantheon.>`        | `mnemos.pantheon.routing`                                                                         | `mnemos-pantheon` add-on          | `mnemos.workers.pantheon_routing_audit_consumer` (ships in the `pantheon` extra) |
| `MNEMOS_WEBHOOKS_OUTBOX`| `mnemos.webhooks.outbox.>` | `mnemos.webhooks.outbox.<tenant>.<event_type>`                                                    | `mnemos/persistence/nats_events.py` | `mnemos/workers/webhooks_dispatch_nats_consumer.py` |
| `MNEMOS_FEDERATION`     | `mnemos.federation.>`      | `mnemos.federation.memory.<ns>`                                                                   | `mnemos/persistence/nats_events.py` | `mnemos/workers/federation_memory_nats_consumer.py` |

> **Operational trap — `mnemos.webhook.` (singular) and
> `mnemos.webhooks.` (plural) are two different subject families on two
> different streams.** Singular `mnemos.webhook.>` carries the delivery
> trigger and subscription events on `MNEMOS_WEBHOOK`; plural
> `mnemos.webhooks.outbox.>` carries outbox dispatch events on
> `MNEMOS_WEBHOOKS_OUTBOX`. An ACL, subject filter, monitoring rule or
> `nats sub` that uses one where the other was meant silently matches
> nothing — no error, no traffic. Grant both explicitly; never assume a
> `mnemos.webhook.>` wildcard covers the outbox.
>
> Likewise `mnemos.federation.>` (the `MNEMOS_FEDERATION` direct-upsert
> contract) is distinct from `mnemos.memory.>` (the nudge contract the
> HTTP-backfill federation consumer reads). Both carry federation traffic;
> they are not interchangeable.

Storage is `FILE` (durable across broker restart). Retention is
`LIMITS` policy — messages drop when EITHER the 30-day age limit OR
the 10 GB byte limit is hit, whichever fires first.

The 2-minute `duplicate_window` matters for the publish-with-`msg_id`
pattern used by every producer:
`nats_bus.publish_event(subject, payload, msg_id=<stable-id>)` will
not double-publish if the same `msg_id` arrives within 2 minutes.
Outside that window, a re-publish becomes a new message — consumers
must idempotency-check on the receive side (see
[Symptom: duplicate messages](#symptom-duplicate-messages)).

### Storage exhaustion fallback

If the broker rejects `add_stream` with `insufficient storage
resources` (NATS error 10047), `ensure_streams()` retries with a
`max_bytes=1 GB` fallback so a small dev/test broker can still
declare the streams. Production should provision adequately to
avoid the fallback path.

### Replay window

Federation push consumers subscribe with `DeliverPolicy.NEW`
([`mnemos/federation/nats_consumer.py`](../mnemos/federation/nats_consumer.py)),
so a fresh process startup does NOT replay the entire 30-day
backlog. Operators who want to replay (e.g. after a peer was
offline) should use the HTTP federation pull path — it's the
canonical backfill route. The NATS consumer is an additive
fast-path; the HTTP poll is the durable fallback.

## Payload sensitivity

What lands on each subject:

| Subject family               | Payload                                                                                                        | Sensitivity |
|------------------------------|----------------------------------------------------------------------------------------------------------------|-------------|
| `mnemos.memory.created.*`    | NUDGE only: `memory_id`, `namespace`, `category`, `source_node`. NO content body.                              | low-medium  |
| `mnemos.memory.updated.*`    | Same shape as `created` — id + namespace + category + source_node, no body.                                    | low-medium  |
| `mnemos.memory.deleted.*`    | `memory_id` + tombstone metadata.                                                                              | low         |
| `mnemos.consultation.*`      | Consultation `id`, `task_type`, model selection. Prompt/response excerpts NOT published — backends fetch via `/v1/consultations/{id}` for the body. | low-medium |
| `mnemos.webhook.*`           | Delivery `id`, `subscription_id`, `event_type`, target URL, payload hash. NOT the payload body.                | medium      |
| `mnemos.webhooks.outbox.*`   | Outbox insert nudge: `event_id`, `delivery_id`, `subscription_id`, `event_type`, target URL, payload hash, namespace, tenant. NOT the payload body. | medium |
| `mnemos.federation.memory.*` | **Full memory upsert, body included** — `content`, `verbatim_content`, `metadata`, category, namespace, `permission_mode`, provenance, `schema_version`. | high |
| `mnemos.pantheon.routing`    | Routing decision: alias/model requested, resolved target, outcome, latency, token counts, request/tenant id. No prompt or completion text. | medium |
| MCP SSE summaries (`/sse`)   | Filtered subset by default: `subject`, `memory_id`, `namespace`, `category`, `source_node`. Full content only when `MNEMOS_MCP_NATS_RAW=true`. | medium |

**`mnemos.federation.memory.>` is the one content-carrying family.**
Everything else on the bus is a nudge. The direct-upsert contract exists
so a trusted peer can apply a memory without a second HTTP round trip,
which necessarily puts the body on the wire. Its export predicate is the
only authorization boundary: federated-in, deleted, archived and
consolidated rows never publish; the secret vault namespace never
publishes under any setting; and unless `federation_feed_include_private`
marks the deployment a trusted fleet, only world-readable rows publish.
Treat a subscriber on this family as equivalent to a read-authorized
federation peer, and scope broker permissions accordingly.

**Architectural note — why the nudge families carry no content.**
Apart from the direct-upsert family above, the shipped subjects are
NUDGES, not content carriers. Federation push
receivers receive the nudge, then fetch the content via the
authorized HTTP federation feed (``GET
/v1/federation/feed?since=...&memory_id=...``) which enforces
per-peer ``namespace_filter`` / ``category_filter`` /
``auth_token`` — the same authorization predicate as the HTTP-pull
path. This means:

  * On the nudge families the broker does NOT hold a 30-day copy
    of every memory body. Those streams retain 30 days of small
    JSON blobs; the body lives in Postgres and travels over the
    authenticated HTTP feed when peers actually need it.
    `MNEMOS_FEDERATION` is the exception — where it is enabled,
    budget its 30-day retention against real memory bodies and
    protect that stream's files like the database itself.
  * Per-peer authorization runs server-side at content fetch
    time, NOT at NATS subscribe time. A peer subscribed to
    ``mnemos.memory.created.*`` sees that an event happened (id +
    namespace + category) but cannot retrieve the body unless the
    HTTP feed authorizes the pull.
  * The metadata fields shipped on the bus (namespace, category,
    source_node) are themselves operator-classified data — a
    rogue subscriber learns WHAT topics are flowing, not WHAT was
    written. Rate-limit + ACL the bus accordingly (see next
    section), but don't treat broker storage as a content vault.

**Operator implication:** the broker's stream files are
operationally important — they hold the activity audit trail and
nudge backlog. Encrypt at rest, restrict the filesystem, and
back up alongside Postgres.

## NATS ACL recommendations

The MNEMOS publish/subscribe topology is asymmetric:

  * MNEMOS server processes PUBLISH on every shipped subject family:
    ``mnemos.memory.>``, ``mnemos.consultation.>``, ``mnemos.webhook.>``,
    ``mnemos.webhooks.outbox.>``, ``mnemos.federation.>``,
    ``mnemos.pantheon.>``.
  * MNEMOS server processes SUBSCRIBE to the same subjects (federation
    push receivers, webhook NATS triggers, webhook outbox dispatch,
    federation direct-upsert, PANTHEON routing audit).
  * MCP HTTP/SSE clients SUBSCRIBE only to the principal-namespaced
    summary subset (subjects derived server-side from the
    authenticated principal — a non-operator client cannot pick its
    own subject filter; see ``mnemos/mcp/http.py::_parse_nats_sse_subjects``).

**Write the allow-list from the full six-family inventory**, not from
the three families an older deployment happened to use. Each family in
the [Streams](#streams) table is published by a live code path; omitting
one produces a broker that accepts the connection, rejects the publish
or subscribe, and shows up only as a consumer that never receives
anything. The plural/singular pair (``mnemos.webhook.>`` vs
``mnemos.webhooks.outbox.>``) is the one operators most often collapse
into a single rule — they must both appear.

Recommended ``authorization`` block (NATS server config snippet):

```hocon
authorization {
  users = [
    # MNEMOS server processes — full pub/sub on every shipped subject.
    {
      user: "mnemos-server"
      password: "$MNEMOS_NATS_TOKEN"
      permissions: {
        publish:   { allow: ["mnemos.>"] }
        subscribe: { allow: ["mnemos.>", "_INBOX.>"] }
      }
    }

    # Operator / observability clients — subscribe-only.
    {
      user: "mnemos-observer"
      password: "$MNEMOS_OBS_TOKEN"
      permissions: {
        publish:   { deny: [">"] }
        subscribe: { allow: ["mnemos.>", "_INBOX.>"] }
      }
    }

    # External federation peers (if you trust a peer to publish into
    # YOUR memory namespace, which is unusual — most operators
    # prefer the HTTP federation feed for inbound). Scope tightly:
    # one user per peer, publish-only, narrowed to the namespaces
    # that peer is allowed to write. The direct-upsert family
    # carries memory BODIES, so a publish grant here is a write
    # grant on the local store.
    # {
    #   user: "peer-alpha"
    #   permissions: {
    #     publish:   { allow: ["mnemos.federation.memory.alpha"] }
    #     subscribe: { deny: [">"] }
    #   }
    # }
  ]
}
```

The "external federation peers" pattern is intentionally commented
out — most operators receive federation via the HTTP pull / push
endpoints, NOT by giving an external peer publish access to their
broker. Only enable it when the peer is operationally trusted at
the same level as the local mnemos process.

**Anti-pattern:** a single shared `mnemos` user with no per-role
split. A compromised MCP HTTP/SSE bridge would then have publish
authority on every memory subject, which lets an attacker forge
``mnemos.memory.created.<any-namespace>`` events that the
federation push receiver would write into the local store as
incoming federated rows. Always split publish from subscribe.

## Subject isolation per tenant

The MCP HTTP/SSE bridge derives subscriber subjects from the
authenticated principal:

  * Default subject for a non-operator client:
    ``mnemos.<event_class>.<event_action>.<safe_namespace>``
    where ``safe_namespace`` comes from the principal's
    ``user.namespace`` (sanitised to a NATS-safe token).
  * Operator-class principals (``role='root'``) MAY pass
    ``?subjects=mnemos.x.y.*`` to widen, but the substring filter
    must still start with ``mnemos.`` and contain no whitespace —
    enforced by ``_parse_nats_sse_subjects`` so a non-operator
    cannot tunnel arbitrary subject filters.

For multi-tenant deployments, the recommended hardening:

  1. Set ``MNEMOS_MCP_NATS_RAW=false`` (the default) so MCP SSE
     emits filtered summaries, not full content payloads.
  2. Scope NATS user permissions to per-namespace publish/subscribe
     prefixes when running multiple tenants on a shared broker.
     Example for tenant ``alice``:

     ```hocon
     {
       user: "tenant-alice"
       permissions: {
         publish:   { allow: ["mnemos.memory.created.alice",
                              "mnemos.memory.updated.alice",
                              "mnemos.memory.deleted.alice",
                              "mnemos.webhooks.outbox.alice.>",
                              "mnemos.federation.memory.alice"] }
         subscribe: { allow: ["mnemos.>.alice", "_INBOX.>"] }
       }
     }
     ```

  3. Run separate broker accounts (NATS multi-tenancy primitive)
     for hard isolation between tenants who must NOT see each
     other's metadata even at the subject level.

## MCP event bridge — live-only contract

``GET /sse`` (the MCP HTTP/SSE event bridge in
``mnemos/mcp/http.py``) is a **live-only telemetry stream**, NOT
a replay-able audit log.

What that means in practice:

  * A SSE client connected at time T sees events published
    AFTER T. Events from before T are not surfaced; this
    bridge does not consume the JetStream stream's 30-day
    backlog.
  * The subscription uses ``DeliverPolicy.NEW`` +
    ``AckPolicy.NONE`` even when it goes through JetStream's
    consumer API, so messages are not acked back to the broker
    and the broker keeps no per-subscriber lag state.
  * If the underlying connection exposes the core-NATS handle
    (``js._nc``), the bridge uses core-NATS ``subscribe`` —
    even more clearly live-only with no JetStream involvement
    at all.

Why: SSE is a long-poll affordance for agent surfaces (Claude
Code, Cursor, ChatGPT Pro Developer Mode) that want to react
to NEW events as they fire. A replay-on-reconnect contract
would force operators to think about per-client cursors and
durable state, which is the opposite of what an interactive
agent loop wants — they generally just resubscribe and pick
up from "now."

Operators who need historical / audit-style reads:

  * Use the HTTP REST surface (``GET /v1/memories/...``,
    ``GET /v1/federation/feed``, ``GET /v1/memories/{id}/log``
    etc.) which goes through the visibility-gated repository
    path with the proper ``VisibilityFilter.for_read`` checks
    + RLS context.
  * All six NATS streams DO retain 30 days of events for backend
    consumers — federation push receivers, webhook NATS triggers,
    webhook outbox dispatch, federation direct-upsert and the
    PANTHEON routing audit are all durable. The MCP SSE bridge is
    the OUTLIER that gives up durability deliberately.

## Federation peer config

Set `MNEMOS_FEDERATION_NATS_PEERS` to a JSON array per peer:

```json
[
  {
    "name": "peer-alpha",
    "nats_url": "nats://<host>:4222",
    "nats_token": "<NATS broker token>",
    "subjects": ["mnemos.memory.>"],
    "base_url": "http://<host>:5002",
    "auth_token": "<HTTP Bearer for /v1/federation/* endpoints>"
  }
]
```

| Field         | Required | Used for                                                     |
|---------------|----------|--------------------------------------------------------------|
| `name`        | yes      | Per-peer durable consumer name + log/metric label            |
| `nats_url`    | yes      | NATS connection target                                       |
| `nats_token`  | optional | Bearer-style token if the peer's broker requires auth        |
| `subjects`    | yes      | Subject patterns to subscribe to (typically `mnemos.memory.>`) |
| `base_url`    | yes      | HTTP federation endpoint for by-id backfill of replayed rows |
| `auth_token`  | yes      | HTTP Bearer for the peer's `/v1/federation/*` routes         |

Peers are loaded via `configured_nats_peers(settings)`. If the env
var is empty/unset, federation NATS is silently disabled (HTTP
federation pull continues). One consumer task is launched per peer
at startup — see `mnemos/api/lifecycle_hooks.py:_federation_nats_post_db_hook`.

## `MNEMOS_NODE_NAME`

Each NATS publish embeds `source_node = get_node_name()` so
consumers can filter loop-back (a peer's events that originated
from this node and were echoed back through federation).

If `MNEMOS_NODE_NAME` is unset, `get_node_name()` falls back to
`socket.gethostname()`. That works on a single host but **collides**
when multiple containers share the same hostname (common with
Docker Compose default container hostname=service name).

A boot-time warning fires when peers are configured but
`MNEMOS_NODE_NAME` is unset — see
`mnemos/api/lifecycle_hooks.py:_federation_nats_post_db_hook`.
Production deployments with federation peers should set this
explicitly to a stable, deployment-unique value.

## Reconnect backoff

`mnemos/nats/backoff.py:ReconnectBackoff` — exponential growth with
full jitter on broker outage. It is the single shared helper; all four
consumer loops construct it identically
(`ReconnectBackoff(base_seconds=1.0, cap_seconds=retry_seconds)`):

| Consumer loop | Module | Stream | Durable prefix |
|---|---|---|---|
| Federation nudge receiver (per peer) | `mnemos/federation/nats_consumer.py:consumer_loop` | `MNEMOS_MEMORY` | `mnemos_federation_` |
| Webhook delivery trigger | `mnemos/webhooks/nats_trigger.py:consumer_loop` | `MNEMOS_WEBHOOK` | `mnemos_webhook_delivery_trigger` |
| Webhook outbox dispatch | `mnemos/workers/webhooks_dispatch_nats_consumer.py:consumer_loop` | `MNEMOS_WEBHOOKS_OUTBOX` | `mnemos_webhooks_outbox_dispatch` |
| Federation memory upsert (per peer) | `mnemos/workers/federation_memory_nats_consumer.py:consumer_loop` | `MNEMOS_FEDERATION` | `mnemos_federation_memory_upsert` |

The PANTHEON routing audit consumer
(`mnemos.workers.pantheon_routing_audit_consumer`, `MNEMOS_PANTHEON`) is a
fifth loop, but it lives in the `mnemos-pantheon` add-on rather than in
mnemos-core; it is only started when the `pantheon` extra is installed and
`MNEMOS_NATS_AUDIT_CONSUMER_ENABLED` is set.

The window starts at 1s, doubles up to a 30s cap (overridable via
`retry_seconds` kwarg), and the actual sleep on each attempt is
`uniform(0, current_window)`. The window resets to base ONLY after
all subscriptions succeed, so a broker that accepts the connection
but rejects subscribe (stream drift, durable name mismatch) still
backs off rather than hot-looping.

### Why full jitter

Reference: AWS Architecture Blog,
"[Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)"
(Marc Brooker). With fixed-delay retry, a fleet of workers that
disconnected together also reconnects together — thundering herd.
Full jitter spreads the retry distribution uniformly across the
exponential window so collisions are rare.

## Resource cleanup on subscribe failure

Each of the four loops carries its own `_drain_partial(nc, subscriptions)`
with identical semantics. It runs on:

1. Cancellation (`asyncio.CancelledError`)
2. Connect-level exceptions before `_consume_subscription` starts
3. Subscribe-level exceptions (durable name collision, stream
   drift, consumer-group recovery)
4. Receive-scope exceptions DURING consume (`next_msg` raising
   non-timeout NATS errors).
5. Ack-scope exceptions DURING consume (`_ack` failing — the broker
   is what we're acking to, so this is a NATS-connection issue).

Both (4) and (5) are re-raised out of `_consume_subscription` so they
reach the outer drain handler.

`_drain_partial` itself does:

1. Best-effort `await sub.unsubscribe()` for each successful
   subscription.
2. Best-effort `await nc.drain()` (falls back to `nc.close()`).

### Three-scope split inside `_consume_subscription`

`_consume_subscription` separates the per-message lifecycle into three
distinct try/except scopes — each with its own classification of
"escape for reconnect" vs "stay local":

| Scope        | Method            | Failure disposition                                                                |
|--------------|-------------------|------------------------------------------------------------------------------------|
| Receive      | `sub.next_msg`    | Timeout → continue. Anything else → re-raise (NATS issue, reconnect)                |
| Handle       | `handle_message`  | **Any** exception → log + don't ack + continue (subscription stays alive)           |
| Ack          | `_ack`            | Any exception → re-raise (broker-side issue, reconnect)                             |

The handle-scope is the load-bearing one for stability:

* `asyncpg.PostgresError` (transient DB hiccup)
* `asyncpg.InterfaceError` (closed/bad pool connection)
* `RuntimeError` from a custom store/fetch path
* HTTP errors from federation by-id backfill (401, timeout, etc.)

…all stay local. The NATS subscription itself is healthy in those
cases, and tearing it down would just delay unrelated traffic on the
same peer behind reconnect backoff. JetStream redelivers unacked
messages after the ack-wait window, so transient handler failures get
retried without code-side intervention.

The split is deliberate and load-bearing: classifying handler errors by
exception type instead of by scope tears the NATS subscription down on a
plain `RuntimeError` or a closed pool connection, neither of which the
broker can do anything about. Scope, not exception type, decides whether
a failure escapes.

With backoff bounding the rate, drain bounding the total, the
receive/ack escape paths handling NATS issues, and the handle scope
keeping handler errors local, a sustained failure stays in a
bounded steady state instead of accumulating sockets, wedging, or
amplifying handler hiccups into peer-wide reconnect storms.

## Operator runbook

### Symptom: federation events stop arriving from a peer

Check in this order:

1. `mnemos/federation/nats_consumer.py` log — look for
   "federation nats consumer peer=<name> unavailable: ...".
   The exception message identifies whether it's connect-level
   (broker down/unreachable) or subscribe-level (stream/consumer
   drift).
2. Broker reachability:
   `nats sub --server $PEER_NATS_URL "mnemos.memory.>"`
   (with `--auth-token` if the peer requires auth). If you see
   messages there but the consumer log is silent, the issue is
   in the consumer's subscribe path.
3. Stream presence on the peer:
   `nats stream info MNEMOS_MEMORY --server $PEER_NATS_URL`.
4. Durable consumer name collision:
   `nats consumer ls MNEMOS_MEMORY` — the nudge-receiver durable is
   `mnemos_federation_<peer_name>_<sanitized_subject>`.
5. If the peer uses the direct-upsert contract, check the other
   stream too: `nats consumer ls MNEMOS_FEDERATION`, durable
   `mnemos_federation_memory_upsert_<peer>_<sanitized_subject>`.
   The two paths fail independently — memory nudges can be flowing
   while `mnemos.federation.memory.>` is not, and vice versa.
6. As a fallback, restart the local mnemos process — the HTTP
   federation pull path will still backfill any rows missed
   while NATS push was unavailable.

### Symptom: webhook deliveries delayed (broker outage)

* Delivery still happens via the polling workers — `repair_worker_loop`
  and `delivery_worker_loop` (exported from `mnemos/webhooks/`, started
  in `mnemos/api/lifecycle_hooks.py`; `recovery_worker_loop` is a
  compatibility alias for `delivery_worker_loop`). Latency goes from
  ~real-time (NATS push trigger) to the polling cadence
  (`RECOVERY_POLL_INTERVAL` in `mnemos/webhooks/types.py`, default 30s).
* No deliveries are lost. The Postgres `webhook_deliveries`
  outbox is authoritative; both NATS webhook paths are nudge
  fast-paths only.

### Symptom: duplicate messages

Within a 2-minute window, the `duplicate_window` config blocks
re-publishes that supply the same `msg_id`. Outside that window
(network split lasting >2 min, broker restart spans the window),
duplicates can land. JetStream is at-least-once regardless, so every
consumer has a receive-side guard:

* **`nats_dispatch_log`, keyed `(event_id, subject)`** — the dedupe
  primitive for the webhook outbox dispatch and federation memory
  upsert consumers. Reached through
  `NatsDispatchLogRepository.record_if_new`
  (`mnemos/persistence/base.py`), which does check-and-insert inside
  the caller's transaction: the federation consumer writes the dedupe
  row and the memory upsert in one transaction, so a failed upsert
  rolls the dedupe row back rather than swallowing the event. A
  redelivery finds the row present and is acked without a second side
  effect. Implemented on every backend (Postgres / SQLite / MySQL /
  MariaDB / Oracle / Db2) against the same primary key.
* **Federation nudge receiver:** memory `id` primary key +
  `ON CONFLICT (id) DO NOTHING`.
* **Webhook delivery trigger:** `webhook_deliveries.id` UUID primary
  key, plus the `SKIP LOCKED` claim that serialises the actual send.

To inspect: `SELECT * FROM nats_dispatch_log WHERE event_id = '<id>'`
tells you whether a given event was already applied. Rows accumulate —
prune on whatever cadence your retention policy calls for, but never
below the JetStream 30-day window, or a redelivered message from the
backlog loses its dedupe record.

If you see duplicate side-effects despite the receive-side
idempotency, check whether a consumer is processing AT LEAST ONCE
(JetStream default) but treating the side effect as exactly-once.

### Symptom: stream config drift

`add_stream` is idempotent for MATCHING configs and raises for
mismatched configs. If you change `max_age` / `max_bytes` /
`duplicate_window` in `_stream_config()` and redeploy against a
running broker, the new declaration will fail with
"already in use" — the running stream keeps the OLD config. To
apply the new config, operator must `nats stream update` manually
or delete + recreate the stream (latter loses retained messages).

## Multi-replica deployment (queue groups)

Every consumer loop supports JetStream queue groups, each behind its own
env var. By default the substrate is single-replica safe — leave the var
empty and each loop keeps its legacy durable:

| Env var                              | Consumer loop                   | Empty (default)                                                          | Non-empty                                                                                                                        |
|--------------------------------------|---------------------------------|--------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| `MNEMOS_FEDERATION_NATS_QUEUE_GROUP` | Federation nudge receiver       | Durable: `mnemos_federation_<peer>_<subject>`. Single subscriber.        | Durable: `mnemos_federation_q_<group>_<peer>_<subject>_<hash>`. JetStream load-balances within the group.                            |
| `MNEMOS_WEBHOOK_NATS_QUEUE_GROUP`    | Webhook delivery trigger        | Durable: `mnemos_webhook_delivery_trigger_<node>`. Per-replica fan-out.  | Durable: `mnemos_webhook_delivery_trigger_q_<group>_<hash>`. JetStream delivers each nudge to exactly ONE replica.                   |
| `MNEMOS_NATS_WEBHOOKS_QUEUE_GROUP`   | Webhook outbox dispatch         | Durable: `mnemos_webhooks_outbox_dispatch_<node>`. Per-replica fan-out.  | Durable: `mnemos_webhooks_outbox_dispatch_q_<group>_<hash>`. One replica per event. Falls back to `MNEMOS_WEBHOOK_NATS_QUEUE_GROUP` when unset. |
| `MNEMOS_NATS_FEDERATION_QUEUE_GROUP` | Federation memory upsert        | Durable: `mnemos_federation_memory_upsert_<peer>_<subject>`.            | Durable: `mnemos_federation_memory_upsert_q_<group>_<peer>_<subject>_<hash>`. Falls back to `MNEMOS_FEDERATION_NATS_QUEUE_GROUP` when unset.  |

Queue-mode durable names append a 12-char SHA-256 digest of the full
untruncated group/peer/subject triple after capping the readable part, so
two distinct triples can never collide inside JetStream's 128-character
durable-name limit.

### Why durable == queue

`nats-py 2.14`'s `js.subscribe(subject, queue=Q, durable=D)` raises
``cannot create queue subscription 'Q' to consumer 'D'`` whenever
`D != Q` — internally the queue name *is* the durable name. mnemos
forces both to the same value (the full namespaced durable above)
and stamps the consumer's `deliver_group` to match. There is no
operator-visible knob to pull these apart.

### Rollout: separate-namespace coexistence (NOT a queue subscriber on a legacy durable)

Queue-mode durables intentionally live in a DIFFERENT namespace
(`_q_<group>_…`) from the legacy single-replica durables. This is
not cosmetic — it is the only way nats-py allows the two modes to
coexist on the same broker, because:

* nats-py rejects a queue subscription against a consumer whose
  `deliver_group` is unset (`cannot create a queue subscription
  for a consumer without a deliver group`).
* Switching an existing consumer's `deliver_group` requires
  delete-and-recreate; mnemos does not do this on your behalf.

So a replica with the queue group set and a replica still running
default behavior land on **two separate JetStream consumers** for
the same stream. Both consumers receive every event published to
the stream. Both replica groups process those events. **Expect
the federation receive path to do duplicate work during a partial
upgrade window.** The persistence layer's
`ON CONFLICT (id) DO NOTHING` makes this idempotent at rest, but
the work itself is not free — get the partial-upgrade window
short, and prefer flipping the entire fleet at once when feasible.

The webhook side has the same shape, but the outbox `delivery_id`
UUID + Postgres `SKIP LOCKED` claim already serializes the actual
delivery work, so duplicate nudges land on the same outbox row and
only one wins. Less to worry about there.

### Steps

1. Pick a stable group name (e.g. `fed_pool`, `webhook_pool`).
2. Set the queue-group env vars for the loops you are pooling
   (`MNEMOS_FEDERATION_NATS_QUEUE_GROUP`,
   `MNEMOS_WEBHOOK_NATS_QUEUE_GROUP`,
   `MNEMOS_NATS_WEBHOOKS_QUEUE_GROUP`,
   `MNEMOS_NATS_FEDERATION_QUEUE_GROUP`) identically on every replica
   that is joining.
3. Set `MNEMOS_NODE_NAME` per replica to a stable, unique value so
   `source_node` filtering still works (federation echo suppression
   does not depend on the queue group).
4. Roll the fleet. The queue-mode durable is auto-created the
   first time a replica subscribes; subsequent replicas bind to it.
5. Once every replica is in the group, delete the
   stale legacy durables to stop their event flow (and the
   duplicate work):

   ```
   nats consumer rm MNEMOS_MEMORY mnemos_federation_<peer>_<subject>
   nats consumer rm MNEMOS_WEBHOOK mnemos_webhook_delivery_trigger_<old_node_name>
   nats consumer rm MNEMOS_WEBHOOKS_OUTBOX mnemos_webhooks_outbox_dispatch_<old_node_name>
   nats consumer rm MNEMOS_FEDERATION mnemos_federation_memory_upsert_<peer>_<subject>
   ```

   per legacy durable. JetStream auto-prunes inactive consumers
   after the 30-day age limit if you forget.

### Verifying queue-group rollout

After the roll, confirm the queue-mode consumer exists and has the
expected `deliver_group`:

```
nats consumer info MNEMOS_MEMORY \
  $(nats consumer ls MNEMOS_MEMORY | grep mnemos_federation_q_)
```

In the output, `Delivery Group:` should match the `_q_<group>_…`
prefix of the durable name. `Push Bound:` should be `true` while a
replica is subscribed.

To verify load-balancing across replicas (rather than one replica
taking everything), publish a small burst on the source side and
watch the per-replica receive logs:

```
# On the source node:
for i in $(seq 1 20); do
  nats pub mnemos.memory.created.default '{"memory_id":"verify_'"$i"'"}'
done

# Then on each receiver replica, count distinct verify_N IDs in
# the last minute of mnemos.federation.nats_consumer logs:
journalctl -u mnemos -n 1000 | grep "received=" | tail -5
```

A healthy queue group shows the burst spread across replicas (not
all 20 on one node). The exact split is not guaranteed even — NATS
delivers to whichever subscriber is currently free — but it should
not concentrate on a single replica when traffic is high enough.

If the burst lands entirely on one replica, check:

* All replicas actually have the env var set (`systemctl show
  mnemos -p Environment | grep QUEUE_GROUP`), spelled identically —
  a group name that differs by one character creates a second,
  separate queue-mode durable rather than joining the first.
* The consumer's `Delivery Group` is set (queue-mode); if it's
  empty the consumer is in legacy single-subscriber mode and
  the env var didn't take effect.
* The replicas are in fact connected to the same broker — split-
  brain across two brokers means each broker has its own consumer
  with its own queue group.

## Live-broker integration tests

`tests/integration_nats/` is a pytest suite that runs against a real
NATS broker. It auto-skips when no broker source is available, so a
default `pytest` run on a machine without NATS stays green. There are
two independent broker sources, and each test routes to the one it
needs:

| Source | How to enable | Tests it serves |
|---|---|---|
| Operator-managed broker | `MNEMOS_NATS_TEST_URL=nats://host:4222` (plus `MNEMOS_NATS_TEST_TOKEN` if the broker requires auth) | Stream declaration, drift, queue-group balance, outbox + federation publish/consume round trips |
| Test-managed broker | a `nats-server` binary on `PATH`, or `MNEMOS_NATS_SERVER_BIN=<abs path>` | Partial-outage tests, which need to stop and restart the broker |

The runtime contracts the suite proves:

* `add_stream` is idempotent on a matching config, across repeated
  redeclarations (`test_stream_drift.py`).
* `add_stream` raises (does not silently mutate) on a drifted config;
  the existing stream keeps its old config.
* `ensure_streams()` is safe to re-run — second-call no-op — and
  survives a broker restart (`test_partial_outage.py`).
* Queue-group subscriptions actually load-balance: two replicas joined
  to the same group both receive traffic and JetStream does not
  duplicate messages across them (`test_queue_group_balance.py`).
* The webhook outbox dispatch and federation memory upsert contracts
  publish and consume end to end on a live broker
  (`test_v5_2_nats_substrate.py`).

### Partial-outage coverage

`test_partial_outage.py` drives the **production** consumer loop —
`mnemos.federation.nats_consumer.consumer_loop`, not a fake — against a
pytest-owned `nats-server` subprocess. The `managed_broker` fixture
(`tests/integration_nats/conftest.py`) gives each test its own broker
process and JetStream store dir, and exposes `pause()` (SIGSTOP),
`resume()` (SIGCONT), `kill()` (SIGKILL) and `restart()` — the restart
reuses the same port and store dir so durables and streams persist
across the cycle, which is what makes the reconnect path testable
rather than merely reconnectable-in-principle. The store and handler
are faked only at the repository boundary, so the NATS subscribe /
receive / ack / drain / backoff path under test is the shipped one.

What that covers: the loop recovers after a hard broker restart and
receives messages published afterwards; it survives a paused handler
without tearing down the subscription; and `ensure_streams()` stays
correct across a managed-broker restart. `test_managed_broker_identity.py`
guards the fixture itself — the readiness probe must accept only its
own child process, so a stray broker already listening on the chosen
port cannot be mistaken for the one under test.

Unit-level fakes in `tests/test_federation_nats_consumer.py` and
`tests/test_webhook_nats_trigger.py` remain as the fast, always-on
tier; the live-broker suite is the one that proves the same behavior
against a real JetStream.

### Running it

```
# Against your own cluster:
MNEMOS_NATS_TEST_URL=nats://<broker-host>:4222 pytest tests/integration_nats/ -v

# Outage tests, using a locally installed nats-server:
pytest tests/integration_nats/test_partial_outage.py -v
```

The suite creates per-test isolated streams (random suffix) and
deletes them in finalizers, so it is safe to point at a shared
broker — though running against a quiet staging cluster is
preferable. Operators rolling out queue groups can use it as a
pre-prod smoke check.
