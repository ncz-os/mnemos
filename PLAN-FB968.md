# PLAN FB968 — Oracle content hash (tests only)

Target: `mnemos/persistence/oracle.py::_content_hash`.
Deliverable: `tests/test_oracle_content_hash_20261107.py` plus this plan.

## What gets pinned

- SHA-256 hex of the UTF-8 text: 64 lowercase hex characters, deterministic, one
  character in the input changes it, `None` and `""` share the empty digest.
- Normalisation: `\r\n` and a lone `\r` both become `\n` (CRLF first); no
  stripping of surrounding whitespace; Unicode encoded as UTF-8.
- Coercion via `str()`: `123`, `True`, `0`, `False`, `[]`, `()` and a custom
  `__str__` are hashed as their text form; `bytes` hash as their `repr`.
- Agreement with the sqlite (`_content_hash_for_sqlite`) and mysql
  (`_content_hash`) siblings on strings, `None`, CRLF text and numbers, with the
  sibling import skipped cleanly when its driver is absent.

## Divergences

- `DIVERGENCE:` the sqlite sibling normalises with `str(content or "")`, so falsy
  non-strings (`0`, `False`, `[]`) hash the empty digest there while oracle and
  mysql hash `"0"`, `"False"`, `"[]"` — the same value gets a different content
  hash on different backends.
- `DIVERGENCE:` `bytes` are hashed through `str()`, i.e. their `repr`, so
  `b"abc"` hashes as `"b'abc'"`; CRLF inside bytes is never normalised (it
  arrives escaped, not literal), so bytes and text content do not compare.
- `DIVERGENCE:` an object without `__str__` hashes its default `repr` (module,
  class and address), so two equal-looking rows get different, run-dependent
  hashes rather than a stable digest.

## Files

- `PLAN-FB968.md` (new)
- `tests/test_oracle_content_hash_20261107.py` (new)
