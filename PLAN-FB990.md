# PLAN FB990 — db2 LISTAGG overflow detection (tests only)

Target: `mnemos/persistence/db2.py::_is_listagg_overflow`.
Deliverable: `tests/test_listagg_overflow_20261107.py` plus this plan.

## What gets pinned

- The documented codes are detected: `SQL0137N`, `SQLSTATE=54006`, `SQLCODE=-137`,
  the legacy `SQL0349N` and `57011`, all case-insensitively.
- A generic exception, an exception with no message, and `None` are *not*
  overflows; the result is always a real `bool`.
- A chained exception whose **cause** carries the code is **not** detected
  (only the top exception's text is read).
- Near-miss substrings (`-1371`, `540060`, `1570112`) are matched, a `bytes`
  payload is matched through its `repr`, an exception whose `__str__` raises
  propagates that error, and the check is pure and deterministic.

## Divergences

- `DIVERGENCE:` only `str(exc)` of the top exception is inspected —
  `__cause__`/`__context__` are never walked, so the brief's "a chained exception
  follows the documented rule" is false and a wrapper that rethrows an overflow
  is misclassified as a generic failure.
- `DIVERGENCE:` matching is raw substring, so `-1371`, `540060` and `1570112`
  all count as overflow; a non-overflow error can be reclassified and send the
  caller down the slow re-read path.
- `DIVERGENCE:` the helper is not total — an exception whose `__str__` raises
  propagates that error out of a function whose only job is classification.

## Files

- `PLAN-FB990.md` (new)
- `tests/test_listagg_overflow_20261107.py` (new)
