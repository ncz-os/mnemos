# MNEMOS Roadmap

Forward-looking scope for MNEMOS releases beyond the current version.
Release-by-release history lives in [`CHANGELOG.md`](./CHANGELOG.md).

This document is kept intentionally narrow. It lists what the next release is
expected to contain, what has been consciously deferred, and why. It does not
list wishlist items, speculative features, or aspirational claims.

---

## Current status — v6.1.7

The 6.1 line is current. What it delivers:

- **MIF 1.0 as the native portability format.** The CHARON adapter does a
  lossless round trip through MIF bundles (concept files plus a manifest) with
  a JSON-LD projection against the published mif-spec.dev schemas —
  `mnemos export --format mif` and `mnemos import --source mif`.
- **Six persistence backends.** DSN-aware one-step schema standup for SQLite,
  PostgreSQL, Oracle Database 26ai, IBM Db2 12.1, MySQL, and MariaDB, each with
  a native vector path.
- **Split-distribution modular install.** A profile-to-services manifest driving
  `install --profile/--with`, scoped migrations, and GRAEAE, PANTHEON, KNEMON,
  and CHARON as opt-in extras over a small `mnemos-core`.
- **PANTHEON LLM gateway.** An OpenAI-compatible multi-provider mesh with
  adaptive routing, cross-provider fallback, durable cooldown stores, external
  pricing-catalog ingest, per-tenant budget pre-gate, BYOK, and lossless
  token compression.
- **KNEMON cost governance.** Usage ledger, subscription-mode and tier-split
  dashboards, by-plan and forecast routes, model registry with pricing ingest,
  and budget delegation with cross-backend routing audit.
- **Security hardening.** A universal Ed25519/Merkle mutation audit chain with
  federation continuity; prompt-injection defense that treats retrieved
  memories as untrusted data; credential-prose detection with
  redact-at-retrieval and secret-vault search exclusion; per-principal memory
  ACLs with delegated group administration; and out-of-distribution query
  rejection behind a semantic relevance floor.
- **Retrieval and dedup.** `current_only` and `exclude_superseded` retrieval
  across every backend, reversible content-hash dedup, and `embedding_status`
  surfaced on memories.

Also shipped in the 6.x line: the GDPR right-to-be-forgotten lifecycle
(deletion request, soft-delete worker, hard-delete worker); the MORPHEUS
divergent dream-state pipeline through CONSOLIDATE and EXTRACT; the PERSEPHONE
archival subsystem with cold-set rotation and zstd-compressed archives; KRONOS
recall-pattern anomaly detection and forecasting; a NATS substrate for PANTHEON
routing pub/sub; and the `mnemos_hot` Rust accelerator covering cosine and
top-k, batch cosine, embedding parse and L2-normalize, composite search rerank,
deterministic judge scoring, and SHA-256 batch hashing behind
`MNEMOS_HOT_RS_ENABLED=1`.

---

## Planned — v7.0 — Rust persistence layer

**Goal:** move the database persistence layer — today a Python
`PersistenceBackend` contract with per-engine implementations — onto a Rust
core, extending the existing `mnemos_hot` accelerator from compute kernels into
the storage layer itself.

**Rationale:** persistence is the hottest and most correctness-critical seam in
MNEMOS. Every store, search, federation write, and audit-chain mutation flows
through it. A Rust contract gives one memory-safe, strongly-typed backend
interface shared across all six databases; connection-pool and vector-search
performance without Python overhead; and a clean FFI boundary so the Python
service, the Rust CLI clients (`mnemosctl`), and the MCP bridge all speak to a
single storage core.

**Scope — candidate, not yet committed:**

- 📋 Express the persistence contract as a Rust trait with per-engine
  implementations behind it, mirroring the current Python contract.
- 📋 Preserve each engine's DSN-aware one-step schema standup and native-vector
  path.
- 📋 Keep the Ed25519/Merkle mutation audit chain and federation continuity at
  the trait boundary, so every backend inherits them uniformly.
- 📋 Cut over incrementally behind a flag, as `mnemos_hot` did with
  `MNEMOS_HOT_RS_ENABLED`, retaining the Python backend as a fallback until
  parity is proven per engine.

---

## Planned — PINAKES (LLM wiki substrate)

Full design and feature tree in [`docs/PINAKES.md`](./docs/PINAKES.md). PINAKES
turns a Markdown corpus into a self-maintaining, cross-linked wiki with MNEMOS
as both the single backend and the single MCP front end — no separate wiki
server, no second datastore. It treats "an LLM wiki" as a view over a memory
system rather than a parallel system. Corpus content stays in the front end;
only the capability lives here.

Shipped:

- ✅ A derived index over a Markdown corpus: page to namespace-isolated memory,
  slug-keyed upsert gated on `content_hash`, disk-removal reconciliation, and a
  transactional commit order.
- ✅ A cross-link knowledge graph: `[[slug]]` becomes an outbound `links_to`
  triple, with backlinks derived at query time so editing one page never drifts
  a neighbour.

Planned:

- 📋 **P1 — reachability:** a `namespace` parameter on the `search_memory` MCP
  tool plus `read_article(slug)`. This is the smallest change that lets an agent
  query a corpus end to end through the existing MNEMOS MCP.
- 📋 **P2 — wiki verb set:** `list_articles`, `search_articles`, `get_concept`,
  `trace_lineage`, `list_sources`, and `answer_question`, each a thin wrapper
  over `/v1/memories?namespace=` and `/v1/kg/triples`.
- 📋 **P3 — review lifecycle:** enforce hand-edit protection through the
  existing `content_hash`, then `draft → verified → published` with confidence
  scores and rejection feedback.
- 📋 **P4 — concept layer:** synthesized concept pages above source pages, with
  cross-source dedup and merge — one article per concept — plus incremental
  compile. This is the largest new build.
- 📋 **P5 — external-source trust:** prompt-injection hardening, source
  trust-tiering, and a publish gate for externally sourced pages.

---

## Consciously not planned

- **Flat-file storage as the memory of record.** A database is canonical.
  Markdown is an import and export surface, not the store.
- **Promotion gates as the primary memory mechanism.** MORPHEUS is a
  synthesizer, not a triage queue; PERSEPHONE covers archival decisions.

---

*This document reflects committed plans, not speculative features. Items listed
here are intended to land in their scheduled release unless explicitly deferred.
Priorities may shift during a release cycle; this document is updated in the
same commit that shifts them.*
