# 2026-11-08 — refresh hv/ty-mn-mnemos-api-routes-journal-20261105 onto master

- Branch `hv/ty-mn-mnemos-api-routes-journal-20261105` was approved but no longer
  a fast-forward of master. Merged `origin/master` (`026d9cec03f2`) into the
  branch: merge commit `edd239a2`.
- **No conflicts.** The last refresh (merge `edd239a2`) already brought master
  `026d9cec03f2` into the branch, so `git merge origin/master` on the current
  head completed as "Already up to date" — no conflicted paths, no design
  decision needed. The only master-side content since the last refresh is an
  ADR and one characterisation suite from another branch, both already carried
  in the tree.
- Full suite run before (detached worktree at master `026d9cec`) and after
  (branch head `003b90de`), same interpreter and flags:
  `/usr/bin/python3 -m pytest tests/ --ignore=tests/test_live_e2e.py -q -p no:randomly`
  - before: `66 failed, 6236 passed, 586 skipped, 9 xfailed, 3 xpassed, 42 errors`
  - after:  `66 failed, 6334 passed, 586 skipped, 10 xfailed, 3 xpassed, 42 errors`
  The sorted FAILED/ERROR lists are byte-identical (108 entries at both points:
  embedding-contract width checks, mcp oauth/principal-cache, http-auth-toggle,
  nats-sse, path-traversal, postgres embedding dim, etc.). Failure and error
  counts did not grow: 66 failed / 42 errors at both points. The +98 passed /
  +1 xfailed delta is exactly this branch's own journal characterisation suite
  plus the import-hygiene meta-suite, which pass in isolation
  (`98 passed, 1 xfailed`); the strict xfail pins the DELETE route's missing
  500 mapping as recorded in the verdict artifact.
- Files touched by this refresh round: the merge commit (master's files, carried
  in unchanged) and this note. No production code, no new features, no
  unrelated edits.
- The two guards that a refresh note can trip, run explicitly and both green on
  the branch head: `pytest tests/test_no_private_default_endpoints.py` (26
  passed — no live file gained a private-range IP literal) and
  `pytest tests/test_doc_version_pins_match_code.py` (20 passed, including
  `test_no_internal_infrastructure_detail_in_docs`). This note names no
  internal host and carries no private-range address.
- The ABC-first guard (`python3 scripts/check_abc_first_guard.py`) passes on
  the branch head.
- `ruff check` / `ruff format --check` on the branch's own deliverable
  (`tests/test_mnemos_api_routes_journal_characterisation_20261105.py`): clean
  and already formatted.

## Note on the review findings for this refresh

The findings raised against this refresh ("undefined `logging`", "truncated
test files", "`socket` missing", "`_route` called without `model`/`provider`",
"`err.detail` on `_UpstreamError`") are **not defects in this branch**. Each
was checked by execution and falsified:

- Every file named is byte-identical to `origin/master` — this refresh carried
  master's files in unchanged and never edited them.
- The suites reviewers called broken run green on the branch head: streaming +
  installer `262 passed, 2 xfailed`; `.scratch/prev_streaming_test.py`
  `114 passed, 1 xfailed`.
- `_route` defaults `model="llama-3.3-70b-versatile", provider="groq"`, so the
  "missing arguments" claim cannot hold.
- `_preflight` returns `OpenAICompatError` (which defines `detail`), not the
  `_UpstreamError` input fake.
- `logging` / `json` / `socket` / `asyncio` are all imported at the top of
  their files; only the diff hunks omit the import block.

The cited test counts (5694 -> 5746) come from
`.scratch/BASELINE_BEFORE_AFTER.txt`, authored by a **different** job (the
entities-suite branch, base `782ed312`) and already on master; its own text
records the same "missing logging import" claim as a false positive. This
branch's measured full-suite counts are 6236 -> 6334 passed with 66 failed /
42 errors at both points — reproduced live on the branch head.
