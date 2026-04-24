# MNEMOS Dream State — Design Scoping

**Status**: Draft (v3.3 / v3.4 candidate)
**Owner**: Jason Perlow (`jperlow@gmail.com`)
**Origin**: Dreamt up 2026-04-23, sprung from MEMORY_PSYCHOLOGY.md's Jungian framing:

> "The shadow is the unknown dark side of the personality... One does not
> become enlightened by imagining figures of light, but by making the
> darkness conscious."

This document scopes a **derivative-ideas layer** that sits alongside the
canonical memory store: MNEMOS, while idle (or on demand), runs seeded
ideation passes over existing memories and persists the output as
**dream branches in the existing memory DAG** — surfaceable to agents,
labeled for provenance, organized by category, and promotable to facts
only through explicit merge.

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
single most important design constraint.

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

## 3. Architectural pattern — dreams as DAG branches

Dreams do **not** get their own table. They live in the existing
`memory_versions` DAG as branches, following the same pattern the
APOLLO roadmap already establishes for `distilled` and `narrated`
branches.

### 3.1 Why DAG branches are the right substrate

| Property                          | Separate `memory_dreams` table | `memory_versions` branches   |
|-----------------------------------|--------------------------------|------------------------------|
| Provenance to seed memory         | FK column                      | **`parent_version_id` — already there** |
| Multi-seed (N→1) provenance       | TEXT[] array                   | **`merge_parents UUID[]` — already there, octopus-merge shape** |
| Content-addressed / tamper-evident | New column                     | **`commit_hash` — already enforced** |
| Discoverability per memory        | New index                      | **`memory_branches` WHERE name LIKE 'dream/%'** |
| Namespace + owner enforcement     | Re-implement                   | **Inherits from the version row** |
| Promotion workflow                | Custom endpoint                | **Merge commit — git-native semantics** |
| APOLLO / narrated integration     | Parallel subsystem             | **Single DAG, one lineage graph**      |

The DAG path is free. The table path duplicates half the DAG badly.

### 3.2 Dreams as "comments or RFCs"

The mental model: a dream is an **RFC against a memory**. In git terms:

- A memory is a mainline commit.
- A dream is an experimental branch proposing an elaboration —
  a comment on the commit, a proposed change, a hypothesis to discuss.
- Agents reading the memory can see the attached dreams the same way
  GitHub shows commit-attached comments.
- Accepting a dream is a merge commit back to main. Rejecting it
  archives the branch.

This is a pattern the system already understands. Nothing new to
invent — dreams just extend the branch taxonomy.

### 3.3 Branch naming convention

Dream branches follow the pattern `dream/<kind>[/<session-short>]`:

| Branch name                      | Meaning                                  |
|----------------------------------|------------------------------------------|
| `dream/connection`               | Edge proposal between 2+ memories        |
| `dream/extrapolation`            | "If these hold, then X also plausibly holds" |
| `dream/hypothesis`               | Testable claim derived from seeds        |
| `dream/question`                 | Gap the seeds imply but don't answer     |
| `dream/synthesis`                | Higher-order summary across seeds        |
| `dream/contradiction`            | Seeds disagree; dream names the conflict |

Session disambiguation is optional:
`dream/hypothesis/8f3c9a1` for multi-dream sessions per memory.

### 3.4 Single-parent vs multi-parent dreams

A dream that elaborates one seed memory (extrapolation, hypothesis,
question) lands as a single-parent branch:

```
main (memory M)  ◀── parent_version_id ◀── dream/hypothesis (new version row)
```

A dream that connects N memories is an **octopus merge** —
`parent_version_id = NULL`, `merge_parents = [v1, v2, v3]`, one for
each seed's current main HEAD. This is exactly `git merge -Xoctopus`
for N-way joins: a first-class multi-parent node with no primary
lineage. Equivalent of the `connection` and `synthesis` kinds.

The dream row is stored under a synthetic `memory_id` so it has
stable identity (`dream_<uuid>` or derived from hash of seed IDs),
but the DAG makes the real shape: a commit with N parents pointing
into N different memory histories.

---

## 4. Schema changes

Minimal. Two additive columns on `memory_versions`, one convention on
`memory_branches`, one new worker table.

### 4.1 `memory_versions` — two additive columns

```sql
-- db/migrations_v3_3_dreams.sql (sketch)

BEGIN;

-- Dream-specific metadata on the version row. NULL on non-dream branches.
ALTER TABLE memory_versions
    ADD COLUMN IF NOT EXISTS dream_kind          TEXT,   -- connection|hypothesis|...
    ADD COLUMN IF NOT EXISTS dream_session_id    UUID,   -- groups batch
    ADD COLUMN IF NOT EXISTS dream_generator     TEXT,   -- 'openai:gpt-5.2-chat-latest'
    ADD COLUMN IF NOT EXISTS dream_status        TEXT
        CHECK (dream_status IN ('active','promoted','archived','rejected'));

CREATE INDEX IF NOT EXISTS idx_mv_dream_kind       ON memory_versions(dream_kind) WHERE dream_kind IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mv_dream_status     ON memory_versions(dream_status) WHERE dream_status IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mv_dream_session    ON memory_versions(dream_session_id) WHERE dream_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mv_branch_prefix    ON memory_versions(branch text_pattern_ops) WHERE branch LIKE 'dream/%';

COMMIT;
```

No new table for the dreams themselves. `memory_versions` carries
them, content-addressed via the existing `commit_hash` column.

### 4.2 `memory_dream_queue` — mirror of the compression queue

Dream generation is asynchronous work. Reuse the compression queue
shape one-for-one so the stranded-running sweep landed today
(`b8f2ab9`, `_sweep_stale_running` in `compression/worker_contest.py`)
applies to the dream worker for free:

```sql
CREATE TABLE IF NOT EXISTS memory_dream_queue (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seed_strategy     TEXT NOT NULL,
    seed_memory_ids   TEXT[] NOT NULL,
    owner_id          TEXT NOT NULL,
    namespace         TEXT NOT NULL,
    generator_providers TEXT[] NOT NULL,
    priority          INT  NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','running','done','failed')),
    attempts          INT  NOT NULL DEFAULT 0,
    max_attempts      INT  NOT NULL DEFAULT 3,
    enqueued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    error             TEXT,
    result_version_ids UUID[]
);
```

### 4.3 `memory_branches` — no schema change

The existing table already tracks branch HEADs per memory.
"Find all dreams against memory M" becomes:

```sql
SELECT name, head_version_id
  FROM memory_branches
 WHERE memory_id = $1
   AND name LIKE 'dream/%';
```

For multi-parent (octopus) dreams, the dream version row is reachable
from each seed memory by traversing `merge_parents`.

---

## 5. Category organization

Dreams inherit from the seed memories' category axis **and** carry a
kind taxonomy (already spelled out in §3.3 as the branch name).

### 5.1 Inherited category

A dream branch inherits `category` from its parent version. For
single-parent dreams this is trivially the seed's category. For
multi-parent (octopus) dreams, the dream version row carries the
**dominant** category (most common across seeds) in `category`, and
the full set in `metadata.category_mix`. This keeps
`GET /memories?category=X` working uniformly — it surfaces main-branch
facts plus any dream branches whose dream version row carries that
category.

### 5.2 Kind as branch name

The `kind` lives in two places for ergonomics:

- `dream_kind` column (indexed, fast query by mode of thought)
- `branch` prefix (human-readable in the DAG CLI/UI)

Both are kept in sync by the dream-writer; the column wins on
disagreement.

---

## 6. Seed strategies

Unchanged from the original draft — pure SQL passes that pick which
memories to feed into a dream generation invocation.

### 6.1 `random`
Baseline. Uniform-random N memories within an owner's namespace,
optionally category-filtered.

### 6.2 `cluster_gap`
Find pairs of memories whose embeddings are close but which *don't*
share a KG triple. "Structurally adjacent, logically disconnected" —
the most fertile ground for a `dream/connection`.

```sql
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

### 6.3 `co_access`
Memories retrieved together in the last N sessions
(`session_memory_injections` already has this). High co-access without
an explicit edge is a strong seed.

### 6.4 `orphan`
Memories with no KG edges, no retrieval hits, no attached dreams.
Gives the long tail at least one pass.

### 6.5 `category_scoped`
Operator-specified: "dream within `projects`."

### 6.6 `recent`
Memories created in the last window. Seeds a
"what-have-I-learned-lately" reflection pass.

---

## 7. Ideation pipeline

Dream generation reuses GRAEAE as the ideation engine, but with a
divergent-reasoning prompt and **consensus disabled**. The value is in
the *spread* of perspectives across muses, not the majority vote.

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
   assigns `dream_kind`.
2. **Novelty check** — embed the candidate; reject if cosine similarity
   to any existing memory OR existing dream > 0.92 (paraphrase, not
   dream).
3. **Coherence check** — optional judge-LLM rating, stored on the
   version row's `metadata` jsonb. Low-coherence dreams still persist
   with a flag so they're discountable, not missing.
4. **Persist** as a new `memory_versions` row with the appropriate
   `branch`, `parent_version_id` / `merge_parents`, `dream_*` columns,
   and `commit_hash` auto-computed.
5. **Update** `memory_branches` — insert or update HEAD for the dream
   branch on each seed memory.

### Cost control

- Per-session cap: `MNEMOS_DREAM_MAX_PER_SESSION` (default 20).
- Per-day budget: `MNEMOS_DREAM_DAILY_BUDGET_USD` (default $1.00).
- Idle trigger: dreams only run when `GPUGuard` is HEALTHY and
  `memory_compression_queue` has zero pending rows older than 60s.
  Dreams never starve the convergent pipeline.

---

## 8. Retrieval contract

Mechanical implementation of §2 (surfaceability).

### 8.1 Search API

```
POST /memories/search
{
  "query": "...",
  "limit": 10,
  "include_dreams": true,    // default true — dreams ARE surfaced
  "dream_kinds": null,       // null = all; or ["hypothesis","question"]
  "category": null
}
```

Response envelope distinguishes facts from dreams:

```json
{
  "facts": [
    { "id": "M1", "content": "...", "kind": "fact", "score": 0.83 }
  ],
  "dreams": [
    {
      "version_id": "V17",
      "branch": "dream/hypothesis",
      "dream_kind": "hypothesis",
      "content": "...",
      "parents": ["V3-main-of-M1", "V7-main-of-M5"],  // seed memory HEADs
      "generator": "openai:gpt-5.2-chat-latest",
      "score": 0.79
    }
  ]
}
```

Dreams reference their parents by version ID. The caller can walk the
DAG to trace any dream back to its seeds — which are real memories —
using the existing `/v1/dag/...` endpoints.

Callers that don't want dreams set `include_dreams=false` and get the
flat pre-v3.3 shape under `results`.

### 8.2 Gateway inject path

`_search_mnemos_context` in `api/handlers/openai_compat.py` gains a
second section:

```
## Recall
- [M1] ...
- [M3] ...

## Adjacent (dreamt)
- [hypothesis, from M1+M5] ...
- [connection, from M2+M7+M9] ...
```

The LLM sees the labeling and the provenance. This is the
*making-conscious* act: the model is told explicitly *this is
speculative material derived from stored memories, not a recalled
fact.*

Env var `MNEMOS_GATEWAY_INCLUDE_DREAMS=false` disables injection for
deployments that want dreams stored-but-not-exposed to the inference
path.

### 8.3 MCP

The `search_memories` MCP tool gains a `dream_mode` parameter:
`"include"` (default), `"exclude"`, or `"only"`.

### 8.4 DAG endpoints

The existing DAG endpoints (`/v1/memories/{id}/branches`,
`/v1/memories/{id}/versions`, `/v1/versions/{id}`) gain a
`kind='dream'` response field. "Show me the dream RFCs attached to
memory M" is just `GET /v1/memories/M/branches?prefix=dream/` — no new
endpoint.

---

## 9. Promotion workflow — dream as RFC, merge as acceptance

A dream becomes a fact only through explicit action. Three paths, all
expressed as DAG operations.

### 9.1 Manual operator review — merge

`POST /v1/dreams/{version_id}/promote` — admin/root only. The
operation is literally a merge commit:

- For single-parent dreams (extrapolation, hypothesis, question,
  synthesis): merge dream branch into main. New main version has
  `parent_version_id` = previous main HEAD, `merge_parents = [dream]`,
  effectively folding the dream content into the seed memory's
  mainline history.
- For multi-parent (connection, contradiction, N-way synthesis):
  either (a) create a *new memory* whose initial version carries
  `merge_parents = [dream_version, ...seed_HEADs]` (genuine new node),
  or (b) promote as a `kg_triple` with `confidence='from_dream'`
  linking the seed memories via a predicate named from the dream
  content.

Choice between (a) and (b) depends on whether the promoted content is
a new *claim* (a memory) or a new *relationship* (a triple). The API
infers from `dream_kind`: `connection` → triple, `synthesis` → memory.

`dream_status` transitions `active → promoted`. The dream version row
stays in place — promotion doesn't delete the RFC, it settles it.

### 9.2 User-in-the-loop

For dreams owned by a per-user namespace, the UI / MCP offers
accept/reject. Promotion scope is the user's own namespace.
Equivalent of merging your own PR.

### 9.3 Agent-driven (opt-in, v3.4)

An agent consuming dreams via retrieval can signal
`POST /v1/dreams/{version_id}/acknowledge` with `accepted=true|false`.
This doesn't promote — it feeds a signal into ranking for future dream
passes. Repeatedly-rejected kinds/strategies get down-weighted.

### 9.4 Rejection

`dream_status = 'rejected'`. Branch HEAD stays; the rejection is
itself content-addressed and auditable. Nothing gets deleted — the
RFC thread is preserved even when its conclusion is "no."

---

## 10. Scheduling

### 10.1 Manual
`POST /admin/dreams/run` — operator-triggered batch. Parameters:
`seed_strategy`, `count`, `category` filter, `generator_providers`.

### 10.2 Idle-driven
A scheduler (`dream_scheduler.py`, lifespan-managed alongside
`distillation_worker`) wakes on interval (default 30 min) and enqueues
a session if:

- GPUGuard is HEALTHY
- `memory_compression_queue` has zero `pending` rows older than 60s
- Per-day budget not exhausted
- Last dream session > `MNEMOS_DREAM_MIN_INTERVAL_MINUTES` ago
  (default 60)

The session rotates seed strategies
(random → cluster_gap → co_access → orphan → recent → category_scoped)
so the dream surface grows evenly.

---

## 11. Safety and containment

Even with surfaceability as a principle, invariants must hold:

1. **Provenance is mandatory.** No dream version row without a
   populated `parent_version_id` OR `merge_parents`. Retrievers can
   always trace back.
2. **Kind labels are required** on every retrieval response.
   The gateway MUST NOT inject a dream as an unlabelled fact.
3. **Dreams don't seed dreams.** Seed strategies SELECT from `memories`,
   never from `memory_versions WHERE branch LIKE 'dream/%'`. Otherwise
   drift compounds. The seed-strategy SQL excludes dream versions.
4. **Namespace isolation**: dream surfaceability respects per-user
   namespace the same way facts do, inherited from the version row.
5. **Federation**: dreams are NOT federated by default. Each instance
   dreams for itself; cross-instance dream exchange is a separate
   feature (plausibly v4.x) that would need its own trust model.
6. **DAG integrity**: dream versions are content-addressed
   (`commit_hash`), same as every other version. Tamper-evident by
   construction.

---

## 12. Open questions

- **Judge-LLM for coherence**: worth the latency and cost, or trust
  the muse's output? Leaning "optional, off by default until we
  measure drift."
- **Dream decay**: should inactive dreams auto-archive after N days
  (branch kept but `dream_status='archived'`)? Leaning yes, 90 days
  default, with a reversal API.
- **Speculative KG triples at dream-time**: should each
  `dream/connection` also emit a provisional `kg_triples` row with
  `confidence='from_dream'`? Would let graph traversal see speculative
  edges. Plausible v3.4 extension.
- **APOLLO-encoded dreams**: does the dense-format engine apply to
  dream content, or only to facts? Probably only facts — dreams are
  explicitly not schema-typed. Confirm with APOLLO's final schema list.
- **Per-memory dream quota**: should a given memory be allowed to
  carry at most N dream branches? Prevents one heavily-seeded memory
  from dominating. Probably yes, N=10.
- **Dream-of-a-dream merges**: if two dream branches against the same
  memory are mutually reinforcing, is there value in a dream-to-dream
  merge commit? Probably a v4.x question.

---

## 13. Roadmap slot

**v3.3** — foundations:
- Migration for `memory_versions` dream columns + `memory_dream_queue`.
- Dream worker + queue drain reusing `process_contest_queue` shape.
- Seed strategies: `random`, `category_scoped`.
- Retrieval surfacing: `POST /memories/search` facts/dreams split.
- Manual promotion via merge commit (single-parent dreams only).
- Manual trigger endpoint (`/admin/dreams/run`); no idle scheduler.

**v3.4** — depth:
- Seed strategies: `cluster_gap`, `co_access`, `orphan`, `recent`.
- Idle scheduler lifespan-managed with GPU-idle gate.
- Gateway inject-path "Adjacent (dreamt)" section.
- Agent-driven acknowledgement feedback loop.
- Octopus-merge promotion path (multi-parent dreams → new memory or
  speculative KG triple).
- Speculative KG triple emission for `dream/connection` kind.

Additive to the APOLLO program, not in conflict. APOLLO writes
`distilled` and `narrated` branches; dreams write `dream/<kind>`
branches. All land in the same DAG. A mature MNEMOS eventually has
both — facts compressed via APOLLO for efficient retrieval, dreams
branched from them for generative context.

---

*Draft status — refinements expected after the first implementation
slice lands and real dreams are inspected.*
