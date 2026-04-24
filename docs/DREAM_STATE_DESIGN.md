# MNEMOS Dream State — Design Scoping

**Status**: Draft (v3.3 / v3.4 candidate)
**Owner**: Jason Perlow (`jperlow@gmail.com`)
**Origin**: Dreamt up 2026-04-23, sprung from MEMORY_PSYCHOLOGY.md's Jungian framing:

> "The shadow is the unknown dark side of the personality... One does not
> become enlightened by imagining figures of light, but by making the
> darkness conscious."

This document scopes a **derivative-ideas layer** that sits alongside the
canonical memory store: MNEMOS, while idle (or on demand), runs seeded
ideation passes over existing memories and persists the output as a
distinct kind of record — *dreams* — which are surfaceable to agents,
labeled for provenance, organized by category, and promotable to facts
only through explicit review.

---

## 1. Why this is not already covered by existing compression

The v3.1 compression contest (LETHE / ANAMNESIS / ALETHEIA) and the
APOLLO program (v3.2–v3.4) all do **convergent** work: take a memory,
produce a compacter, denser, or schema-typed form of *that same memory*.
Outputs are faithful; the judge-LLM scores fidelity against the root.

Dream state is **divergent**: take N memories, produce *new content*
that didn't exist before — connections, hypotheses, extrapolations,
contradictions — content whose fidelity to any single seed is explicitly
not the point. This is generative work. It cannot share the compression
contest's judge-model because there is no single ground truth to score
against.

Every other MNEMOS subsystem treats "memory" as received-and-recorded.
Dream state is the first subsystem that treats memory as a *substrate
for new thought*.

---

## 2. Surfaceability — the core principle

**Dreams must be visible to agents performing retrieval.** This is the
single most important design constraint and the one that distinguishes
MNEMOS's dream state from a "quarantine" pattern.

Rationale: the Jungian framing that motivated this feature explicitly
argues that the value is in *making the darkness conscious*. A dream
layer that is default-hidden from retrieval defeats its own purpose —
the ideas never reach the agent that could act on them, and the store
becomes a private diary nobody reads.

Therefore:

- Dreams are **returned** by `POST /memories/search` and by the
  gateway's `_search_mnemos_context` inject path.
- Dreams are **labeled** in the response envelope so the consuming
  agent can tell the register: this is speculative, generated, with
  provenance back to seed memories.
- Dreams are **filterable** for callers that want them excluded
  (`include_dreams=false` on the search API, `MNEMOS_GATEWAY_INCLUDE_DREAMS`
  env var for the OpenAI-compat gateway).
- Dreams are **never** silently folded into the facts stream. The
  distinction between recalled fact and dreamt idea is preserved all
  the way to the LLM prompt.

The contract for a retrieving agent reads: *here is what you recall;
here is what you've dreamt adjacent to this topic; act accordingly.*

---

## 3. Category organization

Dreams inherit from the seed memories' category axis **and** carry their
own kind taxonomy. Both dimensions are indexed.

### 3.1 Inherited category

A dream derived from memories in category `projects` has `category='projects'`.
This means:

- `GET /memories?category=projects` returns facts + dreams (both labeled).
- The existing per-category search, export, and namespace enforcement
  applies uniformly to dreams without new plumbing.
- Operators already familiar with the category taxonomy don't learn a
  second one to find the dream layer.

When a dream is seeded from memories in **multiple** categories, the
dream row carries the *most common* category among its seeds, with
`category_mix TEXT[]` listing all of them — so a dream bridging
`projects` and `people` is findable under either.

### 3.2 Dream kind — orthogonal taxonomy

The dream itself has a `kind` that describes *what sort of thought it
is*, not what topic:

| kind            | meaning                                                   |
|-----------------|-----------------------------------------------------------|
| `connection`    | Two or more seeds noted as related; new edge, not new node |
| `extrapolation` | "If these facts hold, then X also plausibly holds"         |
| `hypothesis`    | A testable claim derived from seeds                       |
| `question`      | A gap the seeds imply but don't answer                    |
| `synthesis`     | A higher-order summary across seeds                        |
| `contradiction` | Seeds disagree; dream names the conflict                  |

This lets an agent query *by mode of thought* —
"show me all hypotheses about the compression pipeline" —
independently of topic. First-class column, btree-indexed.

---

## 4. Schema

```sql
-- db/migrations_v3_3_dreams.sql (sketch)

CREATE TABLE IF NOT EXISTS memory_dreams (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Ownership axis — mirrors memories table
    owner_id             TEXT NOT NULL DEFAULT 'default',
    namespace            TEXT NOT NULL DEFAULT 'default',

    -- Categorical axes
    category             TEXT NOT NULL,                         -- inherited, dominant
    category_mix         TEXT[] NOT NULL DEFAULT '{}',          -- all seeds' categories
    kind                 TEXT NOT NULL,                         -- connection|extrapolation|...

    -- Content
    content              TEXT NOT NULL,                         -- the dream itself
    summary              TEXT,                                  -- one-line for UI / short retrievals
    embedding            vector(768),                           -- same model as memories

    -- Provenance
    source_memory_ids    TEXT[] NOT NULL,                       -- seeds
    generator_model      TEXT NOT NULL,                         -- e.g. 'gpt-5.2-chat-latest'
    generator_provider   TEXT NOT NULL,                         -- e.g. 'openai'
    dream_session_id     UUID NOT NULL,                         -- groups a batch
    seed_strategy        TEXT NOT NULL,                         -- 'random' | 'cluster_gap' | ...

    -- Lifecycle
    status               TEXT NOT NULL DEFAULT 'active',        -- active|promoted|archived|rejected
    promoted_memory_id   TEXT,                                  -- set when promoted to a real memory
    review_notes         TEXT,

    -- Confidence + scoring
    novelty_score        NUMERIC,                               -- optional: judge-LLM rating
    coherence_score      NUMERIC,                               -- optional: judge-LLM rating

    -- Timestamps
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_at          TIMESTAMPTZ,
    archived_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_dreams_owner_ns_cat
    ON memory_dreams (owner_id, namespace, category);
CREATE INDEX IF NOT EXISTS ix_dreams_kind
    ON memory_dreams (kind);
CREATE INDEX IF NOT EXISTS ix_dreams_status
    ON memory_dreams (status);
CREATE INDEX IF NOT EXISTS ix_dreams_session
    ON memory_dreams (dream_session_id);
CREATE INDEX IF NOT EXISTS ix_dreams_seeds_gin
    ON memory_dreams USING gin (source_memory_ids);

CREATE TABLE IF NOT EXISTS memory_dream_sessions (
    id                   UUID PRIMARY KEY,
    owner_id             TEXT NOT NULL,
    namespace            TEXT NOT NULL,
    trigger              TEXT NOT NULL,                         -- 'scheduled' | 'manual' | 'gpu_idle'
    seed_strategy        TEXT NOT NULL,
    seed_count           INT  NOT NULL,
    dream_count          INT  NOT NULL DEFAULT 0,
    generator_providers  TEXT[] NOT NULL,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at          TIMESTAMPTZ,
    error                TEXT
);
```

A separate `memory_dream_queue` mirrors the compression queue shape
(status: pending → running → done/failed, dequeue with
`FOR UPDATE SKIP LOCKED`, stranded-running sweep reuses the same
`_sweep_stale_running` pattern in `compression/worker_contest.py`).

---

## 5. Seed strategies

The strategy determines which memories feed a given dream invocation.
All strategies run as pure SQL plus light post-processing.

### 5.1 `random`
Baseline. Uniform-random N memories within an owner's namespace,
optionally category-filtered.

### 5.2 `cluster_gap`
Find pairs of memories whose embeddings are close but which *don't*
share a KG triple. These are "structurally adjacent, logically
disconnected" — the most fertile ground for a dream that produces a
genuine connection.

```sql
-- sketch: nearest-neighbor pairs with no edge
SELECT m1.id, m2.id, m1.embedding <-> m2.embedding AS dist
FROM memories m1
JOIN memories m2 ON m1.id < m2.id
              AND m1.owner_id = m2.owner_id
              AND m1.namespace = m2.namespace
LEFT JOIN kg_triples t
       ON (t.subject_id = m1.id AND t.object_id = m2.id)
       OR (t.subject_id = m2.id AND t.object_id = m1.id)
WHERE t.id IS NULL
  AND m1.embedding <-> m2.embedding < 0.30
ORDER BY dist ASC
LIMIT 20;
```

### 5.3 `co_access`
Memories retrieved together in the last N sessions (requires a
session-retrieval log, which the gateway already has at
`session_memory_injections`). High co-access frequency without an
explicit edge is another high-value seed.

### 5.4 `orphan`
Memories with no KG edges, no retrieval hits, no derivative dreams.
Targets the long tail — gives each memory at least one pass.

### 5.5 `category_scoped`
Operator-specified: "dream within `projects`" or "within `people`".
Useful for targeted ideation.

### 5.6 `recent`
Memories created in the last window (hours / days). Seeds a
"what-have-I-learned-lately" reflection pass.

---

## 6. Ideation pipeline

Dream generation reuses GRAEAE as the ideation engine, but with a
divergent-reasoning prompt and **consensus disabled**. The value of a
dream pass is in the *spread* of perspectives across muses, not the
majority vote.

### Request shape

```
POST /graeae/consult
{
  "prompt": "<dream ideation prompt with seed content inline>",
  "task_type": "reasoning",
  "mode": "external",
  "selection": { "providers": ["openai", "google", "anthropic_proxy"] },
  "consensus": false
}
```

Each muse response becomes one candidate dream. Post-processing:

1. **Kind classification** — lightweight LLM or rule-based classifier
   assigns `kind` (connection/extrapolation/hypothesis/question/...).
2. **Novelty check** — embed the candidate; reject if cosine similarity
   to any existing memory OR existing dream > 0.92 (it's a paraphrase,
   not a dream).
3. **Coherence check** — optional: judge-LLM rates whether the dream
   is internally coherent. Low-coherence dreams still persist, flagged
   `coherence_score < threshold`, so they're visible but discountable.
4. **Persist** as `memory_dreams` row with full provenance.

### Cost control

Dreaming is expensive (multiple LLM calls per seed set). Guardrails:

- Per-session cap: `MNEMOS_DREAM_MAX_PER_SESSION` (default 20 dreams).
- Per-day budget: `MNEMOS_DREAM_DAILY_BUDGET_USD` (default $1.00).
- Idle trigger: dreams only run when `GPUGuard` is HEALTHY and
  queue-waiting time for compression is 0 — dreams never starve the
  convergent pipeline.

---

## 7. Retrieval contract

This is the mechanical implementation of §2 (surfaceability).

### 7.1 Search API

```
POST /memories/search
{
  "query": "...",
  "limit": 10,
  "include_dreams": true,       // default true — dreams ARE surfaced
  "dream_kinds": null,          // null = all kinds; or ["hypothesis", "question"]
  "category": null              // inherits standard filter; applies to both
}
```

Response envelope distinguishes:

```json
{
  "facts": [ { "id": "...", "content": "...", "kind": "fact",  "score": 0.83 }, ... ],
  "dreams": [ { "id": "...", "content": "...", "kind": "hypothesis",
                "source_memory_ids": ["...", "..."], "generator_model": "...",
                "score": 0.79 }, ... ]
}
```

Callers that don't want dreams set `include_dreams=false` and get the
flat pre-v3.3 shape under `results`.

### 7.2 Gateway inject path

`_search_mnemos_context` in `api/handlers/openai_compat.py` gains two
sections in the injected context block:

```
## Recall
- [facts] ...
- [facts] ...

## Adjacent (dreamt)
- [hypothesis, from memories A, B] ...
- [connection, from memories C, D, E] ...
```

The LLM sees the labeling. This is the *making-conscious* act: the
model is told explicitly "this is speculative material derived from
your stored memories, not a recalled fact."

Env var `MNEMOS_GATEWAY_INCLUDE_DREAMS=false` disables injection
entirely for deployments that want dreams stored-but-not-exposed to the
inference path (e.g. corporate tenants who haven't opted in).

### 7.3 MCP

The `search_memories` MCP tool gains a `dream_mode` parameter:
`"include"` (default), `"exclude"`, or `"only"`. Claude's side of the
MCP contract stays source-of-truth for naming.

---

## 8. Promotion workflow

A dream becomes a fact only through explicit action. Three paths:

### 8.1 Manual operator review

`POST /v1/dreams/{id}/promote` — admin/root only. Converts the dream
row to:

- A new `memories` row with the dream's `content` (or a lightly-edited
  version) and `origin='dream:<dream_id>'`.
- A set of `kg_triples` linking the new memory to each
  `source_memory_ids` entry via a `derived_from` predicate.
- `memory_dreams.status = 'promoted'`, `promoted_memory_id` stamped.

### 8.2 User-in-the-loop

For dreams owned by a per-user namespace, the web UI (or MCP) offers
the user an accept/reject action on their own dreams. Promotion scope
is their own namespace only; they cannot promote dreams from another
user.

### 8.3 Agent-driven (opt-in, v3.4)

An agent consuming dreams via retrieval can signal
`POST /v1/dreams/{id}/acknowledge` with `accepted=true|false`. This
doesn't promote — it feeds a signal back into the ranking used by
future dream passes. Repeatedly-rejected kinds/strategies get
down-weighted.

---

## 9. Scheduling

Two triggers:

### 9.1 Manual

`POST /admin/dreams/run` — operator-triggered batch, parameters match
`memory_dream_sessions` row.

### 9.2 Idle-driven

A scheduler (`dream_scheduler.py`, lifespan-managed next to
`distillation_worker`) wakes on interval (default 30 min) and runs a
session if:

- GPUGuard is HEALTHY
- `memory_compression_queue` has zero `pending` rows older than 60s
- Per-day budget not exhausted
- Last dream session > `MNEMOS_DREAM_MIN_INTERVAL_MINUTES` ago
  (default 60)

The session picks a seed strategy according to a rotation
(random → cluster_gap → co_access → orphan → recent → …) so the
dream surface grows evenly.

---

## 10. Safety and containment

Even with surfaceability as a principle, a few invariants must hold:

1. **Provenance is mandatory.** No dream row can exist without
   `source_memory_ids` populated. Retrievers can always trace back.
2. **Kind labels are required** on every retrieval response.
   The gateway MUST NOT inject a dream as an unlabelled "fact."
3. **Dreams don't seed dreams.** Seed strategies SELECT from `memories`,
   never from `memory_dreams`. Otherwise drift compounds.
4. **Namespace isolation**: dream surfaceability respects per-user
   namespace the same way facts do. A user never sees another user's
   dreams.
5. **Federation**: dreams are NOT federated by default. Each instance
   dreams for itself; cross-instance dream exchange is a separate
   feature (plausibly v4.x) that would need its own trust model.

---

## 11. Open questions

- **Judge-LLM for coherence**: worth the latency and cost, or trust
  the muse's output? Leaning "optional, off by default until we
  measure drift."
- **Dream decay**: should inactive dreams auto-archive after N days?
  Helps keep the surface fresh vs. cluttered. Leaning yes, 90 days
  default, `active → archived` with a reversal API.
- **KG triple generation at dream-time**: should each `connection`
  dream *also* emit a provisional triple to `kg_triples` with
  `confidence='speculative'`? Would let graph traversal see the
  speculative edges. Plausible v3.4 extension.
- **APOLLO-encoded dreams**: does the dense-format engine apply to
  dream content, or only to facts? Probably only facts — dreams are
  explicitly not schema-typed. Confirm with APOLLO's final schema list.
- **Per-memory dream quota**: should a given memory be allowed to
  appear in at most N dreams? Prevents one heavily-seeded memory
  from dominating the dream surface. Probably yes, N=10.

---

## 12. Roadmap slot

**v3.3** — foundations:
- Migrations, worker, queue, manual trigger, core schema.
- Seed strategies: `random`, `category_scoped`.
- Retrieval surfacing: search API `facts`/`dreams` split.
- Manual promotion endpoint.
- No idle scheduler yet (manual only).

**v3.4** — depth:
- Seed strategies: `cluster_gap`, `co_access`, `orphan`, `recent`.
- Idle scheduler.
- Gateway inject-path "Adjacent (dreamt)" section.
- Agent-driven acknowledgement feedback loop.
- KG triple generation for `connection` kind.

This is additive to the APOLLO program, not in conflict with it:
APOLLO compresses facts for LLM-to-LLM wire use; dream state generates
new material. A mature MNEMOS eventually has both — facts compressed
via APOLLO for efficient retrieval, dreams surfaced alongside for
generative context.

---

*Draft status — refinements expected after the first implementation
slice lands and real dreams are inspected.*
