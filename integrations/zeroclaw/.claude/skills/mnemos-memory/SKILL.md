---
name: mnemos-memory
description: Read and write persistent memory via MNEMOS. Use when you need to recall prior decisions, infrastructure facts, project context, or established patterns across sessions, and when you have learned something new worth persisting (solved problem, validated pattern, architectural decision, team standard). Covers full-text and semantic search, create/update/delete memories, category-scoped queries, and knowledge-graph triples.
---

# MNEMOS Memory

MNEMOS is persistent memory shared across agent sessions. It is a network service, not a prompt. Reads and writes happen through the `mnemos` MCP server.

## When to search MNEMOS

Before you commit to an approach on any of these, run a quick `search_memories`:

- **Infrastructure** — ports, hostnames, service locations, credential storage layout
- **Decisions** — architecture choices, tradeoffs the team has already settled
- **Solutions** — workarounds for known bugs, fixes already applied elsewhere
- **Patterns** — reusable approaches already validated
- **Projects** — current project state, owners, milestones
- **Standards** — conventions, quality gates, review criteria

If the user's task looks like something that might have been solved before, search first. One query is cheap; wasted work is not.

## Search patterns

- **Start broad with full-text:** `search_memories(query="topic keywords", limit=10)` before narrowing
- **Filter by category** when you know the domain: `search_memories(query="auth flow", category="decisions")`
- **Use semantic search** for concept-level matches: `search_memories(query="...", semantic=true)`
- **Large context loads:** use `POST /v1/memories/rehydrate` directly via the REST API (this path is not an MCP tool in the current `mcp_server.py`). Chain multiple `search_memories` calls with a running token tally if you need to stay within MCP.

## When to store

Store after:

- Solving a non-trivial problem (store the solution and the failure mode, not just the fix)
- Making an architectural decision with rationale (use `category=decisions`; include *why* and tradeoffs)
- Discovering infrastructure shape (endpoint, port, auth scheme) that future sessions will need
- Establishing a pattern that other agents should follow (`category=patterns` or `standards`)

Do **not** store:

- Session transcripts (the session ingest endpoint handles that separately)
- Temporary state that will be stale in a day
- Information the user will see in the diff (recent commits, current branch)

## Minimum write shape

```
create_memory(
  content="<concise prose — one paragraph, lead with the claim>",
  category="<decisions|solutions|patterns|infrastructure|projects|standards>",
  subcategory="<optional narrower tag>",
  metadata={"source_agent": "<this skill's invocation context>"}
)
```

Content rules:

- Lead with the **claim or fact**, then supporting detail
- Keep under 500 words; if longer, split
- Include a **why** when you can — the memory is useless if a future reader can't judge whether it's still applicable

## Knowledge graph

For relational facts (`X owns Y`, `A depends on B`, `deadline of X is Y`), use `kg_create_triple` instead of a free-text memory. Triples support temporal validity windows — mark `valid_until` when a fact goes stale rather than deleting.

## Avoid poisoning

Before acting on a returned memory, check that it is still current:

- If it names a file path, verify the file exists
- If it names a port or hostname, verify reachability before trusting the endpoint
- If the user's current situation contradicts the memory, trust current reality and update the memory — don't act on stale data

## Tools (via MCP)

| Tool | Purpose |
|------|---------|
| `search_memories` | Full-text or semantic search, category/subcategory filters |
| `list_memories` | List memories with optional category / limit filters |
| `get_memory` | Fetch a single memory by ID |
| `create_memory` | Store a new memory |
| `bulk_create_memories` | Create many memories in one call (bulk ingest) |
| `update_memory` | Edit content or metadata |
| `delete_memory` | Remove a memory |
| `kg_create_triple` / `update_triple` / `delete_triple` | Knowledge-graph write ops |
| `kg_search` / `kg_timeline` | Knowledge-graph read ops (list triples, subject timeline) |
| `get_stats` | Category counts, compression statistics |

Rehydration (token-budgeted context load) is a REST-only endpoint (`POST /v1/memories/rehydrate`); not exposed as an MCP tool in the current `mcp_server.py`.
