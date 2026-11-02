# PLAN-FB113 — injection quarantine must not rewrite JSON keys (audit M1)
NOTE: plan recorded after implementation (integrated incorrectly when the
task began); the red/green discipline is documented in the test module
docstring instead: 4 failing tests before the fix, 10 passing after.

## Premise (re-verified at master dd74b5f2 + FB112 branch head)
`quarantine_injections` on the audit's JSON status body wraps a detector span
that reaches ACROSS the `", "` boundary into the KEY `"rules"` (the override
pattern `ignore ... rules` has a 40-char free window), producing `"rules]"`
and an unparsable body for a non-root doctor. Reproduced red: 4 failing tests
(`tests/test_json_quarantine_values_20261102.py`), including the audit's
exact shape and a `defend` round-trip.

## Fix (minimal, all inside `mnemos/core/injection_defense.py`)
- `_json_string_value_spans(content)`: spans of JSON string VALUES (inner
  text, quotes excluded) — only when the whole content parses as one JSON
  object/array; a string followed by `:` is a KEY and is never reported.
  Non-JSON content returns [] (byte-identical old behavior).
- `_clip_spans_to_values(...)`: spans are clipped to the value regions, so a
  crossing span is still defanged inside the value while keys/structure are
  never rewritten.
- `quarantine_injections` uses the clip only when values exist. Sentinel
  defanging, framing and every non-JSON path unchanged.

## Tests (`tests/test_json_quarantine_values_20261102.py`, 10)
- Red at base: audit body unparsable/keys rewritten (4).
- Both directions: injection prose inside a value still wrapped; simple
  value injection stays parsable; non-JSON prose byte-identical wrapped/
  unwrapped; JSON without strings untouched; nondetector bodies byte-identical.

## Risks / verified
- Existing suites green: test_prompt_injection + accept-negotiation +
  mcp format + narrate (137), broad `-k "injection or quarantin or defense"`
  (205 passed, 14 skipped). ruff clean on both changed files.
- No schema/API change; read paths all route through the same core function.
