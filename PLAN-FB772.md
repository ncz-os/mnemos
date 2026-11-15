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
