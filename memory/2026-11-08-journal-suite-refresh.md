# 2026-11-08 — refresh hv/ty-mn-mnemos-api-routes-journal-20261105 onto master

- Branch `hv/ty-mn-mnemos-api-routes-journal-20261105` was approved but no longer
  a fast-forward of master. Merged `origin/master` (`026d9cec03f2`) into the
  branch: merge commit `edd239a2`.
- **No conflicts.** Master's 22 commits since the last refresh (`24497c97`, which
  was already an ancestor of the branch) only ADDED files — four other
  characterisation suites, two review-verdict artifacts, one ADR, and scratch
  notes — plus a 4-line/3-line tweak to
  `tests/test_federation_peer_interval_validation_20261105.py`, a file this
  branch never touched. `git merge` completed with no conflicted paths, so no
  design decision was needed.
- Full suite run before (detached worktree at master `026d9cec`) and after
  (merged head), same interpreter and flags:
  `/usr/bin/python3 -m pytest tests/ --ignore=tests/test_live_e2e.py -q -p no:randomly`
  - before: `57 failed, 6248 passed, 589 skipped, 9 xfailed, 3 xpassed, 42 errors`
  - after:  `57 failed, 6306 passed, 589 skipped, 10 xfailed, 3 xpassed, 42 errors`
  The sorted FAILED/ERROR lists are byte-identical (99 entries at both points:
  embedding-contract width checks, mcp oauth/principal-cache, http-auth-toggle,
  connector smoke, etc.). Failure count did not grow: 57/42 -> 57/42. The
  +58 passed / +1 xfailed delta is exactly this branch's own journal
  characterisation suite, which passes in isolation
  (`58 passed, 1 xfailed`, the strict xfail pinning the DELETE route's missing
  500 mapping as recorded in the verdict artifact).
- Files touched by this refresh round: the merge commit (master's files, carried
  in unchanged — nine added, one modified by master itself) and this note. No
  production code, no new features, no unrelated edits.
- The two guards that a refresh note can trip, run explicitly and both green on
  the merged head: `pytest tests/test_no_private_default_endpoints.py` (26
  passed — no live file gained a private-range IP literal) and
  `pytest tests/test_doc_version_pins_match_code.py` (20 passed, including
  `test_no_internal_infrastructure_detail_in_docs`). This note names no internal
  host and carries no private-range address.
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
- The suites reviewers called broken run green on the merged head: streaming +
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
branch's measured full-suite counts are 6248 -> 6306 passed with 57 failed /
42 errors at both points — reproduced live on the merged head.
