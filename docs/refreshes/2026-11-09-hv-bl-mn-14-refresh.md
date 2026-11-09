# Refresh record — `hv/bl-mn-14-20261107` onto `master` (450dcf52)

**Date:** 2026-11-09
**Branch:** `hv/bl-mn-14-20261107`
**Target:** `origin/master` @ `450dcf520777`
**Merge commit:** `fc162967` (`d5fe719c` + `450dcf52`)
**Task:** MN-14 / CF-23 — rate limit trusted the left-most `X-Forwarded-For`

## What was refreshed

The branch was reviewed and approved at `85ba0384`, but `master` had since
advanced to `450dcf520777`, so the branch was no longer a fast-forward of it.
`origin/master` was merged into the branch (`fc162967`):

- **Conflicts resolved:** none. The recipe merged cleanly — the branch's
  touched files (`mnemos/core/rate_limit.py`, `mnemos/core/config.py`,
  `tests/test_rate_limit_trusted_proxy_hops.py`, `deploy/pantheon/*`) do not
  overlap master's newer work (document-import webhook redaction, Oracle
  reserved-word bind names, ADR-D-MN5, version/docs pins).
- **Branch intent preserved:** each of the five branch-touched paths is
  byte-identical between the pre-merge branch tip `d5fe719c` and the merged
  head — the trusted-XFF-hop fix and its 12 regression tests survived
  unchanged.
- **Target's newer code present:** `450dcf520777` is an ancestor of the merged
  head, and every file the merge brought in is a target-side path.

## Test evidence (repository suite, `tests/`, excluding the externally-gated
`tests/integration_nats` and `tests/integration_patroni`)

| Tree | Result |
|---|---|
| target `450dcf52` (pre-merge) | 69 failed, 6245 passed, 563 skipped, 42 errors |
| branch head (post-merge) | 69 failed, 6257 passed, 563 skipped, 42 errors |

The failure/error sets are identical (111 F/E node ids on each side, empty
symmetric difference) — **failures did not grow** versus the target. The 12
extra passes are this branch's own `tests/test_rate_limit_trusted_proxy_hops.py`
regression tests, which all pass. Remaining failures are pre-existing
environment issues on `master`.

## Delivery

The same head branch was pushed to ARGONAS (the canonical private bare repo);
local HEAD, `origin` and `argonas` refs agree.
