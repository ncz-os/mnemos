# Evolution of MNEMOS

This document is the design-intent companion to `CHANGELOG.md`. `CHANGELOG.md` lists what shipped in each release; this document captures *why*: the origin observation that set the project's direction, the architectural decisions that survived every refactor, and the ones that would be made differently today.

---

## The origin observation

The catalyzing moment was one question typed into a Claude Code session:

> *"Who is Lonnie and what did he promise me?"*

Lonnie is a friend who had promised to send me some equipment. That promise had been discussed across half a dozen prior sessions with Claude, was sitting in a ChromaDB vector store fully intact, and still took **more than fifteen semantic searches** for Claude to piece back together into a single answer:

- *"friend"* → found fragments.
- *"promised"* → more fragments.
- *"equipment"* → connected the promise.
- *"going to send"* → finally reconstructed the story.

It worked, eventually. It should not have taken fifteen searches for an agent to answer a question about a person whose context I had already discussed with it. The problem was not retrieval quality — the vectors were fine. The problem was that vector search finds similar *text*, and the question was about connected *meaning*: **Lonnie → is friend of → me → promised → equipment**. A flat chunked vector store has no way to traverse a graph like that. The agent can only hope the right fragments happen to be semantically nearby, and pray the re-assembly happens in the model.

The written design review from that moment captured it in one line:

> *"Vector search finds similar TEXT, not connected MEANING. Can't traverse relationships like 'Lonnie → is friend of → Jason → promised → equipment'."*

That one observation is the reason almost everything downstream exists. It is why the knowledge-graph API (`/kg/triples`, `/kg/timeline/{subject}`) stores subject → predicate → object triples with temporal validity windows alongside vector memories. It is why MNEMOS ships per-owner scoping on memories, consultations, state, and entities — because *"who"* and *"what's true of them"* and *"who said what to whom, when"* are not answered by similarity alone. And it is why, when the project eventually reached for a proper name, the Greek goddess of memory ended up feeling right: what was being built was not a database and not a cache; it was something that was supposed to remember *the way humans remember*, which is always relational.

The non-negotiable design commitment MNEMOS has carried through every subsequent version: **memory that is persistent, inspectable, attributable, and operationally reliable — not just convenient in a demo.**

---

## Release history

Release-by-release changes live in [`CHANGELOG.md`](./CHANGELOG.md). This document stays focused on the architectural *reasoning*. Version-level retrospectives are not maintained here.

---

## Architectural decisions that held up

The non-obvious calls that proved out across every refactor:

1. **PostgreSQL over anything else.** Every alternative memory system looked at had eventually regretted its SQLite / ChromaDB / duckdb foundation. This codebase has not.
2. **Treat reasoning as its own named subsystem (GRAEAE)** rather than "MNEMOS plus an LLM call". This is what made the audit chain, the circuit breakers, and the Arena.ai weighting possible. A reasoning-as-a-feature design would have had to bolt all of that onto an existing call site.
3. **Compression gets a receipt.** The per-transformation quality manifest is the single most operator-valued feature for audit-sensitive users. No other memory system treats compression as something that requires documentation.
4. **Greek names are subsystem tags, not theming.** Every Greek name in the source tree maps to a real subsystem. When extractive token filter was rebranded to LETHE the change was load-bearing; when ANAMNESIS was added as Tier 3 it had to be its own file, its own code path, its own failure mode. The naming does work.
5. **Referential integrity at the database layer, not the app layer.** The FK edges and their `ON DELETE` choices are the spine of the data model. Applications pass through; the constraint stays.

---

## Architectural decisions we would revisit

Equally important — the calls that were correct at the time but would be made differently today:

1. **Single-writer assumption.** The app currently requires `workers=1` because the circuit breakers, rate limiters, and semaphores are in-process state. A move to a shared state backend (Redis or the DB itself) would let the service scale horizontally. Today it is a scale ceiling accepted honestly.
2. **OAuth state via Starlette `SessionMiddleware`.** Works, but the signing key regenerates on restart if `MNEMOS_SESSION_SECRET` is unset. A future pass should refuse to start rather than warning loudly.
3. **SQL migrations as sequential files rather than a migration tool.** Acceptable at the beginning; at eleven ordered files it is time to move to Alembic or sqitch. The current ordering is documented in `install.py` and `installer/db.py` — two places that must stay in sync.
4. **FakePool in the test harness.** Substring-match-on-SQL is the wrong abstraction for long-term use — shorter patterns silently match as prefixes of longer ones. The right end state is either a real Postgres container in CI or a SQL-parser-based mock. Today's harness is pragmatic; it is not the right end state.

---

## Architectural decision records

### ADR-01: Response cache stays exact-match, not semantic

- **Context.** The GRAEAE consultation cache is an LRU keyed on `sha256(task_type + normalized_prompt)`, not on embedding similarity.
- **Decision.** Keep the exact-match implementation. Do not ship embedding-similarity lookup in v3.0.0.
- **Rationale.**
  - Embedding lookup cost (~5–20 ms for nomic-embed-text / BGE) against a provider round-trip (~500–2000 ms) is ~1% — cheap, not free.
  - Agent workflows in practice repeat exactly-the-same constructed prompts (system prompt + context block + templated question), so exact-match on the normalized form plausibly catches most realistic hits without added complexity.
- **Upgrade trigger.** If instrumentation shows cache hit rate below ~15% under real load, re-evaluate semantic lookup with a live comparison of latency-saved-per-call.
- **Reversibility.** The cache sits behind `ResponseCache.get` / `.set`. Swapping the key function from `sha256(normalized)` to `embedding_lookup(normalized)` is a localized change, not a schema change. A cheap decision to revisit.

### ADR-02: Distillation worker keeps the supervisor-wrapper pattern

- **Context.** Two files, one feature — `distillation_worker.py` is the worker class; `api/lifecycle.py::_run_distillation_worker` is the supervisor that wraps it with exponential-backoff restart.
- **Decision.** Keep the two-file separation. Worker knows how to do the work. Supervisor knows how to keep the worker alive. Two concerns, two files.
- **Rationale.** Conflating work and supervision inside a single class is the pattern that produces workers that "never die because we can't tell whether they died." Moving supervision out — to something that is literally a `while True` loop with a `try/except Exception` and a backoff counter — is what lets the worker class stay small and testable.

---

## For contributors

- Read [`CHANGELOG.md`](./CHANGELOG.md) for what shipped in each release.
- Read this file for *why* — the architectural reasoning behind the shape you see.
- The biggest "don't touch this without understanding why" areas are:
  - **The audit chain lock** (`api/handlers/consultations.py::_write_audit_entry_on_conn`) — widening or narrowing the lock window changes both correctness and throughput. Don't adjust casually.
  - **The FK edge `ON DELETE` choices** (`db/migrations_*.sql`) — each one is the result of a real design conversation about whether history survives deletion.
  - **The SSRF validator** (`api/handlers/webhooks.py::validate_webhook_url`) — the block list and the async-resolve path have both been tightened in response to real concerns. Loosen carefully.
  - **The Greek names.** They look like whimsy from outside. From inside they are subsystem labels that appear in logs, tables, migrations, and code paths. Renaming one is a distributed refactor.

---

*This is a living document. Release-specific history belongs in `CHANGELOG.md`. Architectural decisions that land or reverse after publication belong here.*
