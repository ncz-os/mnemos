# PANTHEON — Unified LLM Provider Facade

**Packaging:** PANTHEON is a separately-installable add-on. It ships as the
`mnemos-pantheon` namespace distribution under the `mnemos.domain.pantheon.*`
import path and is selected from mnemos-core with the `pantheon` extra
(`mnemos-pantheon>=0.2,<0.3`). mnemos-core carries the integration seams —
MCP tool wrappers, config settings, routing-audit migrations, deploy units —
and none of the gateway implementation.

**Serving surface:** the opt-in `/pantheon/v1` slice. Routing is a direct HTTP
forward from the gateway to the selected upstream; NATS carries the
best-effort routing-audit substrate, not the main request path.

**Position in stack:** above local GPU inference and GRAEAE; below every
OpenAI-compatible client.

**Greek-name fit:** *Temple of all gods.* One facade, many providers behind it. Pairs with CHARON (the ferryman who carries memories across systems): same interop posture, different surface.

## Shipped behavior

The `/pantheon/v1` slice provides per-(user, session) hard caps for
`usage_tier=consultation_only` models, best-effort MNEMOS `pantheon_routing`
memory writes for successful and failed gateway calls, rolling-window adaptive
selection for `auto:*` aliases, and `/pantheon/v1/route/explain` output
carrying candidates, scores, the selected backend, and the selection reason.

The consultation cap bucket is process-local. That is correct for a single
MNEMOS process and for test/dev deployments; a horizontally scaled deployment
needs a Redis-backed bucket so every replica shares the same per-session count.

Not yet implemented: Redis-backed cap buckets, full tool-use streaming
passthrough across every provider adapter, real-time provider health over NATS,
and KRONOS forecasting integration for proactive routing.

---

## Mission

> One place to store provider keys. One URL for clients. One catalog spanning every model the operator has access to.

Today: configuring an agent stack means juggling 5–10 provider keys (OpenAI, Together, Groq, Gemini, Perplexity, vLLM, local Ollama, …) across every tool that wants LLM access. Each tool reimplements rate-limit handling, fallback, model-name normalization. Each tool maintains its own key vault.

PANTHEON collapses that to:

- **One PANTHEON URL** as the OpenAI-compat endpoint for every client (Cursor, Continue, langchain, openai-python, custom-provider configs).
- **One PANTHEON token** per tool — a personal token that PANTHEON uses to identify and rate-limit the caller, while it holds the real provider keys server-side.
- **One `/v1/models` catalog** that's the union of every backend, with extended metadata (cost tier, capabilities, context length, current health) so clients pick intelligently.

Every existing tool keeps working — PANTHEON is OpenAI-shape. The win is the centralization.

---

## CHARON contract note (related work)

As of 2026-09-18, CHARON portability and ingest routes (`/v1/import`,
`/v1/export`, universal ingest, document import), their CLI tools, adapters,
and STYX ship in `mnemos-core` and mount unconditionally. Only Docling's heavy
conversion dependencies require the optional `docling` extra.

The CHARON portability subsystem restricts the trigger-suppressed
`memory_versions` sidecar import path to the **root + preserve_owner=true**
admin/migration path. Non-root callers can ship `kg_triples` and
`compression_manifest` sidecars without restriction, but `memory_versions`
requires a root bearer token (`--preserve-metadata` on the import CLI). This
restriction is structural: the interaction of caller-scoped deterministic id
derivation, ON CONFLICT idempotency, and `memory_versions` surviving memory
deletion makes the non-root path a defect-prone surface where adversarial
review surfaced a sequence of stale-state edge cases that each required
extending the equality check on every column. The architectural restriction
collapses the entire class.

**Practical impact for PANTHEON clients:** none; CHARON is always installed.
Callers hitting `/v1/import` for non-DAG-history use cases (typical agent
memory sync) work normally. Cross-system migrations go through the documented
root path. A peer-system adapter that wants to preserve authoritative version
history across systems needs a root token — the same constraint as any
administrative data movement.

## What we are NOT building

- **A new message queue.** Where PANTHEON needs a bus — routing-audit fan-out, shared cooldown and breaker state across a worker pool — it uses **NATS JetStream** (Apache 2.0, single binary, ~30MB RAM, native Python client). Building bespoke MQ infrastructure is not the project. Same posture as MNEMOS choosing pgvector over a custom vector store: pick the boring proven option, focus engineering on the layer that's actually novel.
- **A new pricing database.** The catalog is regenerated from machine-readable upstream price feeds by a timer-driven sync job, with a vendored last-good seed for feed outages. See "Catalog sync" below.
- **A new auth system.** Tokens map to existing MNEMOS owner_id + namespace identity. The same auth that gates `/v1/memories` gates `/v1/chat/completions` here.

## OpenAI-Compatible Memory Injection Control

The OpenAI-compatible gateway injects MNEMOS memory context by default on
`POST /v1/chat/completions`. Callers can bypass retrieval for a single request
without changing gateway configuration:

- Header: `X-Mnemos-Inject-Memory: false`
- Body extension: `"mnemos_inject_memory": false`

Malformed header values are treated as default-on. When the header is supplied,
non-streaming JSON responses include `mnemos_metadata.memory_injected` so callers
can verify whether the gateway searched and injected memory for that request.

## Position in the fleet

```
                     ┌───────────────────────────────────────────┐
                     │ Clients (Cursor, Continue, langchain,     │
                     │ openai-python, MCP-aware agents…)         │
                     └───────────────┬───────────────────────────┘
                                     │ /v1/chat/completions
                                     │ /v1/models, /v1/embeddings
                                     ▼
                     ┌───────────────────────────────────────────┐
                     │ PANTHEON frontend (FastAPI, MCP server)   │
                     │ - /v1/* OpenAI-compat surface             │
                     │ - extended /v1/models catalog             │
                     │ - usage_tier policy enforcement           │
                     │ - per-tenant token + cost cap             │
                     │ - direct upstream forwarding              │
                     └───────────────┬───────────────────────────┘
                                     │ alias resolver
                                     │ + adaptive policy
                                     ▼
                     ┌───────────────────────────────────────────┐
                     │ Direct upstream forward                    │
                     │ - provider key resolved server-side       │
                     │ - caller identity attached as MNEMOS hdrs │
                     │ - OpenAI `user` field set from auth       │
                     └───────────────┬───────────────────────────┘
                                     │
       ┌─────────────────────────────┼───────────────────────────┐
       ▼                             ▼                           ▼
   Triton/vLLM                 Cloud APIs                Cloud APIs
   (gpu-host)                  (Together, Groq,          (OpenAI,
   local GPU                    Perplexity)               Gemini)

Routing decisions are also written to MNEMOS as `pantheon_routing` memories.
When enabled, the same payload is published to `mnemos.pantheon.routing` and
mirrored by the optional audit consumer into `pantheon_routing_audit`.
```

GRAEAE remains a peer service — but PANTHEON's catalog can advertise GRAEAE-backed virtual models like `consensus:reasoning` that route through GRAEAE under the hood. Clients don't need to know the difference.

---

## Catalog: extended `/v1/models`

Stock OpenAI returns `{id, object, created, owned_by}`. PANTHEON returns the same plus structured metadata; clients that ignore the new fields still see a normal model list.

```json
{
  "object": "list",
  "data": [
    {
      "id": "mistral-7b-instruct",
      "object": "model",
      "created": 1714000000,
      "owned_by": "pantheon:vllm.local",
      "pantheon": {
        "backend": "vllm.local",
        "cost_tier": "free",
        "usage_tier": "agentic_ok",
        "context_window": 32768,
        "capabilities": ["chat"],
        "latency_p50_ms": 850,
        "health": "ok",
        "rate_limit_rpm": null
      }
    },
    {
      "id": "claude-opus-4-7",
      "object": "model",
      "created": 1714000000,
      "owned_by": "pantheon:anthropic",
      "pantheon": {
        "backend": "anthropic",
        "cost_tier": "premium",
        "usage_tier": "consultation_only",
        "context_window": 200000,
        "capabilities": ["chat", "tool_use", "vision"],
        "latency_p50_ms": 4200,
        "health": "ok",
        "rate_limit_rpm": 50,
        "advisory": "Anthropic ToS forbids agentic-loop usage; PANTHEON enforces a per-(user,session) hard cap. For agent workflows use a model with usage_tier=agentic_ok."
      }
    }
  ]
}
```

### Catalog sync

The catalog is regenerated by a timer-driven sync job rather than assembled
from live worker advertisements. mnemos-core ships the ops units and the
entrypoints; the fetch/normalize implementation lives in the add-on
(`mnemos.domain.pantheon.pricing`).

| Artifact | Role |
|---|---|
| `systemd/pantheon-catalog-sync.service` | oneshot refresh job |
| `systemd/pantheon-catalog-sync.timer` | daily schedule, `Persistent=true`, randomized delay |
| `scripts/refresh_pantheon_catalog.py` | script entrypoint invoked by the unit |
| `mnemos/tools/refresh_pantheon_catalog.py` | console-script shim; exits 2 with an install hint when the add-on is absent |

```
      machine-readable upstream price feeds
      + live model/availability APIs
      + vendored last-good seed
                 │  pantheon-catalog-sync.timer (daily)
                 ▼
      ┌─────────────────────────────┐
      │ PANTHEON catalog cache      │
      │ JSON + SQLite on disk       │
      │ - provider / model list     │
      │ - cost_per_mtok in/out      │
      │ - context, capabilities     │
      │ - usage_tier per model      │
      │ - per-source fetched_at     │
      └─────────────┬───────────────┘
                    │ read by the gateway
                    ▼
              /v1/models response
```

Cache locations are operator-set (`PANTHEON_CATALOG_CACHE`,
`PANTHEON_CATALOG_SQLITE`, or `MNEMOS_PANTHEON_CATALOG_CACHE_PATH`). Each
source carries its own `fetched_at` and staleness; a failed fetch keeps the
last-good catalog rather than emptying it, so a feed outage degrades freshness
and never availability.

Health and latency fields are observed at request time from the routing-audit
rolling window, not advertised by workers.

Adding a provider is therefore a catalog-plus-key operation, not a code change:
the model appears at the next sync, and the `usage_tier` annotation travels
with the catalog entry rather than living in a second file to keep in sync.

### Required fields per entry

| Field | Type | Purpose |
|---|---|---|
| `backend` | string | which upstream serves this. e.g. `vllm.local`, `together`, `groq` |
| `cost_tier` | enum: `free`, `paid`, `premium` | for `prefer:free` style routing |
| `usage_tier` | enum: `agentic_ok`, `consultation_only`, `embedding_only` | enforcement boundary |
| `context_window` | int | max prompt+completion tokens |
| `capabilities` | list | `chat`, `tool_use`, `vision`, `json_mode`, `embedding`, `reasoning` |
| `latency_p50_ms` | int | observed median; updated from rolling window |
| `health` | enum: `ok`, `degraded`, `down` | from observed outcomes + cooldown state |
| `rate_limit_rpm` | int or null | provider's stated limit (null = local/unbounded) |
| `advisory` | string (optional) | human-readable warning surfaced to clients |

The catalog is computed, not hand-configured: the sync job supplies the static
metadata and the gateway overlays observed health and latency.

---

## Routing

### Capability-based model names

Beyond literal names like `mistral-7b-instruct`, PANTHEON resolves alias prefixes:

```
auto:reasoning            → highest-quality reasoning model the caller can afford
auto:cheap-fast           → free-tier first, paid fallback only
free:embedding            → free embedding model (Nomic / MiniLM via vLLM)
tool:json                 → model with capabilities=["tool_use", "json_mode"]
consensus:reasoning       → routes through GRAEAE for multi-LLM consensus
```

The alias is resolved server-side at request time, using:

1. The caller's tenant policy (cost cap, allowed_tiers).
2. Current backend health (skip degraded/down and anything in cooldown).
3. MNEMOS-stored history for this caller (which backend has been winning recently).

### Hint headers (alternative to alias)

```
X-Pantheon-Cost-Tier: free
X-Pantheon-Latency: low
X-Pantheon-Capability: tool_use
X-Pantheon-Mode: agentic       # locks out usage_tier=consultation_only
```

Stock OpenAI clients ignore these headers (no harm). Smart clients use them to express intent without changing model name.

### Deterministic policy (no LLM in the loop)

The routing decision is a pure function of:
- catalog state (synced metadata + observed backend health)
- caller policy (tenant config + recent usage)
- request hints (model name + headers)
- MNEMOS rolling stats (last N minutes per provider × outcome)

**No Claude / GPT call is ever made to decide routing.** That would (a) waste credits and (b) reintroduce the agentic-loop pattern Anthropic flags. Policy improvements come from operator-tuned weights or A/B-tested adjustments, not from another LLM choosing.

---

## `usage_tier` and the Anthropic boundary

**Default fleet posture (from CLAUDE.md):** Anthropic is forbidden as an LLM provider for agent frameworks. Single-shot consultation is fine.

PANTHEON encodes this directly:

| Tier | Meaning | Allowed clients | Anthropic example |
|---|---|---|---|
| `agentic_ok` | Free-form use, fine for repeated calls in a session | All | local vLLM, Together, Groq |
| `consultation_only` | One-shot reasoning; per-(user,session) hard cap | Hint-aware clients only; agentic clients filtered out by default | Claude Opus / Sonnet via Anthropic Max sub |
| `embedding_only` | Embedding endpoints (no chat) | All embedding clients | OpenAI embed, Nomic via vLLM |

### Enforcement mechanism

1. Client sends request with `model: "claude-opus-4-7"` or `auto:reasoning` resolving to it.
2. Frontend checks `usage_tier`. If `consultation_only`:
   - Look up the caller's recent dispatch history (MNEMOS).
   - If they've made >N calls to this tier in the last hour: 429 with `{"error": "consultation_tier_cap", "suggested_alternative": "...", "rationale": "..."}`.
   - If their request looks agentic (header `X-Pantheon-Mode: agentic`, OR detected via session-id repetition rate): 403.
3. Otherwise dispatch normally.

### Why this matters for the fleet

The user runs Anthropic Max as a personal sub. They're not abusing it; consultation use IS in-scope. PANTHEON makes the boundary explicit so:
- Personal consultation calls still flow.
- Agentic-loop misuse (which would draw Anthropic flags) is structurally prevented.
- The doc + UX surface tells external users where the line is, so PANTHEON-as-OSS doesn't accidentally encourage ToS violations.

---

## Dispatch: direct HTTP forward

Routing is a direct HTTP forward. Streaming and non-streaming chat completions,
and embeddings, all take the same path: alias resolution → adaptive policy
selection → forward to the selected provider, with the provider key resolved
server-side and caller identity attached. Streaming responses are proxied
through as SSE. NATS carries audit events and shared limiter state, never the
request itself.

There is no per-provider worker daemon and no `work.<backend>` queue. Scale-out
is process replication of the same gateway app behind the reverse proxy, which
is why cooldown, breaker, and rate-limit state must be shared (see "Packaging
and deployment") before a pool runs more than one worker.

A queue-mediated path — competing consumers per provider subject, nak-with-delay
on rate limits, redelivery semantics — remains a plausible future direction for
batch and async workloads. It is not the current model, and nothing in the
shipped slice depends on it.

---

## Auth model

### Per-tenant tokens

PANTHEON issues tokens per tenant (or per-tool-per-tenant for finer audit). The token carries:

- `tenant_id` — for cost ceilings, audit attribution
- `allowed_tiers` — subset of `{agentic_ok, consultation_only, embedding_only}`
- `cost_ceiling_usd_per_day` — optional
- `model_allowlist` / `model_denylist` — optional

The frontend validates the token, attaches the policy to the request envelope, then dispatches.

### Provider keys

Stored centrally in PANTHEON's keyvault, encrypted at rest under a master key; production deployments can back it with HashiCorp Vault, AWS KMS, or sealed secrets. The gateway unseals on startup and resolves the provider key server-side per request — a key is never handed to a client.

```
~/.pantheon/keys/
├── openai.enc         ← OpenAI API key
├── together.enc       ← Together API key
├── groq.enc           ← Groq API key
├── gemini.enc         ← Google AI key
├── perplexity.enc     ← Perplexity key
├── anthropic.enc      ← (consultation-only flag enforced)
└── master.key         ← root key (mode 600, owner only)
```

A new tool integrating with PANTHEON gets ONE token. Adding a backend is a key in the vault plus a catalog entry. No client config changes anywhere.

---

## Client-side: agent model discovery is the hard problem

The single-API-config-on-the-client pitch only delivers if the agent ALSO knows how to use that single config to discover and pick from many models. Most existing agents don't:

| Agent | Model discovery today | What needs to change |
|---|---|---|
| **OpenClaw** | Hardcoded model list at compile time. `models.providers.<name>.model = "..."` in config. | Add `discover-from-endpoint` mode: on connect, GET `/v1/models`, populate the available-model list. Honor PANTHEON catalog metadata (cost_tier, usage_tier, capabilities). Allow runtime model switching in the active session. |
| **Hermes** | Similar — provider config has a fixed model name per provider. | Same patch: discover-from-endpoint, runtime switching, capability-aware aliases (`auto:reasoning`). |
| **zterm** | Talks to whatever its single configured backend exposes. | Already simple; can use PANTHEON's `auto:` aliases without code changes. |
| **Cursor / Continue / langchain** | Custom-provider config takes a base URL and a model name. They DO call `/v1/models` for autocomplete but don't use the result for live switching. | Lighter touch: PANTHEON works today with a static model name; capability aliases need an extension on their side. |

### The contribution work

PANTHEON ships with PR-ready patches for the agents we own/influence:

1. **OpenClaw model-discovery PR** — adds a `discovery: auto` config option. When set, the agent calls `/v1/models` at startup, builds its model list dynamically, and exposes a `/model <name>` slash command for runtime switching. Catalog metadata (`pantheon.cost_tier`, `pantheon.usage_tier`) drives the agent's filter (e.g. agentic-mode auto-skips `consultation_only`).

2. **Hermes model-discovery PR** — same shape, different file.

3. **MCP-aware agents (Claude Code, etc.)** — the MCP front-door is the easier path here. Agent calls `pantheon_list_models(filter_capabilities)` via MCP, gets a typed response with metadata baked in.

4. **OpenAI-shape ecosystem (langchain, openai-python)** — these aren't ours to patch. PANTHEON's solution is the alias convention: clients pass `model="auto:reasoning"` and the resolution happens server-side. No client changes required.

Where upstream is slow to take a discovery patch, the fallback is a documented
sidecar branch operators can apply themselves; the server side needs nothing
from the client either way.

### Why client-side changes matter

Without them, PANTHEON degrades to "single endpoint with one default model." That's still useful (key-vault consolidation, audit, cost cap) but it's not the unlock. The unlock is the agent treating PANTHEON as a fleet of capabilities and switching across them based on the task at hand. That requires the agent to KNOW it can switch — which means it has to discover.

## MCP front-door

Alongside the HTTP `/v1` surface, MNEMOS's MCP server exposes two PANTHEON
tools:

```
pantheon_list_models(filter_capabilities?, filter_tier?, max_cost?)
pantheon_route_explain(messages, model_or_alias)   # diagnostic
```

Both are **capability-filtered out of the advertised tool list when the
`pantheon` extra is not installed**, so an agent connected to a core-only
MNEMOS never sees them. If one is invoked anyway, the handler returns
`{"success": false, "error": "PANTHEON not installed"}` rather than raising an
import error.

Inference itself is not an MCP tool. There is no `pantheon_chat` or
`pantheon_embed` — completions and embeddings go through the OpenAI-compatible
HTTP surface, which every client already speaks. The MCP tools exist for what
HTTP cannot express: browsing the catalog with structured metadata, and asking
the router to explain a decision without making the call.

MCP-aware agents discover both through the standard MCP advertising mechanism.

---

## MNEMOS feedback loop

Every routing decision + outcome lands in MNEMOS as a structured memory:

```python
create_memory({
    "category": "pantheon_routing",
    "content": json.dumps({
        "request_id": "...",
        "tenant_user_id": "alice",
        "alias_or_model": "auto:reasoning",
        "resolved_to": "llama-4-405b",
        "outcome": "success",
        "latency_ms": 2400,
        "tokens_in": 1200,
        "tokens_out": 380,
        "cost_usd": 0.012,
        "error_class": null,
    }),
    "namespace": "pantheon",
    "owner_id": "system:pantheon",
    "metadata": {
        "pantheon_version": "0.2",
        "session_id": "session-123",
        "usage_tier": "agentic_ok",
        "resolved_to": "llama-4-405b",
        "outcome": "success",
        "latency_ms": 2400,
        "cost_usd": 0.012,
    },
})
```

The routing policy queries this rolling window at decision time:

```sql
SELECT metadata->>'resolved_to' AS backend,
       AVG((metadata->>'latency_ms')::FLOAT) AS avg_latency_ms,
       SUM(CASE WHEN metadata->>'outcome' = 'error' THEN 1 ELSE 0 END)::FLOAT
         / COUNT(*)::FLOAT AS error_rate,
       AVG((metadata->>'cost_usd')::FLOAT) AS avg_cost
FROM memories
WHERE category = 'pantheon_routing'
  AND created > NOW() - INTERVAL '15 min'
  AND metadata->>'resolved_to' = ANY($candidate_list)
GROUP BY backend
```

Result: **the routing improves with use, automatically.** Backends that have been winning get more traffic; ones that are degraded shed load before the catalog's `health` field flips. This is the key differentiator from LiteLLM / Portkey, which use static config.

The durable audit destination is the `pantheon_routing_audit` table rather than
the memory store. mnemos-core ships that migration for every supported backend
(PostgreSQL, SQLite, Oracle, Db2), so the audit trail survives on an install
whose memory writes are disabled or failing. The memory write is best-effort and
feeds the rolling-window policy above; the NATS publish and its optional
consumer are what move a routing event into the audit table.

---

## Adoption path

PANTHEON is additive. It adds an endpoint; it removes none, and existing
per-provider client configs keep working the whole way through.

1. Install the add-on (`mnemos-core[pantheon]`) and enable the service, then run
   the gateway with an empty or seed catalog.
2. Run the catalog sync once so `/v1/models` reflects real pricing and
   capability metadata.
3. Point one low-stakes client at PANTHEON. Everything else stays on its old
   config.
4. Add providers as catalog entries plus keys. Each becomes routable at the next
   sync with no client change.
5. Tag restricted providers `usage_tier: consultation_only` so the cap and the
   agentic-mode filter apply from the moment they appear.
6. Migrate remaining clients by replacing their per-provider sections with one
   PANTHEON base URL and token.

---

## Packaging and deployment

### Distribution

PANTHEON is a namespace distribution, `mnemos-pantheon`, occupying
`mnemos.domain.pantheon.*`. mnemos-core declares it as the `pantheon` extra and
holds only the seams:

| Seam in mnemos-core | What it does |
|---|---|
| `mnemos/api/main.py` | mounts `mnemos.api.routes.pantheon` when the `pantheon` service is enabled and the distribution is importable |
| `mnemos/mcp/tools/models.py` | the two MCP tool wrappers, each guarded by an extra check and a lazy import |
| `mnemos/core/config.py` | the `MNEMOS_PANTHEON_*` settings surface — caps, policy weights, timeouts, rate limit, catalog cache paths, passthrough, audit queue |
| `mnemos/core/services.py` | `pantheon` and `pantheon_routing_audit_consumer` service entries |
| `mnemos/db_migrations/` | `pantheon_routing_audit` schema for PostgreSQL, SQLite, Oracle, and Db2 |
| `mnemos/tools/refresh_pantheon_catalog.py` | console-script shim with an actionable message when the add-on is missing |

Absent the extra, every seam degrades to nothing: routes are not mounted, MCP
tools are filtered out of the advertised list, the refresh shim exits 2 with an
install hint. Strict layering turns those silent skips into a loud startup
failure for operators who would rather not discover a missing add-on at request
time.

### Service enablement

PANTHEON is **off in every default profile**, including `server`. It is a niche
model-proxy surface rather than required substrate, so operators opt in with the
`pantheon` component selection (which also enables the routing-audit consumer)
or the `full` bundle, or by setting `MNEMOS_PANTHEON_ENABLED` directly.

### Gateway process

The gateway app is `mnemos.api.pantheon_shadow:app`, run single-process for dev

```sh
uvicorn mnemos.api.pantheon_shadow:app --port 4101
```

and as a worker pool in production, behind a reverse proxy that owns the stable
public VIP and forwards to loopback:

```sh
gunicorn mnemos.api.pantheon_shadow:app \
  -k uvicorn.workers.UvicornWorker -w "${WEB_CONCURRENCY}" -b 127.0.0.1:4110
```

`deploy/pantheon/` carries the ops artifacts for that topology:
`pantheon-gunicorn.sh` (validated launcher), `pantheon-gunicorn.env.example`
(environment template), `pantheon-gateway.service` (systemd unit), a
review-only reverse-proxy site snippet, and a README with the VIP-stable
cutover and one-line rollback.

**Shared state is a precondition for a multi-worker pool.** Cooldown, breaker,
and request-limit state are process-local by default, which is wrong the moment
the proxy fans out across workers. Before running more than one worker: point
every worker at the same NATS bus, keep
`MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT` identical across the pool, and move HTTP
rate-limit counters off `memory://` to a shared `limits` backend. A
single-worker capacity ceiling is a capacity finding, not the target topology.

### Smoke test

`scripts/pantheon_shadow_smoke.py` exercises a running gateway over the
OpenAI-compatible surface: health, chat completion, tool-call passthrough
(asserting the tool call survives with its id, name, and parseable JSON
arguments intact), and `/responses`-style routing for models that require it.
Run it against the shadow port before any cutover, and against the pool after.

---

## Deliberately not in scope

- **Cross-fleet PANTHEON** — routing to a peer instance as a backend. A
  federation story, not a gateway one.
- **Token-by-token streaming over the bus.** Direct SSE forwarding is
  sufficient and simpler.
- **Response caching** (content-hash lookups). Real wins for reasoning
  workloads, but the invalidation rules have to be right first.
- **Mid-stream cost kill switch.** Enforcement happens pre-dispatch and at the
  limiter; killing a response in flight buys little and complicates every
  adapter.
