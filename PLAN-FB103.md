# PLAN-FB103 — structured status read-back by job id

## Premise check (read the route code, not memory)

- `GET /v1/memories` (`mnemos/api/routes/memories.py:411`) filters on category,
  subcategory, tags, namespace, archived/superseded only. There is no
  metadata filter on any surface.
- `GET /v1/memories/{id}` needs the id. `POST /v1/memories/search` is FTS or
  vector over the whole body, so `"job_id job-<hex>"` returns rows that only
  *mention* the id (audit-mnemos-agent-status-20261102, finding 5) and has no
  offset.
- The MCP tool `list_memories` (`mnemos/mcp/tools/memory.py:262`) proxies that
  same GET, so it inherits the gap.
- Nothing in `mnemos/**/*.py` mentions `job_id`: the checkpoints are written by
  agents, so the filter has to be generic over metadata keys, not a `job_id`
  special case.

## Design (smallest exact-match filter)

`metadata_eq: Mapping[str, str] | None` — exact match on top-level string
fields of `memories.metadata`, ANDed, values always bound.

- HTTP: `GET /v1/memories?meta=job_id:J-123` (repeatable, `key:value`, split on
  the first colon). Bad key / empty value / >8 keys / value >512 chars → 400.
- MCP: `list_memories(metadata_eq={"job_id": "J-123"})`, forwarded to the REST
  seam as repeated `meta=` params.
- One validator, `normalize_metadata_eq` + `metadata_json_path` in
  `mnemos/persistence/types.py` (leaf module, imported by both the persistence
  layer and `types`-only domain code). SQLite, MySQL and Oracle take a JSON
  path as literal SQL text — a computed path is not accepted or not indexable —
  so the key is validated against `[A-Za-z_][A-Za-z0-9_.-]{0,63}` and
  interpolated as a quoted path segment; the value never is.

Per backend (all six, `mariadb` inherits `mysql`, `db2` inherits `oracle`):

| backend  | predicate |
| -------- | --------- |
| sqlite   | `json_extract(metadata, '$."k"') = ?` |
| postgres | `metadata @> $n::jsonb` (json.dumps payload) |
| mysql    | `JSON_UNQUOTE(JSON_EXTRACT(m.metadata, %s)) = %s` (path bound too) |
| oracle   | `JSON_VALUE(m.metadata, '$."k"') = :meta_0` |
| db2      | inherited from Oracle; `_adapt_oracle_to_db2` rewrites the binds |
| mariadb  | inherited from MySQL |

Indexes (the brief asks for sqlite + postgres):

- `migrations/0070_memory_metadata_eq_index.sql`: `gin (metadata
  jsonb_path_ops)` — the operator class for `@>`, generic in the key.
- `migrations_sqlite/migrations_v7_3_memory_metadata_eq_index_sqlite.sql`:
  `json_extract(metadata, '$."job_id"')`. SQLite expression indexes are
  per-path, so this covers the driver's key; other keys scan. Documented.

## Files

- `mnemos/persistence/types.py` — validator + path builder.
- `mnemos/persistence/base.py` — ABC kwarg + docstring.
- `sqlite.py`, `postgres.py`, `mysql.py`, `oracle.py` — predicate; `db2.py`
  inherits.
- `mnemos/api/routes/memories.py` — `meta` query param, parse, 400 on garbage.
- `mnemos/mcp/tools/memory.py` — tool arg + schema.
- `mnemos/db_migrations/migrations/0070_…sql`,
  `mnemos/db_migrations/migrations_sqlite/migrations_v7_3_…sql`,
  sqlite migration list.
- `tests/test_memory_metadata_eq_filter.py` (new), `tests/_fake_backend.py`
  (record the kwarg).

## Risks

- Oracle/Db2 have no SQL JSON-path use anywhere else in the tree — this is new
  for them, and no server is reachable, so those arms assert statement shape,
  not rows.
- Index-on-a-specific-key on SQLite is the only indexable shape SQLite offers
  for a literal path; the alternative (json_each) is generic but unindexed.
- `metadata` NULL on any backend must simply not match (no error).
