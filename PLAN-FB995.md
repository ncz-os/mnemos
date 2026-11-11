# PLAN FB995 — oracle JSON value decoder (tests only)

Target: `mnemos/persistence/oracle.py::_json_value`.
Deliverable: `tests/test_oracle_json_value_20261107.py` plus this plan.

## What gets pinned

- `None` and `""` yield the default (and a supplied default is returned).
- A mapping and a list are returned by identity; a default object is returned by
  identity.
- JSON object/list text parses, `bytes` are decoded then parsed, a JSON string
  scalar yields a `str`, `5` yields an `int`, `true` yields `True`, and a Python
  `5`/`1.5` round-trips through `str()`.
- Unparsable text yields the default; nested structures are preserved; the decoder
  is deterministic.

## Divergences

- `DIVERGENCE:` invalid UTF-8 `bytes` raise `UnicodeDecodeError` because the decode
  happens outside the `try`, so one input class crashes where every other
  unparsable input degrades to the default.
- `DIVERGENCE:` `False` collapses to the default while `0` returns `0`, because the
  guard compares equality against `(None, "")` instead of testing truthiness.
- `DIVERGENCE:` a JSON `null` decodes to the default, making a stored null
  indistinguishable from a value that was missing or unparsable.

## Files

- `PLAN-FB995.md` (new)
- `tests/test_oracle_json_value_20261107.py` (new)
