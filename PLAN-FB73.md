# PLAN-FB73 — mnemos: characterisation tests for the least-tested large modules

Task: Find the three largest mnemos modules with the fewest test references, add
characterisation tests only (new files, no production changes) pinning today's
behaviour including error paths; mark suspected defects `xfail(strict=True)`
with a comment and list them.

## Finding the targets (measured 2026-10-31 at master b38624f)

Package line counts vs tests/ import-reference counts:

| module | lines | test files referencing |
|---|---|---|
| mnemos/core/resilience.py | 2203 | 2 |
| mnemos/persistence/{oracle,sqlite,mysql,postgres,db2}.py | 5.9k–9k | 30–67 (heavily tested, and the contract suites already exist) |
| mnemos/installer/__main__.py | 2537 | 8 (though 166 ref lines) |
| mnemos/tools/memory_import.py | 1449 | 3 |
| mnemos/portability/charon.py | 1335 | 3 |

Chosen: **core.resilience** (2203), **tools.memory_import** (1449),
**portability.charon** (1335) — the three largest modules whose test coverage
is limited to a couple of files. persistence backends and installer are larger
but already carry dedicated suites, so "least-tested largest" excludes them.

## Files

- `tests/test_characterisation_fb73_20261031.py` — ONE new test file, ~35 tests,
  sections per module. No other files touched. Production code untouched.

## What each section pins (all verified by probes on 2026-10-31)

### InProcessCircuitBreaker (mnemos/core/resilience.py)

- new breaker: closed, allowed, status `{"state": "closed", "failures": 0}`
- failures accrue; at threshold state flips OPEN and check_open mirrors is_allowed
- OPEN before cooldown: is_allowed False, state stays OPEN, record_success is a
  no-op (failures unchanged, still OPEN)
- cooldown elapsed (0s cooldown): is_allowed True transitions OPEN→HALF_OPEN
- HALF_OPEN record_success: needs success_threshold successes to close; failures
  reset to 0 on close
- HALF_OPEN record_failure: re-OPENs when failures >= threshold again
- HALF_OPEN is_allowed unconditionally True
- CLOSED record_success DECREMENTS failures (2 fails + 1 success = 1 left)

### InProcessCircuitBreakerPool

- lazily creates one breaker per provider; untouched provider stays out of status()
- an open provider blocks only itself; a second provider still allowed
- per-provider status shape

### NATS helper functions (no server needed)

- `_nats_missing_key` / `_nats_wrong_revision` match class-name or message text,
  false otherwise
- `_nats_entry_value` coerces bytes/bytearray/str; `_nats_entry_revision`
  prefers `.revision`, falls back to `.rev`, None otherwise
- `maybe_await` / `call_maybe_async` with sync values and coroutines

### mnemos/tools/memory_import (JsonImporter, dry_run so no server)

- `_stable_id_from_mem`: exact digest for a fixed memory dict; deterministic
- `_apply_scope_overrides`: owner and/or namespace override, identity when none
- wire shapes 1–4 through run(): plain array; `{"memories": [...]}`; MPF
  envelope one-shot; `.jsonl`/jsonl=True per-line (MPF-record lines unwrapped)
- counting: empty-content rows skipped; malformed top-level JSON → failed=1;
  jsonl bad line → failed += 1 while good lines still import
- jsonl + sidecar trailer + preserve_metadata → `source_envelope` built with the
  sidecars verbatim plus mpf and lifted-flat records
- jsonl + sidecar trailer WITHOUT preserve_metadata: warning printed, sidecars
  dropped, records still import with failed unchanged

### mnemos/portability/charon (bundle export/import)

- manifest shape (mif_version/schema/context/generator/count/concepts),
  `<conceptType>/<uuid>.md` layout, manifest entries point at real files
- round-trip: export → import recovers the memory rows
- error path: empty content is non-conformant → ValueError naming the memory id
  and the schema error; validate=False skips the check and exports anyway
- stream_manifest=True: return value keeps the fixed keys (no `concepts`), the
  on-disk manifest still parses and carries the streamed concepts list
- import of an empty directory → []
- `_row_to_memory_dict`: metadata/merge_parents JSON strings coerced, embedding
  coerced to list-of-floats, other keys untouched
- `_sidecar_id_for_insert`: preserve keeps the original id unless uuid is
  required and the id is not a UUID; non-preserve and non-UUID-original get the
  generator; `_new_memory_id` shape `mem_<us>_<6 hex>`

## Suspected defects → xfail(strict=True)

1. `memory_import`: a JSONL export with a sidecar trailer
   (`{"mpf_sidecars": true, ...}`) imported WITHOUT `--preserve-metadata`
   prints a warning but still reports success (`failed=0`) while silently
   dropping the sidecars — a round-trip loses data while exit counts look
   clean. The xfail-only test asserts the corrected behaviour (the run must
   not report a fully successful import when the sidecars were dropped).
   Comment cites the code's own warning text.

Nothing else today felt like a defect; two shapes (pool-sized breakers never
getting a custom success_threshold, stream-manifest's split return shape) are
explicitly documented in the code as deliberate.

## Risks / limits

- NatsCircuitBreaker / RedisCircuitBreaker(RateLimit)Pool need live NATS/redis
  servers — deliberately NOT exercised; only the pure helper functions are
  pinned. Noted in the file docstring.
- ChatGPTImporter/ObsidianImporter/CsvImporter/TextImporter/MifImporter/StatsCommand
  are out of scope for this pass (JsonImporter is the load-bearing surface);
  listed as follow-up, not a defect.
- Time-dependent breaker branches pinned via cooldown_seconds=0 (elapsed) and
  3600 (not elapsed) instead of patching the clock, so no clock coupling.

## Verification loop

- `.venv-fb73/bin/pytest tests/test_characterisation_fb73_20261031.py -q`
- `ruff check` on the new file only.
- Then structured non-author review, push, ls-remote.
