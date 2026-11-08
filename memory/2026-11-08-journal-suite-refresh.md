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
- Files touched by this refresh round: the merge commit (master's additive files
  only) and this note. No production code, no new features, no unrelated edits.
- `tests/test_no_private_default_endpoints.py` still passes (26 passed) — this
  note names no internal host or private-range address.
