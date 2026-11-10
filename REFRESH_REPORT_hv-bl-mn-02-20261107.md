# Refresh report: `hv/bl-mn-02-20261107` onto `master` (1dfca0dd / b922fabb)

**Date:** 2026-11-10 (04:35–04:55 ET)
**Branch:** `hv/bl-mn-02-20261107`
**Target master:** `1dfca0dd9c23` (head at job time `b922fabb`, the memory-dedup
refresh merge)
**Pre-refresh branch head:** `b2f31411` ("Merge origin/master (1dfca0dd) into
hv/bl-mn-02-20261107")
**Post-refresh branch head:** `49ed6525` ("Merge remote-tracking branch
'origin/master' into hv/bl-mn-02-20261107")

## Why

`master` advanced past the branch's merge point: the refresh of
`hv/ty-mn-mnemos-domain-memory-dedup-20261105` (`b922fabb`) landed on
`master` with its memory-dedup characterisation suite plus `VERIFY_5dff28820b26.json`.
The approved MN-02 branch was therefore no longer a fast-forward of master.

## Method

- `git fetch origin master`
- `git merge origin/master` into `hv/bl-mn-02-20261107` — **clean, zero
  conflicting hunks** (ort strategy). No design decision was required.
- Suite run on **target** (`origin/master`, `b922fabb`, in a throwaway
  worktree) and on the **refreshed branch head** (`49ed6525`), same sandbox,
  `python3 -m pytest tests/ -q` (Python 3.14.8, pytest 9.2.0, `-p
  no:cacheprovider`).

## Suite comparison

| Tree | Result |
| --- | --- |
| target `b922fabb` (origin/master) | **83 failed, 6635 passed, 586 skipped, 12 xfailed, 3 xpassed, 42 errors** (104s) |
| refreshed branch `49ed6525` | **83 failed, 6646 passed, 586 skipped, 12 xfailed, 3 xpassed, 42 errors** (100s) |

The per-test `FAILED`/`ERROR` sets are **byte-identical** between the two
trees (125 entries compared with `diff`). The +11 passed delta is exactly the
master-brought-in memory-dedup characterisation suite
(`tests/test_mnemos_domain_memory_dedup_characterisation_20261105.py`):
re-run standalone in the target tree it collects 40 items — 38 passed +
2 xfailed — of which 29 already ran in the target's full-suite run and 11
(its `asyncio`-parametrised variants, which fail to collect there without the
asyncio driver) now run green on the refreshed head. **No failure grew
versus the target.** All 125 failing/erroring tests are pre-existing on
master (sqlite-parametrised embedding/audit tests, MCP extras errors,
etc.), identical before and after.

## Branch's intended change (unchanged)

The MN-02 change-set is exactly:

- `mnemos/api/routes/document_import.py` — redact vaulted chunk content in
  the `memory.created` webhook dispatch (MN-02)
- `mnemos/domain/document_repo.py` — stored-chunk metadata contract
- `tests/test_document_import_webhook_redaction.py`
- `tests/persistence/test_document_repo_imported_chunk_metadata.py`

No unrelated files were touched by this refresh. The merge diff
(`git diff b2f31411 49ed6525`) is exactly the two master-side additions:
`VERIFY_5dff28820b26.json` and
`tests/test_mnemos_domain_memory_dedup_characterisation_20261105.py` —
zero changes under `mnemos/` and no modifications to the branch's own
MN-02 change-set.

## Delivery

- Branch `hv/bl-mn-02-20261107` pushed to ARGONAS (`origin`), upstream
  tracking intact, remote SHA == local HEAD.
