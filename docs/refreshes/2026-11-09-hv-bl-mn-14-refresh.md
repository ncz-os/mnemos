# Refresh record — `hv/bl-mn-14-20261107` onto `master` (046fcd1f)

**Date:** 2026-11-09
**Branch:** `hv/bl-mn-14-20261107`
**Target:** `origin/master` @ `046fcd1fc542`
**Merge commit:** `3578dd74` (`d5fe719c` + `046fcd1f`)
**Task:** MN-14 / CF-23 — rate limit trusted the left-most `X-Forwarded-For`

## Why this refresh

The branch was reviewed and approved at `85ba0384` and then refreshed onto
`450dcf52` (`fc162967`). `master` has since advanced to `046fcd1fc542`, so the
branch was once again no longer a fast-forward of it.

## What was refreshed

`origin/master` was merged into the branch (`3578dd74`):

- **Conflicts resolved:** none. The recipe merged cleanly — the branch's
  touched files (`mnemos/core/rate_limit.py`, `mnemos/core/config.py`,
  `tests/test_rate_limit_trusted_proxy_hops.py`, `deploy/pantheon/*`) do not
  overlap master's newer work (CF-22 `/v1/audit/proof` cross-tenant oracle,
  MN-05 CF-09 deletion fence on bulk/import paths, deletion-log
  characterisation).
- **Branch intent preserved:** each of the branch-touched source and test
  paths is byte-identical between the pre-merge branch tip `d5fe719c` and the
  merged head — the trusted-XFF-hop fix and its 12 regression tests survived
  unchanged.
- **Target's newer code present:** `046fcd1fc542` is an ancestor of the merged
  head, and `git diff origin/master HEAD` is exactly the branch's own change
  (7 files: the fix, its tests, the pantheon deploy note, the review verdict
  and this record). No master-side path was reverted.

## Test evidence

Repository suite: `python3 -m pytest tests/ --ignore=tests/test_live_e2e.py`
(the externally-gated `tests/integration_nats` / `tests/integration_patroni`
suites are not part of `make test`).

| Tree | Result |
|---|---|
| branch head `929c7258` (pre-merge) | 70 failed, 6257 passed, 576 skipped, 42 errors |
| merged head `3578dd74` (post-merge) | 70 failed, 6307 passed, 576 skipped, 42 errors |

The failing/erroring node-id sets are **identical** (112 ids on each side,
empty symmetric difference) — failures did not grow versus the target. The 50
extra passes are the target's own new tests brought in by the merge.
`tests/test_rate_limit_trusted_proxy_hops.py` passes 12/12, and
`scripts/check_abc_first_guard.py` (the `lint:abc-first` ratchet) passes.
Remaining failures are pre-existing environment issues on `master`.

## Delivery

The same head branch was pushed to ARGONAS (the canonical private bare repo);
local HEAD, `origin` and `argonas` refs agree.

## Review follow-up (2026-11-09, post-merge re-review)

A re-review of the merge raised one [high] finding against
`mnemos/domain/portability/import_.py`:

> `preflight_import_scope_fence` calls `assert_no_active_deletion_for_scopes`,
> but the module does not import it — `NameError`, fence never executes.

**Not reproducible on any revision in this branch's history, and not a defect
in the merged tree.** The module has imported both names since before this
refresh:

- `mnemos/domain/portability/import_.py:14` — `from mnemos.core.security import is_root`
- `mnemos/domain/portability/import_.py:15` — `from mnemos.domain.deletion_fence import assert_no_active_deletion_for_scopes`

Line 15 is `ca391c9c` (MN-05, the commit that introduced the fence call); line
14 predates it. Identical lines are present in `origin/master` and in the
pre-merge branch tip, so the merge could not have introduced the problem.

Evidence gathered rather than asserted:

- `import mnemos.domain.portability.import_` succeeds and both names resolve
  to the expected callables (`deletion_fence.py:69`, `security.py:42`).
- An AST scan of the module reports **no** unresolved global name.
- `git blame` on the import block shows the binding and its use originate in
  the same commit (`ca391c9c`).
- `tests/test_deletion_fence_bulk_and_import_paths.py` passes 12/12, and those
  tests drive `preflight_import_scope_fence` — a `NameError` there would fail
  them.

No code change was made: adding a second, duplicate import of names that are
already imported would be an edit manufactured to satisfy a report rather than
a fix. The reviewer's "required next step" is already satisfied verbatim by the
existing line 15.

Re-ran the full suite after the merge: 70 failed, 6307 passed, 576 skipped,
42 errors — the same 112 failing/erroring node ids as the pre-refresh
baseline, empty symmetric difference. No regression.
