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

## Round 2 — review-defect fixes (2026-11-10)

The first delivery was reviewed; the characterisation test file that master
brought into this branch carried test-logic defects, plus two genuine defects
the tests had pinned with `xfail(strict=True)`. All addressed:

**Code (`mnemos/domain/memory_dedup.py`):**

1. **`delivery_ids` correlation fix** (was the pinned `xfail` "stringifies
   WebhookDeliveryIntent via repr"): `_dispatch_delete_side_effects` now
   extracts `intent.delivery_id` from the intents `dispatch_event` returns —
   the same extraction `mnemos/api/routes/memories.py` performs — and the
   apply loop no longer double-wraps with `str(item)`. Callers can now
   correlate outbox rows / post-commit NATS nudges on the delivery id.
2. **Best-effort audit contract documented** on
   `_maybe_write_delete_audit`: `write_configured_audit_entry` swallows
   (and `logger.exception`s) audit write failures in the default
   `MNEMOS_AUDIT_CHAIN=on` mode by design — a failed audit write never rolls
   back the soft delete; only `MNEMOS_AUDIT_CHAIN=required` propagates
   `AuditChainContinuityError`.
3. **`_memory_ids_from_row` doc note**: a raw string row never reaches the
   split branch (`_row_get`'s str fallback drops it), and the reachable str
   branch's `if part` filter also drops falsy-but-present ids.

**Tests (`tests/test_mnemos_domain_memory_dedup_characterisation_20261105.py`):**

- `test_memory_ids_from_unit_separator_string_is_always_empty` →
  `test_memory_ids_from_raw_unit_separator_string_returns_empty`: renamed and
  re-documented to pin the actual mechanism (IndexError/AttributeError
  fallback → default `[]`), and the adjacent xfail's reason/docstring now
  state that a raw-string row is dropped entirely rather than "filters falsy
  parts".
- `test_row_get_maps_falsy_values_to_the_default` →
  `test_row_get_maps_none_to_the_default_but_passes_other_falsy_values_through`:
  explicit assertions for the `None` → default mapping (the contract it
  actually carries) alongside the falsy pass-throughs.
- New `test_group_from_row_maps_none_fields_to_empty_strings`: `None`
  owner/namespace/hash fields become `""` and still form a group.
- New `test_backfill_repository_error_propagates_and_rolls_back` (plus a
  default-kwarg pinning test): a repository failure propagates out of
  `transactional()` with a rollback.
- `test_dedup_dry_run_with_no_groups` strengthened (empty-groups payload
  fields + clean commit) and new `test_dedup_apply_with_no_groups_is_a_noop_commit`.
- New `test_dedup_apply_best_effort_audit_write_failure_does_not_rollback`:
  audit insert raising in `on` mode does not roll back the dedup commit.
- The delivery_ids `xfail` removed; replaced by positive pins
  (`test_dispatch_delete_side_effects_returns_delivery_ids_and_nats_intents`
  asserts `delivery_ids == ["d1", "d2"]`, and the new end-to-end
  `test_dedup_apply_delivery_ids_correlate_on_delivery_id`).

**Suite after the round-2 fixes** (same sandbox, `pytest tests/ -q`):
target `b922fabb` = 83 failed / 6635 passed / 42 errors vs fixed head =
83 failed / 6652 passed / 42 errors. The 125-entry `FAILED`/`ERROR` sets
are **byte-identical** to the target (diff clean); passed +17 vs target,
xfailed -1 (the fixed defect no longer xfails). **No failure grew.**

## Round 3 — the pinned memory_ids defects fixed, not characterised (2026-11-10)

The review of round 2 asked for the `_memory_ids_from_row` defects to be
**fixed** rather than pinned as xfail. Done.

**Code (`mnemos/domain/memory_dedup.py`):**

1. **Raw-string rows are now processed** (was the "silently drops raw string
   memory_ids" critical defect): `_memory_ids_from_row` handles a raw string
   row directly — `_row_get` cannot look a key up inside a str (IndexError
   → AttributeError → default `[]`), so the string is split itself. A
   raw-string group row (GROUP_CONCAT/LISTAGG shape) now forms a group with
   the first id as keeper and the rest as duplicates, instead of being
   silently discarded.
2. **Falsy-present ids are preserved on the str branch** (was the high
   defect): memory ids are opaque strings, so the string `"0"` is a valid
   id. The split keeps **all** non-empty parts — only genuinely empty
   segments (double separators, leading/trailing `\x1f`) are dropped.
3. **`_row_get` None-vs-falsy behaviour reviewed and documented**: the
   asymmetry (only `None` maps to the default; `0`/`""`/`[]`/`False` pass
   through) is deliberate and safe — every `_row_get` caller either coerces
   through `str(...)` with a default or treats the value as an opaque
   field, so a falsy-but-present value is never silently replaced. It is
   now stated in the helper's docstring and pinned by
   `test_row_get_maps_none_to_the_default_but_passes_other_falsy_values_through`.

**Tests:**

- The two `xfail(strict=True)` pins are REMOVED and converted to positive
  tests: `test_memory_ids_from_raw_unit_separator_string_splits_the_ids`
  (raw string → split ids) and `test_memory_ids_preserves_falsy_present_id_on_split`
  (`"a\x1f0\x1fb"` → `["a", "0", "b"]` on both the raw-string and
  mapping-with-string paths; a lone `"0"` survives).
- New `test_group_from_raw_string_row_forms_group_with_all_ids`: an
  end-to-end `_group_from_row("keep\x1fdup1\x1fdup2")` forms a group
  (previously it returned `None` — no group at all).
- All six backends' `find_duplicate_content_groups` already normalise
  `memory_ids` to a list, so this hardens the domain layer against
  raw-column shapes (and matches the independent
  `mnemos/domain/artemis_dedup.py` implementation, which already handled
  raw strings).

**Suite after the round-3 fixes** (same sandbox, `pytest tests/ -q`):
target `b922fabb` = 83 failed / 6635 passed / 42 errors vs fixed head =
83 failed / 6654 passed / 42 errors. The 125-entry `FAILED`/`ERROR` sets
are **byte-identical** to the target (diff clean); passed +19 vs target,
xfailed -2 (both fixed defects now pass). `lint:abc-first` guard passes.
**No failure grew.**

## Merge conflict-resolution strategy (task requirement)

Both merges (`49ed6525` onto `b922fabb`, and the round-2/3 fix commits on
top) were **clean — zero conflicting hunks** (ort strategy): master's
additions (the memory-dedup characterisation suite + `VERIFY_5dff28820b26.json`)
and the branch's MN-02 change-set touch disjoint files, so the branch's
intended change and the target's newer code are both kept in full, with no
manual resolution and no design decision required. The round-2/3 commits
modify only files already owned by the branch's intended change-set's
review surface (`mnemos/domain/memory_dedup.py`, its characterisation test,
and this report) — no unrelated files.
