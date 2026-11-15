# PLAN FB772 — `_federation_allow_private`

## Scope
Tests only. `mnemos/domain/federation.py::_federation_allow_private` (line 97), the lazy read of
`FEDERATION_ALLOW_PRIVATE` behind the peer-URL SSRF guard.

## Files
- `tests/test_federation_private_20261107.py` (new). No production change.

## Cases
Every accepted boolean spelling maps as documented; the switch is read per call (late-bound, not captured at
import); an absent switch takes the field default and the result is a real `bool`. Invalid spellings — including
the empty string, numeric-looking values and whitespace padding — are pinned to whatever actually happens.

## Risks
Reads module-global settings. Every case sets the env var and clears `config._settings`, which the autouse
`isolate_settings_singleton` fixture already clears around each test.

## Landing (2026-11-14)

Landed onto `master` as `hv/land-FB772-20261114`: `origin/master` (`de535000`) is merged in so both intents are
kept — the target's newer code and the branch's suite. Tests only; no production code is changed.

### Verification against the source

Every claim in the suite was re-derived from the source before landing, not taken from the branch's prose:

- `mnemos/domain/federation.py::_federation_allow_private` is exactly the lazy `get_settings().federation.allow_private`
  read (the import is inside the function, so module import never loads config).
- `mnemos/core/config.py::_FederationSettings.allow_private` is `Field(True, validation_alias="FEDERATION_ALLOW_PRIVATE")`
  — the absent-switch default is the permissive trusted-LAN posture (`True`), not a safe one. The unconditional guard
  that IS safe is the metadata-host rejection in `net_validation`, which does not read this switch.
- The accepted spellings map as the suite asserts; `"banana"`, `"2"`, `"1.0"`, `"-1"`, `""` and `" true "` all raise
  `pydantic.ValidationError` out of the call (the value is validated verbatim — no trimming, and an empty string is
  not "unset"); the result is always a real `bool`.

### Mutation check

| mutant | caught |
|---|---|
| capture the value at import instead of reading it per call | yes — 19 of the 29 cases fail |
| flip the field default to `False` | yes — `test_an_absent_switch_takes_the_field_default` fails |
| return `allow_private is True` | no — semantically equivalent for a `bool` field, so not a defect in the suite |

### Review

Two independent non-author reviews, both **APPROVE** with no findings:

- `tydeus-reviewer` (local reviewer endpoint) — APPROVE, 0 findings.
- `Gemma-4-31B-IT` (`tokenlab_gemma`) — APPROVE, 0 findings.

Both were asked about vacuous/tautological assertions, state leakage through the memoised `config._settings` global,
the accuracy of the DIVERGENCE docstrings, over-constraint, and whether a meaningful behaviour (the unconditional
metadata-host refusal) is left unpinned. Neither raised a defect.

### Suite counts

Full suite (`tests/`, `--ignore=tests/test_live_e2e.py`, order pinned with `-p no:randomly`):

| state | passed | failed | errors | skipped |
|---|---|---|---|---|
| `origin/master` (`de535000`) alone | 7912 | 53 | 42 | 576 |
| after the merge (branch + suite) | 7941 | 53 | 42 | 576 |

The failure set is unchanged by the landing: the same 53 failures and 42 errors, all pre-existing on `master` itself
(missing optional dependencies in this environment — not regressions). The +29 passed are exactly the new cases.
`ruff check` and `ruff format --check` are clean on the touched file; the ABC-first ratchet guard is unaffected (no
production file touched).
