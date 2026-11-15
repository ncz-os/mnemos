# PLAN FB787 — `metadata_json_path`

## Scope
Tests only. `mnemos/persistence/types.py::metadata_json_path`: the quoted JSON path interpolated into SQL for an exact-match metadata filter.

## Files
- `tests/test_metadata_json_path_20261107.py` (new). No production change.

## Cases
The `$."key"` form for the whole accepted character set; dotted and hyphenated keys as one literal field name; refusal of empty, non-string,
over-long, non-ASCII and trailing-newline keys; and the injection surface — every key that could carry a quote, backslash or path operator is
refused, so no statement text can be smuggled through.

## Misconception
The brief expects special characters to be "escaped per the documented rule". Nothing is escaped: the key charset excludes quotes and
backslashes, so such keys are refused outright and no escaping rule exists. Recorded as `DIVERGENCE:`.

## Risks
Pure function; the assertions pin the exact output form, so a change of quoting or path syntax fails loudly.
