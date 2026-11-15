# PLAN FB797 — `_conn_from_tx` / `_call`

## Scope
Tests only. `mnemos/persistence/oracle_helpers.py`: the tx-to-connection resolver and the call-and-maybe-await helper used by the Oracle driver
adapter.

## Files
- `tests/test_oracle_helpers_20261107.py` (new). No production change.

## Cases
`_conn_from_tx`: `None`, an object carrying `conn`, an object without it, a `None`-valued `conn`, a raising property, and the fact that nothing is
called. `_call`: plain values by identity, sync and async callables, argument and keyword forwarding, a coroutine passed directly, a callable
returning an awaitable, exceptions from both sides, and the class-is-callable case.

## Misconception
The brief expects a missing `conn` attribute to raise. It does not: `getattr(tx, "conn", tx)` falls back to the tx itself, so a handle without the
attribute is silently treated as its own connection. Recorded as `DIVERGENCE:`. `_call` also instantiates anything callable, so passing a class
constructs it.

## Risks
Fakes only; no Oracle driver is imported and no connection is opened, so `_conn_from_tx` is pinned with plain objects and a counting property.
