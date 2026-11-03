# PLAN-FB136 — mnemos: SQLite passes the raw user query to FTS5 MATCH (audit M7)

Base: `origin/master` @ 750d7332, branch `freebuff/FB136-20261103`, clone `~/Projects/freebuff-fb136-fts5/`.
Audit source: `~/Projects/refactor-research-20261031/audit-mnemos-agent-status-20261102.md` finding 7 (M7): "SQLite passes the raw query to FTS5 MATCH (live operators and syntax errors). PG uses plainto_tsquery. 'job_id job-<hex>' returns 0 rows on SQLite and the hit on PG."

## Reproduced facts (checked on this head, 2026-11-03)
- `SqliteBackend.memories.fts_search` (mnemos/persistence/sqlite.py:1692) binds the raw `query`
  as the FTS5 MATCH parameter (the MATCH fetch is at :1778-1786). FTS5 operators in user
  input are live (`a OR b` acts as OR, `x -y` acts as AND NOT, `"unbalanced` raises), and
  malformed input lands in the `except sqlite3.Error` → LIKE fallback by luck.
- `postgres.py:fts_search` (:1808-1812) uses `plainto_tsquery('english', $1)`: plain text,
  implicit AND, no operators, no prefix. Both backends have an empty-result → LIKE/ILIKE
  rescue, so an empty query ends at the LIKE path on both.
- FTS5 table: `memories_fts` (migrations_sqlite/migrations.sql:72-77), columns
  id UNINDEXED / content / category, `tokenize = 'porter'` (unicode61 token shapes +
  porter stemming). No stopword handling — differs from PG's english parser (see limits).

## Change (production, one commit)
- Add module-level `_fts5_plain_query(query: str) -> str | None` next to the search helpers
  in sqlite.py: whitespace-split tokens; the audit's `job-<hex>` case must survive, so a
  hyphen-containing token stays one literal phrase; a trailing `*` on a token is kept as the
  FTS5 prefix operator; everything else that could be an operator or a syntax error is
  neutralised by phrase-quoting (interior `"` doubled). Control chars are stripped (raw NUL
  would otherwise truncate the string in sqlite's C layer). Tokens reduce away → emit None.
  No read-then-write or other behavioural surface beyond the one expression.
- In `fts_search`, bind `_fts5_plain_query(query)` instead of the raw query; None → skip the
  FTS attempt and take the existing `_like_search()` rescue directly (that is exactly the
  state an empty/malformed raw query reached today, minus the pointless FTS roundtrip).
  Every other parameter binding, condition order, bm25 ordering, and both rescues unchanged.

## Property preserved
Plaintext parity target (per the brief): for plain terms the sanitised MATCH returns the
same rows PG's plainto path would for a AND-of-terms corpus. Not provable without live PG —
tested as "terms AND together, operators are literal text" on the sqlite side, with the
residual semantic gaps listed in the task report.

## Tests (new file `tests/test_fts5_query_sanitiser_20261103.py`)
- Acceptance, against a real `SqliteBackend` (fresh file DB, no embeddings needed):
  document corpus with known contents; table-driven cases — plain terms, quotes, hyphens,
  colons, parentheses, `AND`/`OR`/`NEAR`, unbalanced quotes, unicode, empty string,
  whitespace-only, the audit's literal `job_id job-<hex>` probe; every input: no FTS5
  syntax error escapes, and asserted hit sets (AND semantics, operators literal, prefix
  `*` still finds `universe` from `univ*`). Malformed-only inputs (e.g. `"`, `*`,
  whitespace) must produce the LIKE-path hits (the empty-result rescue on both backends
  yields the containing-rows set).
- Property test (hypothesis; `pytest.importorskip` so suite stays green without it):
  for arbitrary text (parked charclasses: ascii printable + surrogate-free unicode,
  exercising quotes/hyphens/colons/parens/operators heavily), `_fts5_plain_query` output
  bound to a real FTS5 MATCH never raises sqlite3.OperationalError, on the migration-built
  table, for both empty and populated corpora.
- Unit pins on the sanitiser itself: token doubling of embedded quotes; single trailing
  `*` kept, interior/lone `*` neutralised; control-char stripping; N-token AND join.

## Risks / limits
- PG english parser drops stopwords ("and" ⇒ absent from the tsquery); FTS5 porter has no
  stopwords, so a query "foo AND bar" makes `and` a required term (matches PG only when the
  doc contains the literal word). Stated as a documented sqlite↔postgres difference, not fixed.
- PG `plainto_tsquery` does stemming config language-side; FTS5 porter differs in stem
  cases. Only same-corpus AND semantics are promised.
- No hidden requirements: no other backend touched; no API/route changes; migration none.

## Verification
- One batched command per loop: `python -m pytest tests/test_fts5_query_sanitiser_20261103.py
  tests/test_sqlite_search_regressions.py -q` in a project venv, plus `ruff check` on the
  two changed files, plus the full pre-existing search/backed-battery files listed in the
  final report (counts pasted).
- Structured non-author review `zoder --json review --scope branch --base origin/master
  -m nvidia/nemotron-3-ultra-550b-a55b`; verify every finding against source; then push and
  capture `git ls-remote origin freebuff/FB136-20261103`.
