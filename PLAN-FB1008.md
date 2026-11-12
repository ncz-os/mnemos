# PLAN-FB1008 — mnemos: webhook record mapping (tests only)

Brief: FREEBUFF-LANE-BRIEF-67.md `### FB1008.` (base `master`).
Symbols: `_sqlite_webhook_subscription`, `_sqlite_webhook_delivery`.

## Scope
- New file: `tests/test_webhook_records_20261107.py`. Production untouched.

## Established facts (source read)
- `WebhookSubscriptionRecord` (frozen, slots): id, url, events, description, owner_id,
  namespace, created, revoked, revoked_at.
- `WebhookDeliveryRecord` (frozen, slots): id, subscription_id, event_type, payload,
  payload_hash, attempt_num, status, response_status, response_body, error, scheduled_at,
  delivered_at, created, status_updated_at, superseded, lease_token, lease_expires_at,
  writer_revision.
- `events` goes through `_json_list` then `str()` per element; bad JSON yields `()`.
- `description` is only read when the row is a `dict`; otherwise `None`.
- `id`, `subscription_id`, `lease_token` are `str()`-coerced; `writer_revision` is
  `int(row[...] or 0)`; timestamps go through `_sqlite_webhook_datetime` (UTC).
- Delivery rejects a status outside pending/retrying/succeeded/abandoned with `ValueError`.

## Cases
1. Subscription row → exact record equality (all nine fields).
2. Subscription timestamps are aware UTC; `revoked_at` None stays None.
3. `revoked` int flag becomes a bool.
4. `events` JSON text becomes a tuple of str.
5. Non-list / absent / invalid `events` become the empty tuple.
6. `description` missing from a dict → None (documented default).
7. `description` present on a dict → preserved.
8. `description` on a `sqlite3.Row` → None (the guard only accepts dict).
9. `id` int is coerced to str.
10. Subscription mapper does not mutate the row and adds no keys.
11. Delivery row → exact record equality (all eighteen fields).
12. Delivery optional columns absent/None → None; `superseded` bool.
13. `writer_revision` 0/None → 0; `attempt_num` int; `lease_token` str-coerced.
14. All four documented statuses accepted; anything else raises `ValueError`.
15. Delivery mapper does not mutate the row.
16. Both mappers accept a real `sqlite3.Row`.

## Verification
`pytest tests/test_webhook_records_20261107.py -q` plus `ruff check`.
