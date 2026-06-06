# PINAKES — LLM Wiki Substrate

**Status:** design / partial-shipped. The derived-index + cross-link-graph
layers exist today (the `mnemos_sync` reference frontend writes them); the
wiki-semantic MCP surface, the review lifecycle, and the concept layer are
planned. This doc is the feature tree and the source-of-truth for what PINAKES
is and is not.
**Position in stack:** Above the `PersistenceBackend` (memory CRUD + KG triples +
vector search); beside the MCP server (PINAKES adds wiki-semantic verbs there);
below any file-aware agent or static-site renderer that consumes the corpus.
**Greek-name fit:** The *Pinakes* (Πίνακες, "tables") was Callimachus's catalog
of the Library of Alexandria — the first known library catalog. A wiki built on
Mnemosyne (memory) is exactly that: a living, cross-referenced catalog compiled
*over* raw memory, not a second copy of it.

---

## Mission

> Turn a corpus of raw notes into a self-maintaining, cross-linked wiki that any
> agent can read and reason over — with MNEMOS as the single backend and the
> single MCP front-end, no separate wiki server, no second database.

The pattern (after Andrej Karpathy's "LLM wiki" idea): treat raw notes as
*source material*, not the final artifact. A note is a claim; a wiki page is the
compiled, cross-linked explanation derived from one or many notes. An LLM does
the compilation. The wiki **persists and compounds** — every note added makes it
richer — and unlike a chatbot it does not forget.

PINAKES is the MNEMOS-side capability that makes this work without bolting on a
parallel stack:

- **One backend.** Pages live in MNEMOS as namespace-isolated memories; their
  `[[wikilinks]]` live as knowledge-graph triples. No SQLite concept registry,
  no separate vector DB — MNEMOS already has FTS, vector search, and a KG.
- **One front-end.** Agents talk to the wiki through the **MNEMOS MCP server**,
  the same transport they already use for memory. PINAKES adds wiki-semantic
  verbs there rather than shipping a second MCP server.
- **Markdown is source-of-truth.** MNEMOS holds a *derived, rebuildable* index.
  A full wipe + resync is always safe; nothing is authored only in MNEMOS.

---

## Architecture

```
  raw sources ──▶  ingest (LLM)  ──▶  wiki/*.md  ──▶  render (static site)
  (notes, PDFs,     [frontend]        on-disk          [frontend]
   docs, URLs)                        SOURCE-OF-TRUTH
                                          │
                                          │  mnemos_sync (derived, idempotent)
                                          ▼
                          ┌─────────────────────────────────────┐
                          │  MNEMOS  (PINAKES substrate)         │
                          │  · page  -> memory (namespace=corpus)│
                          │  · [[link]] -> KG triple links_to    │
                          │  · backlinks DERIVED at query time   │
                          │  · vector + FTS search over pages    │
                          └─────────────────────────────────────┘
                                          │
                                          │  MCP (wiki-semantic verbs)
                                          ▼
                       any file-aware agent (Claude, Codex, …)
```

**Two repos, one boundary.** PINAKES (this doc) is the **generic capability** in
MNEMOS. A *frontend* — the reference `llm-wiki` pipeline — owns the
corpus-specific ingest prompts, render targets, and content. The frontend is
swappable; PINAKES is the substrate every frontend shares. Corpus content never
lives in the MNEMOS repo; only the capability does.

---

## Feature tree

Legend: ✅ shipped · 🔨 in progress / partial · 📋 planned · (F) = frontend-side,
(M) = MNEMOS-side.

```
PINAKES — LLM Wiki Substrate
├── 1. Derived index over a markdown corpus
│   ├── 1.1 ✅ (M) Page → one memory, namespace-isolated per corpus
│   ├── 1.2 ✅ (M) Upsert keyed by stable slug, gated on content_hash
│   │         (unchanged pages cost one list call, zero writes)
│   ├── 1.3 ✅ (M) Reconciliation: page removed from disk → memory + edges deleted
│   ├── 1.4 ✅ (M) Transactional commit order (blank hash → edges → real hash)
│   │         so a mid-flight failure forces a clean redo, never a half-synced graph
│   └── 1.5 ✅ (F) Markdown is source-of-truth; index is fully rebuildable
├── 2. Cross-link knowledge graph
│   ├── 2.1 ✅ (M) [[slug]] → outbound `links_to` triple
│   ├── 2.2 ✅ (M) Backlinks DERIVED at query time (?object=slug) — no stored inverse,
│   │         so editing one page never drifts a neighbour's graph
│   ├── 2.3 ✅ (M) Edge sync as a delta (add wanted, collapse dup, drop unwanted)
│   └── 2.4 📋 (M) Typed edges beyond `links_to` (cites, supersedes, part_of)
├── 3. Wiki-semantic MCP surface  ← the current gap
│   ├── 3.1 📋 (M) namespace param on search_memory (today it can't reach a corpus)
│   ├── 3.2 📋 (M) read_article(slug) / get_by_slug
│   ├── 3.3 📋 (M) list_articles / search_articles (vector + FTS, scoped to corpus)
│   ├── 3.4 📋 (M) get_concept / neighbours (KG ?subject=) + trace_lineage (?object=)
│   ├── 3.5 📋 (M) list_sources (filter by source_type)
│   └── 3.6 📋 (M) answer_question (routed Q&A over the corpus)
├── 4. Ingest / compile pipeline
│   ├── 4.1 ✅ (F) LLM page generation, single-pass + map-reduce for large sources
│   ├── 4.2 ✅ (F) Per-page LLM one-sentence summaries (no blind char truncation)
│   ├── 4.3 ✅ (F) Mermaid diagram generation grounded only in stated relationships
│   ├── 4.4 🔨 (F) Source-intake connectors (pull external docs into raw/)
│   ├── 4.5 📋 (F) Incremental compile (only recompile pages tied to a changed source)
│   └── 4.6 📋 (F) Concept layer: synthesized concept pages above source pages,
│             dedup/merge across sources (the Karpathy "one article per concept")
├── 5. Review lifecycle
│   ├── 5.1 🔨 (M) content_hash exists; enforce hand-edit protection on resync
│   │         (never clobber a page edited since last compile)
│   ├── 5.2 📋 draft → verified → published states (frontmatter + index)
│   ├── 5.3 📋 confidence score per compiled draft + threshold publish
│   └── 5.4 📋 rejection feedback loop (reason injected into next compile)
├── 6. Render / consumption targets
│   ├── 6.1 ✅ (F) Static site (mkdocs): landing page, per-page, nav collapse
│   ├── 6.2 ✅ (F) Mind-map / graph view derived from the KG
│   └── 6.3 📋 (F) Agent-pack export (index + entry points for any file-aware agent)
└── 7. Trust / safety for external sources
    ├── 7.1 📋 prompt-injection hardening (nonce-delimited source blocks)
    ├── 7.2 📋 source trust-tiering + license-gated verbatim passage access
    └── 7.3 📋 review gate before an externally-sourced page is published
```

---

## Design decisions (locked)

- **Derived, not authoritative.** MNEMOS is a rebuildable index over on-disk
  markdown. This is what lets a corpus be re-synced, branched, or wiped without
  data loss, and why hand-edit protection (5.1) protects the *file*, not the
  memory.
- **Outbound-only edges, derived backlinks.** A page owns only its own outbound
  triples. Backlinks are a query, not stored state. This is the fix for the
  incremental-sync backlink-freeze class of bug — confirmed via GRAEAE
  multi-muse consult (2026-06-04).
- **MNEMOS is the only MCP front-end.** PINAKES does not ship a second MCP
  server. The synto/Karpathy "12 wiki tools" become a verb set *added to*
  MNEMOS's MCP, each a thin wrapper over REST endpoints that already exist
  (`/v1/memories?namespace=`, `/v1/kg/triples`). Rationale: agents already trust
  one MNEMOS endpoint; a corpus is just a namespace behind it.
- **Frontend/substrate split.** Corpus-specific ingest prompts, render config,
  and content stay in the frontend repo. Only the reusable capability is in
  MNEMOS, so the public project carries no deployment-specific content.

## Why on MNEMOS rather than a standalone tool

Comparable tools (synto, obsidian-llm-wiki, and the cluster of projects in the
Karpathy LLM-wiki gist) each ship their own store (SQLite concept registry),
their own query layer (an INDEX.json or a bolted-on vector DB), and their own
MCP server. MNEMOS already provides all three — namespace-isolated memory CRUD,
a knowledge graph, FTS + vector search, and an MCP server with cross-tenant
security gates. PINAKES is the recognition that "an LLM wiki" is a *view* over a
memory system, not a separate system. Building it as a MNEMOS feature avoids a
parallel datastore, a second search stack, and a second MCP surface to secure.

---

## Roadmap slices

- **P1 — Reachability (unblocks agents today).** 3.1 namespace param on
  `search_memory` + 3.2 `read_article(slug)`. Smallest change that lets an agent
  query a corpus end-to-end through the MNEMOS MCP. Touches the shared MCP
  bridge → Codex review gate before merge.
- **P2 — Wiki verb set.** 3.3–3.6 (list/search/concept/lineage/answer). The full
  front-end surface, all thin wrappers over existing REST.
- **P3 — Review lifecycle.** 5.1 hand-edit protection (enforce the existing
  content_hash), then 5.2 draft→verified→published.
- **P4 — Concept layer.** 4.6 synthesized concept pages + 4.5 incremental
  compile — the Karpathy core, the largest new build.
- **P5 — External-source trust.** Section 7, gated on P2/P3 landing.

---

## References

- Karpathy, "LLM wiki" — gist 442a6bf555914893e9891c11519de94f.
- synto (kytmanov) and the broader project cluster in that gist — prior art for
  the compile/lifecycle/MCP-verb patterns; PINAKES adopts the *verbs* and
  *lifecycle*, not the parallel stack.
- GRAEAE multi-muse consult 2dadc78 (2026-06-04) — derived-index + outbound-only
  edge model.
- Reference frontend: the `llm-wiki` pipeline (`mnemos_sync` is its bridge into
  this substrate).
