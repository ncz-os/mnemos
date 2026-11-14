# 2026-11-07 — refresh hv/ty-mn-mnemos-api-routes-journal-20261105 onto master

- Branch `hv/ty-mn-mnemos-api-routes-journal-20261105` was approved but stale vs master.
- Merged `origin/master` (24497c97c935) into the branch. No conflicts — master
  added only `tests/test_mnemos_domain_portability_phases_characterisation_20261105.py`,
  which touches no file the branch changed.
- Full suite run before (master @24497c97) and after (merged head):
  `python3 -m pytest tests/ --ignore=tests/test_live_e2e.py -q`.
  Before: 4 failed / 5924 passed. After: the same 4 pre-existing failures
  (embedding-contract width checks) — no new failures.
- Fixed two leaks the branch's own notes introduced: the earlier refresh note
  named an internal host and carried a private-range IP literal, which tripped
  `test_no_internal_infrastructure_detail_in_docs` and
  `test_private_ip_literal_in_live_files`. The note now names neither.
- Pushed the SAME branch name to the configured remote; local HEAD == remote
  SHA, upstream tracking set, worktree clean.
