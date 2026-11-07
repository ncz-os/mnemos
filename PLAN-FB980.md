# PLAN FB980 — webhook scope validation (tests only)

Target: `mnemos/persistence/sqlite.py::_validate_webhook_scope`.
Deliverable: `tests/test_webhook_scope_20261107.py` plus this plan.

## What gets pinned

- A documented `(owner_id, namespace)` pair passes for all four call-site methods,
  and so does the `(None, None)` root/operator pair; both return `None`.
- A half-set pair is refused in either direction with `ValueError`, not
  `TypeError`, and the message names both `owner_id` and `namespace` plus the
  calling method verbatim (pinned exactly for one case).
- Empty strings count as set (`("", "")` passes, `("", None)` raises) and only
  `None` identity matters (`0` and `False` pass as a scoped owner).
- The validator is synchronous, returns `None` on success, and the MySQL sibling
  raises and returns identically across the four-case matrix.

## Divergences

- `DIVERGENCE:` `method` is never validated against a known set — the brief's
  "an unknown method is refused" is false; any string (`"not_a_real_method"`)
  passes in both the scoped and the root/operator form, so the parameter is only
  ever interpolation.
- `DIVERGENCE:` the `(None, None)` root/operator view is a permissive default
  admitted with no caller-identity check — whatever reaches this function with
  both values `None` reads every owner's subscriptions, so the guard enforces
  *consistency* of the pair, not entitlement to the unscoped view.
- `DIVERGENCE:` empty strings are treated as set values, so `("", "")` passes and
  a blank-but-present owner is indistinguishable from a real one; the check is
  `None`-identity only, so `0` and `False` are also accepted as a scoped owner.

## Files

- `PLAN-FB980.md` (new)
- `tests/test_webhook_scope_20261107.py` (new)
