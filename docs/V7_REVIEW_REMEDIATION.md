# v7 adversarial review remediation

Review base: core `1d8ad87^..b53835b`, CHARON `65e458e..1b2e90b`.
The review was performed directly against the canonical GitLab source.

## Findings and fixes

1. **Oracle import cycle.** Importing `oracle_audit` before `oracle` tried to
   resolve the unfinished audit module. Shared driver helpers now live in
   `oracle_helpers`; subprocess regressions check Oracle, audit, and Db2 import
   orders. Journal mixin dispatch and concrete audit ABC implementation are
   checked. The move verifier checks the original four symbols and five helpers.
2. **Audit coverage exclusions hid reachable behavior.** MySQL falls into the
   unsupported audit branch; malformed stored public keys and the supported
   Python 3.13 UUID fallback are also reachable. All audit coverage exclusions
   were removed. Tests cover backend dispatch, both UUID paths, malformed keys,
   and failed timezone conversion. Driver dispatch tests use real constructors
   and an isolated writer spy; they are not live enterprise database tests.
3. **Parity inventory overclaimed evidence.** A facade accessor and a matching
   test filename cannot establish complete implementation or passing tests.
   The generated report now identifies static surfaces and test candidates,
   with explicit limits. Its inheritance traversal uses C3 precedence, resolves
   import aliases and same-file bases, respects shadowing, and detects empty
   and None-returning accessors without treating docstrings as executable code.
   The canonical GitLab pipeline now enforces regeneration, in addition to GitHub.
4. **Move checker gave an incomplete guarantee.** It missed changes to global
   imports/constants, inspected the hardcoded Oracle source for arbitrary moves,
   and used a drifting HEAD~1 default. It now checks direct bindings, the actual
   source path, async functions, class-closure hazards, and a fixed historical
   base. It explicitly disclaims dynamic/transitive semantic equivalence;
   execution tests remain necessary. GitLab now runs the checker.
5. **Streaming cursor rejection occurred after HTTP 200.** CHARON validates
   cursor syntax and tenant scope before constructing either streaming response.
   Per-record client spooling batches commits instead of fsyncing every record.
   The existing shared authorization, verbatim redaction, canonical query,
   keyset ordering, and repeatable-read connection remain intact.
6. **Benchmark evidence and lifecycle gaps.** The harness requires aiosqlite,
   reports peak in-flight inserts and the single-connection model, validates
   persisted row counts, records the actual capped warmup count, measures the
   whole cell, and cancels workers/closes/removes the database on errors.
   The original artifact's insert throughput remains historical evidence;
   its total wall time excluded setup and cleanup. Documentation corrects that.
7. **Composition CI used stale versions.** Satellite pipelines pinned a pre-v7
   core and old sibling revisions; the core CHARON release pin excluded the
   streaming implementation. Follow-up pin commits bind CI and release composition
   to the reviewed sources. No moving branch references are used for dependencies.

8. **Composition-only layer violations.** Core shutdown imported the GRAEAE
   domain directly and could construct an engine just to close it. GRAEAE now
   registers its own optional cleanup when its singleton is created. The MCP
   tool also imported pure helpers from an API route; those six helpers moved
   unchanged into the GRAEAE domain and remain re-exported for compatibility.
   All seven import contracts now pass with every add-on installed; composition
   CI enforces that check so the core-only installation cannot hide this again.

## Verification boundaries

The SQLite harness measures overlapping tasks queued at one backend lock, not
parallel database writers, real embeddings, or end-to-end API throughput.
Phase 2 remains unperformed: 100k/1M memories, concurrency 25/50/100, and packing
capacity require the hardware allocation identified in the roadmap. AST parity
candidates cannot distinguish skipped, mocked, negative, or irrelevant tests.

CHARON's public export route is PostgreSQL-only. Its keyset needs unique IDs and
non-null creation timestamps, not monotonically increasing IDs. The handoff's
claim that no verbatim read exists outside the canonical projection was inaccurate:
the pre-existing scoped-ID backfill remains, followed by serializer redaction.
No new verbatim disclosure was found. Record buffering is bounded by the sub-batch;
page IDs and capped sidecars still consume page-sized memory.

Live verification in this review uses a disposable local PostgreSQL 17 instance;
no production data is modified. Oracle/Db2 changes receive import, AST, and mocked
repository tests, not new live enterprise-backend qualification. Final literal
suite results, commit IDs, and CI status are recorded in the completed handoff.

## Local validation (2026-09-16)

- Core plus pinned add-ons: 3,745 passed, 476 skipped.
- Core only: 3,722 passed, 495 skipped; all seven import contracts also pass in composition.
- Audit gate: 156 passed, 35 skipped; 100% statements and branches without exclusions.
- CHARON with disposable PostgreSQL 17: 320 passed, 4 skipped.
- GRAEAE: 150 passed, 4 skipped; KNEMON with PostgreSQL: 227 passed, 1 skipped;
  PANTHEON: 376 passed.
- Oracle structural move check: nine symbols passed. Migration inventory: 62 with parity.
- Full dependency resolution and installed-package compatibility check passed.
- SQLite Phase 1: six cells completed; peak in-flight inserts equalled requested
  concurrency in each cell. This shared-host run is not a capacity qualification.

These counts separate executed tests from skips; no skipped test is treated as
positive live evidence. Changes are on `fix/v7-review-20260916`; this review does
not merge the branch or deploy a service.
