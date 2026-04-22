# Evolution of MNEMOS

The version numbers on releases are tidy. The actual path was not. This
document is the honest version — decisions, refactors, mistakes, and the
reasoning that got us from the original prototype to v3.0.0-beta. If you
are considering MNEMOS for your own stack, you should know where it came
from; if you are contributing, you should know which doors have been closed
and why.

Written in April 2026 for the first public release. Maintained going
forward as architecture changes land.

---

## Pre-history — the story that started it all

> **TO FILL IN (from the author).** The original catalyzing moment —
> the conversation with Claude (or the specific failure mode in a
> Claude-assisted agent workflow) that made it obvious this had to be
> a separate system rather than a convenience feature. Keep it concrete:
> what did the agent forget, what broke because of it, what made
> "just use a context file" look inadequate. This paragraph is more
> important to future contributors than any of the architecture notes
> below; it's the *why*.
>
> Placeholder to replace with the real story: a long-running Claude
> session, an accumulating context file, an observable failure in
> retrieval quality after some threshold, and the realization that
> what was needed was not "bigger context" but "memory with operational
> semantics." Whatever the actual details are, put them here.

What followed from that moment was the non-negotiable design commitment
MNEMOS has carried through every subsequent version: **memory that is
persistent, inspectable, attributable, and operationally reliable — not
just convenient in a demo.**

---

## v1.0.0 — December 2025 — "a file that doesn't disappear"

**What shipped:** a single-file FastAPI server with a PostgreSQL + pgvector
backend. GRAEAE ran as a separate service on port 5001; memory and
reasoning were two processes that didn't know much about each other. The
primary goal was the simplest thing that could survive a process restart.

**Key design choices that survived into v3:**

- **PostgreSQL as the backing store, not ChromaDB or SQLite.** Made on the
  first day and never revisited. ACID transactions, real foreign keys,
  real operational tooling. Every "toy" vector store we compared against
  has since been surpassed in capability by the pgvector extension inside
  a real RDBMS.
- **Separate reasoning layer (GRAEAE).** The choice to not bolt consensus
  reasoning onto the memory store, but to run it as its own service with
  its own auth and lifecycle, predates the merger on port 5002 — they
  stayed architecturally separate even after they lived in one process.
- **Memory is a first-class resource with an owner, a category, and a
  provenance**, not a blob with metadata. Every later subsystem —
  versioning, federation, compression, multi-tenancy — was possible
  because this was decided at the beginning.

**What v1.0 couldn't do:**

- No versioning — a memory write was destructive.
- No compression — store text, retrieve text.
- No multi-tenant isolation — every caller shared one bucket.
- No audit trail on reasoning.
- No federation, no webhooks, no OAuth.
- No migrations runner — installs were `psql < schema.sql`.

**Operational shape:** single-user, single-machine, bare-metal. Good
enough to prove the thesis; obviously not good enough to ship.

---

## v2.0.0 — February 2026 — "make it a service, not a script"

**The refactor:** single-file `api_server.py` became the `api/handlers/`
package. `asyncpg` replaced `psycopg2`. Connection pooling arrived.
pgvector-based semantic search moved from "grep in Python" to real vector
search. GRAEAE acquired multi-provider consensus (replacing the single
Mistral-7B fallback) with per-provider quality scoring.

**Why this refactor happened at all:** the single-file server became
unmaintainable around 2,000 lines. Testing was impossible; every bug fix
risked a regression in a module-flavored function three pages away.
`api/handlers/` was the smallest refactor that restored the ability to
reason locally about each domain.

**Architectural decisions committed here:**

- **Handler-per-domain file layout** (memories, consultations, providers,
  sessions, etc.) with a shared `api/lifecycle.py` for DB pool / cache /
  worker registration. Still the shape at v3.
- **`asyncpg` connection pooling** with `min=5, max=20` per worker. These
  defaults have never been revisited and have never needed to be.
- **Per-provider circuit breakers** in GRAEAE — CLOSED / OPEN / HALF_OPEN
  with 5-minute cooldown. Built because one provider's bad afternoon
  was repeatedly killing consultation throughput.

**Mistakes made here:**

- `memories.id` was declared `UUID` at v2. We had to migrate it to `TEXT`
  later when the `mem_xxxxxxxxxxxx` prefix convention showed up in v2.3,
  and the migration caused foreign-key ripples across four dependent
  tables. Lesson: if you're going to prefix your ids with a type tag,
  don't also require them to be UUIDs.
- Backward-compat aliases (`/graeae/*`, `/memories/*`, `/model-registry/*`)
  were *claimed* in the v2→v3 migration story but never actually
  implemented as aliases. The CHANGELOG carried the claim for weeks
  before a release-gate audit caught it and removed it. Lesson: don't
  document promises the code hasn't kept.

---

## v2.3.0 — April 12, 2026 — "memory starts to act like a substrate"

The release where MNEMOS stopped looking like a memory API and started
looking like a substrate. This is also the release where the most
migration pain lived.

**New surface:**

- **Knowledge graph** — temporal triples (subject → predicate → object
  with `valid_from` / `valid_until`), `/kg/triples`, `/kg/timeline/{subject}`.
  First concession that pure embedding search wasn't the whole story.
- **Journal** (`/journal`) — date-partitioned operational log.
- **Key-value state** (`/state/{key}`) — persistent session state store.
- **Entity tracking** (`/entities`) — people, projects, concepts with
  bidirectional links.
- **Model registry** (`/model-registry/`) — the first version of the
  self-maintaining provider catalog. This predates the Arena.ai Elo sync;
  at this point the registry was just a typed catalog of what each
  provider had said was available.
- **LETHE compression (Tier 1, CPU)** — unified the two earlier
  experiments (extractive token filter and SENTENCE) into a single module with two modes:
  `token` (the old extractive token filter) and `sentence` (the old SENTENCE). Both names
  survived as `was extractive token filter` / `was SENTENCE` annotations in source for
  traceability. Eventually the public rename to LETHE was the right call
  but it broke every internal doc and integration that referenced the
  old names — budget a release for that kind of rename.
- **Cryptographic audit chain** — SHA-256-hashed consultation log.
  Earliest version of what's now the tamper-evident hash chain.
- **Multi-user RLS** via PostgreSQL session variables. Put the foundation
  in place for what v3 would extend into full per-owner scoping.
- **Memory versioning with diff and revert** — the `memory_versions` table
  with a trigger that auto-snapshots on every mutation. This was the
  foundation the v3 DAG extended.

**Mistakes made here (this was the painful release):**

- Five `created_at` → `created` column reference bugs across migrations
  and views. A minor column-name inconsistency shipped, got replicated
  across dependent views, and each one was found separately over the
  course of three days. Lesson: pick one column-naming convention at
  migration time and enforce it with a lint rule, not with reviewer
  attention.
- `kg_triples` table was missing from migrations on fresh installs. The
  KG API 500'd on anyone who tried it on a clean DB. Nobody caught it
  because every developer's DB had the table from the dev branch.
  Lesson: "fresh install CI" is not optional for a multi-table system.
- `compression_quality_log.memory_id` was UUID but `memories.id` was
  now TEXT — a type mismatch that was declared via FK and then silently
  tolerated by PostgreSQL until the first real insert. Lesson: if you
  rename a column's type across a version boundary, grep every FK that
  targets it.
- `CREATE EXTENSION vector/pgcrypto` was not sequenced before the tables
  that used those types. Fresh installs that weren't already bootstrapped
  failed. Lesson: extensions belong at the top of the very first
  migration, and migration-runner ordering is load-bearing.
- `distillation_worker.py` crashed on reconnect because `self.db` was
  referenced where `self.db_pool` existed. Simple typo, lived for a
  quarter of the v2 lifetime before surfacing at an inconvenient moment.
- Port 5000 → 5002 move. MNEMOS (5000) and GRAEAE (5001) were split
  across two ports since v1. 2.3 moved MNEMOS to 5002 in preparation
  for the v3 unification. Any operator with an `API_URL` env var set
  to `:5000` silently broke. Lesson: ports are contracts; treat moves
  like API breaking changes.

**Architectural decision committed here, in retrospect too late:** the
decision to keep GRAEAE on its own port, even though both services were
now on the same machine. This would be reversed in v3. The v2.3 split
introduced one real failure mode: the two services could disagree about
whether PostgreSQL was reachable, depending on which pool was healthy.

---

## v2.4.0 — April 19, 2026 — "the unification baseline"

Not a release so much as a consolidation point. v2.4 was the development
baseline from which v3.0 was cut; it's numbered in the CHANGELOG for
traceability but did not ship as a public version.

**What landed in the working tree here:**

- **OpenAI-compatible gateway** (`/v1/chat/completions`, `/v1/models`) with
  automatic provider routing and optional memory injection. The decision
  to expose OpenAI-shape endpoints on top of GRAEAE routing was taken
  here. It turned out to be the single biggest lever for adoption — any
  team already using OpenAI SDKs could switch to MNEMOS by changing
  `base_url`.
- **Stateful session management** (`/sessions/*`) — multi-turn state with
  memory injection at turn boundaries.
- **DAG versioning** — full git-like operations (branch, merge, revert)
  on memory history. Extended the v2.3 `memory_versions` table with
  `commit_hash`, `parent_version_id`, `memory_branches`.
- **THE MOIRAI compression triad** — LETHE / ALETHEIA / ANAMNESIS as the
  formal three-tier compression subsystem with a written quality
  manifest on every transformation. The manifest was the decisive change:
  compression-as-data rather than compression-as-side-effect. Every
  compressed memory now has a receipt explaining what was removed, what
  was preserved, and which downstream uses remain safe.
- **Distillation worker lifecycle integration** — the background worker
  became a real lifespan-tracked task rather than a separately-managed
  process.

v2.4 is where the surface of v3 took its final shape. Everything from
v2.4 ships in v3 without modification; the difference is unification and
security hardening on top.

---

## v3.0.0-beta — April 22, 2026 — "one service, no aliases, production-grade"

The release. Everything between v2.4 and v3.0 is one of: unification,
security hardening, multi-tenant scoping, or honesty (removing claims
the code didn't back).

**Unification.** MNEMOS and GRAEAE became one FastAPI service on port
5002. Nine routers included in a single app. All primary routes
namespaced under `/v1/*`. The earlier backward-compat alias claim was
dropped from both docs and code — v3.0 is the first public release, so
there is no pre-v3 surface to be compatible with.

**Webhooks.** Durable outbound event delivery. HMAC-SHA256 signed. Retry
log replayed on restart via a recovery worker. Soft-delete of
subscriptions retains the delivery log for audit. The SSRF defense on
webhook URLs was the largest single security addition between v2.4 and
v3.0 — it was not in v2.4.

**OAuth / OIDC.** Browser login via Google / GitHub / Azure AD / generic
OIDC. DB-backed sessions with hourly GC. Coexists with Bearer API keys.
The `email_verified` guard for cross-provider account linking was added
specifically because a first draft of the OAuth path would have let a
permissive identity provider claim existing accounts by email match.

**Cross-instance federation.** Pull-based sync. Per-memory opt-in via
`permission_mode` (others-read bit). Loop-prevention via
`federation_source`. The `permission_mode` gate was added late — the
first version of the feed endpoint served every non-federation-sourced
memory, which in retrospect was one configuration error away from a
full tenant leak.

**Self-maintaining model registry.** `provider_sync` against each
provider's `/v1/models` daily. `refresh_elo_weights.py` against the
Arena.ai leaderboard quarterly via systemd timer. `graeae_weight`
(0.50–1.00) feeds the consensus scorer. This replaced the previous
hand-maintained provider list, which had gone stale twice within a
single release cycle.

**Per-owner multi-tenant scoping** on memories, consultations, state,
journal, entities. A `migrations_v3_ownership.sql` migration backfills
`owner_id` columns with the string `'default'` for existing rows and
re-keys the affected uniqueness constraints. Pre-v3 these tables were
globally shared across users — fine for single-user installs, silently
unsafe for multi-user ones.

**Atomic consultation persistence.** Consultation row, audit entry, and
memory references commit in a single transaction held under
`pg_advisory_xact_lock`. Pre-v3, these were three separate transactions;
a crash between them could leave the consultation visible without an
audit entry. Not good for a tamper-evident chain.

**Referential integrity pass.** Every cross-table reference is now a
real FK with an explicit `ON DELETE` semantic (CASCADE or SET NULL,
chosen per edge based on whether the target owns the source's lifecycle
or whether audit has to survive deletion). Twenty-two FK edges.

**Docs / packaging / install story.**

- Installer CLI (`mnemos-install`) now lands on `PATH` via
  `[project.scripts]`. You can `pip install mnemos-os` and run it.
- All 11 SQL migrations ship as `db/*.sql` package data.
- `MANIFEST.in` added so the sdist carries `config.toml.example`,
  `mnemos.service`, `Dockerfile`, `docker-compose.yml`, integrations
  bundle, tests, and docs.
- CI moved from `pip install -e .[dev]` to `uv venv + uv pip install`
  so the CI path matches the supported user workflow.
- Twenty-plus internal dev artifacts (test plans, release checklists,
  ops runbooks) removed from the tree entirely. They did not belong in
  a public repo. One of them, `EXECUTION_WITH_SYSTEM_LOCATIONS.md`,
  carried plaintext SSH passwords — caught during the release-gate
  scrub, not during the feature work. Lesson: ops runbooks and OSS
  codebases must not share a git repo.

**Mistakes found during the v3 release-gate audits (multiple passes):**

Two independent audit passes — self + Codex re-audits — caught work the
original v3 PRs had claimed but not delivered. The ones worth remembering:

- The `created AS created_at` SQL alias that shipped in consultation
  handlers matched the real database but did not match the test harness's
  substring-based mock matcher. Tests passed, production failed. Fixed
  by moving the column rename to the Python response-builder layer and
  keeping the SQL honest to the column name.
- The FakePool owner-scoped query branch was ordered after the
  non-scoped substring branch. Substring matching on SQL is not
  composable; the shorter pattern silently won. Tests claimed owner
  scoping worked; the test harness wasn't actually exercising it.
- A production bearer token was hardcoded at module scope in the
  Docling export tool. Not in main code paths, but it was checked in
  and would have shipped publicly. Caught during the P0 credentials
  scrub.
- The webhook SSRF validator was initially added at *create* time only.
  A subscription URL set outside the handler (direct DB write during
  migration) would bypass the validator at dispatch time. Fixed by
  calling the validator a second time in the dispatcher, with a
  docstring admission that a DNS-rebinding window still exists and
  requires host-pinning to fully close.
- The audit chain lock was held only around the audit INSERT, not
  around the surrounding consultation + memory-refs writes. A crash
  mid-sequence could produce a consultation row without an audit entry.
  The fix widens the lock and aborts the consultation if the audit
  write fails — a visible capacity cost under load, in exchange for
  correctness that the tamper-evident story actually requires.

**Naming scrubs:**

- extractive token filter → LETHE (token mode) and SENTENCE → LETHE (sentence mode). The
  internal acronyms had never been good public names; the Greek names
  are what the compression tiers will be called going forward.
- Internal-infrastructure hostnames (PYTHIA, CERBERUS, PROTEUS,
  ARGONAS) and IP literals scrubbed from all public docs and code
  paths. See `GPU_PROVIDER_HOST` for the generic replacement.
- The MemPalace comparison section was initially a takedown (it led
  with a benchmark critique). Rewritten before release to the current
  *"MemPalace and MNEMOS: different problems, not competitors"* framing.
  The original was accurate on the narrow technical points but wrong
  in spirit for a first-public-release document. Competitors deserve
  respect; we would want the same.

---

## Architectural decisions that held up

For anyone reading this to understand why MNEMOS is shaped the way it
is, the list of non-obvious calls that proved out:

1. **PostgreSQL over anything else.** Every alternative memory system we
   looked at had eventually regretted its SQLite / ChromaDB / duckdb
   foundation. We have not.
2. **Treat reasoning as its own named subsystem** (GRAEAE) rather than
   "MNEMOS plus an LLM call". This made the audit chain possible, the
   circuit breakers possible, the Arena.ai weighting possible. A
   reasoning-as-a-feature design would have had to bolt all of that
   onto an existing call site.
3. **Compression gets a receipt.** The quality manifest turned out to
   be the single most operator-valued feature for audit-sensitive
   users. No other memory system treats compression as something that
   requires documentation.
4. **Greek names are subsystem tags, not theming.** Every Greek name in
   the source tree maps to a real subsystem. When we rebranded extractive token filter
   → LETHE, the change was load-bearing; when we added ANAMNESIS as
   Tier 3, it had to be its own file, its own code path, its own
   failure mode. The naming does work.
5. **Referential integrity at the database layer, not the app layer.**
   The FK edges and their `ON DELETE` choices are the spine of the
   data model. Applications pass through; the constraint stays.

## Architectural decisions we would revisit

Equally important — the calls that were correct at the time but would
be made differently today:

1. **Single-writer assumption.** The app currently requires `workers=1`
   because the circuit breakers, rate limiters, and semaphores are
   in-process state. A v3.1+ move to a shared state backend (Redis or
   the DB itself) would let us scale horizontally. Today it's a scale
   ceiling we accept.
2. **OAuth state via Starlette `SessionMiddleware`.** Works, but the
   signing key regenerates on restart if `MNEMOS_SESSION_SECRET` is
   unset. We warn about it loudly; we should probably refuse to start
   instead.
3. **SQL migrations as sequential files rather than a migration tool.**
   Worked at v1 when there was one file. At v3 with eleven ordered
   files, we should probably move to Alembic or sqitch. The current
   ordering is documented in `install.py` and `installer/db.py` but
   that documentation is two places that must stay in sync.
4. **FakePool in the test harness.** Substring-match-on-SQL is
   fundamentally the wrong abstraction; it silently matches shorter
   patterns as prefixes of longer ones and we've been bitten by that
   twice. The v3.1+ path is either a real postgres container in CI or
   a proper SQL parser-based mock. Today's harness is pragmatic but
   not sound.

---

## For contributors

If you're here to contribute and want to understand the history:

- Read `CHANGELOG.md` for what shipped.
- Read this file for why, and what almost shipped and didn't.
- The biggest "don't touch this without understanding why" areas are:
  - **The audit chain lock** (`api/handlers/consultations.py::_write_audit_entry_on_conn`) — widening or narrowing the lock window changes both correctness and throughput. Don't adjust casually.
  - **The FK edge ON-DELETE choices** (`db/migrations_*.sql`) — each one is the result of a real design conversation about whether history survives deletion.
  - **The SSRF validator** (`api/handlers/webhooks.py::validate_webhook_url`) — the block list and the async-resolve path have both been tightened in response to real concerns. Loosen carefully.
  - **The Greek names.** They look like whimsy from outside. From inside they are subsystem labels that appear in logs, tables, migrations, and code paths. Renaming one is a distributed refactor.

---

*This is a living document. If a major architectural decision lands or
reverses after publication, it goes here. The point of writing it down
is that nobody should have to re-learn any of this by running into it in
production.*
