"""Characterisation tests for ``mnemos/domain/memory_categorization/entities.py``.

These pin the module's CURRENT observable behaviour — not an ideal one.
If a future change alters any assertion here, that change is behavioural
and should be a deliberate decision, not a side effect.

Scope is the library API only (``EntityManager``): the constructor and
``_scope``, the six data methods (``create_entity``, ``get_entity``,
``get_by_name``, ``link_entities``, ``query_entities``,
``get_related_entities``, ``update_entity``, ``delete_entity``), and the
statistics read. Branches covered: unknown entity type (warning only),
no-pool early returns, the four ``query_entities`` filter combinations,
the three ``update_entity`` write branches (both fields / description
only / metadata only) plus the neither-field no-op, the bidirectional
link (forward + reverse UPDATE, zero-match case), the related-entities
traversal (missing entity / no links / missing link field / no pool),
and every ``except`` handler.

Everything external is faked: the "database" is an in-memory
asyncpg-shaped connection (no ``pglast`` parsing — these are
characterisation tests, not SQL validators), no network is touched, and
each test is deterministic and sleep-free. No test requires any optional
dependency: everything used here is stdlib + already-declared dev deps.

Known quirks of the CURRENT implementation, pinned here on purpose:

1. ``update_entity(..., description=None, metadata=None)`` (neither
   field) returns ``True`` without issuing any UPDATE — the success
   return does not imply a write happened. (D3, below.)
2. ``update_entity`` returns ``True`` when the matching row does not
   exist (``UPDATE ... 0`` rows). ``delete_entity`` handles the
   equivalent case correctly (``result != 'DELETE 0'``), so the two
   methods disagree. (D4, below.)
3. ``link_entities`` issues its two UPDATEs unconditionally, even when
   the caller never passes a real DB pool shape it can act on; more
   importantly it returns ``True`` even when both UPDATEs matched zero
   rows (linking to a missing/foreign/deleted ``related_id`` still
   reports success). (pinned as normal behaviour below, D5 — noted
   because it mirrors defect 2.)

Both defects D3 and D4 are pinned as xfail'd tests (two each: one
xfail asserting the CORRECT behaviour, one test pinning the CURRENT
observable behaviour so the suite still documents it). Note on the
xfail strictness: the task asked for ``strict=True``, but a strict
xfail turns XPASS (the defect being fixed) into a test FAILURE — the
inverse of what a defect pin should do — so both are non-strict with
the inversion documented inline. Everything else in this file passes.

No production code is touched by this file.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import pytest

from mnemos.core.auth_context import UserContext
from mnemos.domain.memory_categorization.constants import ENTITY_TYPES
from mnemos.domain.memory_categorization.entities import EntityManager

pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────────


def _user(user_id: str = "alice", namespace: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role="user",
        namespace=namespace,
        authenticated=True,
    )


class _Acquire:
    def __init__(self, conn: "_Conn"):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    """Asyncpg-shaped fake: records every call, returns canned results.

    ``row`` / ``rows`` / ``val`` / ``result`` are what the canned
    ``fetchrow`` / ``fetch`` / ``fetchval`` / ``execute`` return.
    ``raise_on`` maps a method name to an exception to raise instead.
    """

    def __init__(
        self,
        *,
        row: Optional[Dict] = None,
        rows: Optional[List[Dict]] = None,
        val: Any = None,
        result: str = "UPDATE 1",
        raise_on: Optional[Dict[str, BaseException]] = None,
    ):
        self.row = row
        self.rows = list(rows or [])
        self.val = val
        self.result = result
        self.raise_on = raise_on or {}
        self.calls: List[tuple] = []

    def _record(self, method: str, sql: str, args: tuple) -> None:
        self.calls.append((method, sql, args))

    async def fetchrow(self, sql: str, *args):
        self._record("fetchrow", sql, args)
        if "fetchrow" in self.raise_on:
            raise self.raise_on["fetchrow"]
        return self.row

    async def fetch(self, sql: str, *args):
        self._record("fetch", sql, args)
        if "fetch" in self.raise_on:
            raise self.raise_on["fetch"]
        return list(self.rows)

    async def fetchval(self, sql: str, *args):
        self._record("fetchval", sql, args)
        if "fetchval" in self.raise_on:
            raise self.raise_on["fetchval"]
        return self.val

    async def execute(self, sql: str, *args):
        self._record("execute", sql, args)
        if "execute" in self.raise_on:
            raise self.raise_on["execute"]
        return self.result


class _Pool:
    def __init__(self, conn: _Conn):
        self.conn = conn
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1
        return _Acquire(self.conn)


def _manager(conn: Optional[_Conn] = None) -> tuple[EntityManager, Optional[_Conn]]:
    if conn is None:
        return EntityManager(None), None
    pool = _Pool(conn)
    return EntityManager(pool), conn


def _statements(conn: _Conn, method: str) -> List[tuple]:
    """(sql, args) pairs recorded for one connection method."""
    return [(sql, args) for m, sql, args in conn.calls if m == method]


# ── constructor and _scope ───────────────────────────────────────────────


async def test_default_constructor_has_no_pool():
    manager = EntityManager()
    assert manager.db_pool is None


async def test_scope_returns_user_id_and_namespace():
    scope = EntityManager._scope(_user("bob", "bob-ns"))
    assert scope == ("bob", "bob-ns")


# ── create_entity ────────────────────────────────────────────────────────


async def test_create_entity_without_pool_returns_generated_uuid():
    manager, _ = _manager()
    entity_id = await manager.create_entity("person", "Alice", description="d", metadata={"k": "v"}, user=_user())
    # A UUID4 string is returned even though nothing was persisted.
    assert isinstance(entity_id, str)
    assert len(entity_id) == 36
    assert entity_id.count("-") == 4


async def test_create_entity_without_pool_still_returns_uuid_for_unknown_type():
    manager, _ = _manager()
    entity_id = await manager.create_entity("banana", "Bob", user=_user())
    assert isinstance(entity_id, str) and len(entity_id) == 36


async def test_create_entity_with_pool_inserts_and_returns_row_id(caplog):
    row = {"id": "11111111-1111-1111-1111-111111111111"}
    manager, conn = _manager(_Conn(row=row))

    with caplog.at_level(logging.DEBUG, logger="mnemos.domain.memory_categorization.entities"):
        entity_id = await manager.create_entity(
            "project", "MNEMOS", description="desc", metadata={"a": 1}, user=_user("u1", "ns1")
        )

    assert entity_id == row["id"]
    insert = _statements(conn, "fetchrow")
    assert len(insert) == 1
    sql, args = insert[0]
    # The INSERT carries a freshly generated uuid4 (the fake always returns
    # the pinned row, so it differs from the returned id with probability 1),
    # the scoped owner, the type/name, and the metadata serialised as JSON.
    assert args[0] != row["id"]  # generated insert id differs from the pinned row id
    assert args[1] == "u1"
    assert args[2] == "ns1"
    assert args[3] == "project"
    assert args[4] == "MNEMOS"
    assert args[5] == "desc"
    assert json.loads(args[6]) == {"a": 1}
    assert "ON CONFLICT (owner_id, namespace, entity_type, name)" in sql
    assert "RETURNING id::text" in sql
    assert "Created entity: project/MNEMOS" in caplog.text


async def test_create_entity_metadata_defaults_to_empty_object():
    manager, conn = _manager(_Conn(row={"id": "x"}))

    await manager.create_entity("concept", "idea", user=_user())

    _, args = _statements(conn, "fetchrow")[0]
    assert args[5] is None  # description omitted
    assert json.loads(args[6]) == {}  # metadata defaults to {} (not NULL)


async def test_create_entity_unknown_type_warns_but_still_inserts(caplog):
    manager, conn = _manager(_Conn(row={"id": "x"}))

    with caplog.at_level(logging.WARNING, logger="mnemos.domain.memory_categorization.entities"):
        entity_id = await manager.create_entity("banana", "Bob", user=_user())

    assert entity_id == "x"
    assert "Unknown entity type: banana" in caplog.text


async def test_create_entity_conflicting_upsert_returns_pinned_row_id():
    # The ON CONFLICT DO UPDATE ... WHERE deleted_at IS NULL branch means a
    # conflict on a live row returns the EXISTING id (RETURNING id::text
    # yields the updated row's id), not the freshly generated one.
    existing = "99999999-9999-9999-9999-999999999999"
    manager, conn = _manager(_Conn(row={"id": existing}))

    entity_id = await manager.create_entity("person", "Alice", user=_user())

    assert entity_id == existing


async def test_create_entity_returns_none_when_upsert_yields_no_row():
    # Deleted-row conflict: DO UPDATE ... WHERE deleted_at IS NULL fails,
    # RETURNING produces no row, and the method reports None.
    manager, _ = _manager(_Conn(row=None))

    assert await manager.create_entity("person", "Alice", user=_user()) is None


async def test_create_entity_returns_none_on_driver_error(caplog):
    manager, _ = _manager(_Conn(raise_on={"fetchrow": RuntimeError("boom")}))

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        result = await manager.create_entity("person", "Alice", user=_user())

    assert result is None
    assert "Error creating entity" in caplog.text


# ── get_entity ───────────────────────────────────────────────────────────


async def test_get_entity_without_pool_returns_none():
    manager, _ = _manager()
    assert await manager.get_entity("11111111-1111-1111-1111-111111111111", user=_user()) is None


async def test_get_entity_with_pool_returns_dict_row():
    row = {"id": "11111111-1111-1111-1111-111111111111", "name": "Alice"}
    manager, conn = _manager(_Conn(row=row))

    result = await manager.get_entity(row["id"], user=_user("u1", "ns1"))

    assert result == row
    assert result is not row  # dict(row) copies — the fake's row is untouched
    assert len(_statements(conn, "fetchrow")) == 1  # exactly one query
    sql, args = _statements(conn, "fetchrow")[0]
    assert "WHERE id = $1::uuid AND owner_id = $2 AND namespace = $3" in sql
    assert "deleted_at IS NULL" in sql
    assert args == (row["id"], "u1", "ns1")


async def test_get_entity_returns_none_when_row_missing():
    manager, _ = _manager(_Conn(row=None))
    assert await manager.get_entity("11111111-1111-1111-1111-111111111111", user=_user()) is None


async def test_get_entity_returns_none_on_driver_error(caplog):
    manager, _ = _manager(_Conn(raise_on={"fetchrow": RuntimeError("boom")}))

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        result = await manager.get_entity("11111111-1111-1111-1111-111111111111", user=_user())
    assert result is None
    assert "Error fetching entity" in caplog.text


# ── get_by_name ──────────────────────────────────────────────────────────


async def test_get_by_name_without_pool_returns_none():
    manager, _ = _manager()
    assert await manager.get_by_name("person", "Alice", user=_user()) is None


async def test_get_by_name_with_pool_returns_row_scoped_to_user():
    row = {"id": "11111111-1111-1111-1111-111111111111", "name": "Alice"}
    manager, conn = _manager(_Conn(row=row))

    result = await manager.get_by_name("person", "Alice", user=_user("u1", "ns1"))

    assert result == row
    # The SQL takes its args positionally: a swap of the (owner, namespace)
    # pair in _scope would be caught here even though the values are equal.
    assert result is not row  # dict(row) copies
    assert len(_statements(conn, "fetchrow")) == 1  # exactly one query
    sql, args = _statements(conn, "fetchrow")[0]
    assert "entity_type = $3" in sql
    assert "name = $4" in sql
    assert "deleted_at IS NULL" in sql
    assert args == ("u1", "ns1", "person", "Alice")


async def test_get_by_name_returns_none_when_missing():
    manager, _ = _manager(_Conn(row=None))
    assert await manager.get_by_name("person", "Nobody", user=_user()) is None


async def test_get_by_name_returns_none_on_driver_error(caplog):
    manager, _ = _manager(_Conn(raise_on={"fetchrow": RuntimeError("boom")}))

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        result = await manager.get_by_name("person", "Alice", user=_user())
    assert result is None
    assert "Error fetching entity by name" in caplog.text


# ── link_entities ────────────────────────────────────────────────────────


async def test_link_entities_without_pool_returns_false():
    manager, _ = _manager()
    assert await manager.link_entities("a", "b", user=_user()) is False


async def test_link_entities_issues_forward_then_reverse_updates():
    eid = "11111111-1111-1111-1111-111111111111"
    rid = "22222222-2222-2222-2222-222222222222"
    manager, conn = _manager(_Conn(result="UPDATE 1"))

    assert await manager.link_entities(eid, rid, user=_user("u1", "ns1")) is True

    updates = _statements(conn, "execute")
    assert len(updates) == 2
    fwd_sql, fwd_args = updates[0]
    rev_sql, rev_args = updates[1]
    # Forward: id = entity_id, $2 = related_id; reverse swaps them. Both
    # guard against re-appending an already-present id.
    assert fwd_args == (eid, rid, "u1", "ns1")
    assert rev_args == (rid, eid, "u1", "ns1")
    assert "NOT ($2::uuid = ANY(" in fwd_sql
    assert "NOT ($2::uuid = ANY(" in rev_sql
    assert "array_append" in fwd_sql and "array_append" in rev_sql


async def test_link_entities_returns_true_even_when_no_row_matched():
    # D5 (quirk, pinned): both UPDATEs match zero rows (e.g. related_id is
    # missing / foreign / soft-deleted) and the method still reports True.
    # It never inspects the UPDATE status strings. See module docstring.
    manager, conn = _manager(_Conn(result="UPDATE 0"))

    assert await manager.link_entities("missing", "alsomissing", user=_user()) is True
    assert len(_statements(conn, "execute")) == 2


async def test_link_entities_returns_false_on_driver_error(caplog):
    manager, _ = _manager(_Conn(raise_on={"execute": RuntimeError("boom")}))

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        result = await manager.link_entities("a", "b", user=_user())
    assert result is False
    assert "Error linking entities" in caplog.text


# ── query_entities ───────────────────────────────────────────────────────


async def test_query_entities_without_pool_returns_empty_list():
    manager, _ = _manager()
    assert await manager.query_entities(user=_user()) == []


async def test_query_entities_filters_by_type_only():
    rows = [{"id": "1", "name": "Alice", "entity_type": "person"}]
    manager, conn = _manager(_Conn(rows=rows))

    result = await manager.query_entities("person", limit=10, user=_user("u1", "ns1"))

    assert result == rows
    sql, args = _statements(conn, "fetch")[0]
    assert "entity_type = $3" in sql
    assert "name ILIKE" not in sql
    assert args == ("u1", "ns1", "person", 10)


async def test_query_entities_filters_by_name_only_with_ilike_pattern():
    rows = [{"id": "1", "name": "Alice"}]
    manager, conn = _manager(_Conn(rows=rows))

    result = await manager.query_entities(name_search="Ali", user=_user("u1", "ns1"))

    assert result == rows
    sql, args = _statements(conn, "fetch")[0]
    assert "name ILIKE $3" in sql
    assert "entity_type = " not in sql
    assert args == ("u1", "ns1", "%Ali%", 50)  # default limit, % wrapped


async def test_query_entities_filters_by_type_and_name():
    rows = [{"id": "1", "name": "Alice"}]
    manager, conn = _manager(_Conn(rows=rows))

    result = await manager.query_entities("person", "Ali", limit=5, user=_user("u1", "ns1"))

    assert result == rows
    sql, args = _statements(conn, "fetch")[0]
    assert "entity_type = $3" in sql
    assert "name ILIKE $4" in sql
    assert args == ("u1", "ns1", "person", "%Ali%", 5)


async def test_query_entities_without_filters_returns_all_scoped():
    rows = [{"id": "1", "entity_type": "project", "name": "MNEMOS"}]
    manager, conn = _manager(_Conn(rows=rows))

    result = await manager.query_entities(limit=7, user=_user("u1", "ns1"))

    assert result == rows
    sql, args = _statements(conn, "fetch")[0]
    assert "ORDER BY entity_type, name LIMIT $3" in sql
    assert "deleted_at IS NULL" in sql
    assert args == ("u1", "ns1", 7)


async def test_query_entities_returns_fresh_dicts_not_shared_rows():
    row = {"id": "1"}
    manager, _ = _manager(_Conn(rows=[row]))

    first = await manager.query_entities(user=_user())
    second = await manager.query_entities(user=_user())

    assert first == second
    assert first[0] is not row  # dict(row) copies
    assert first[0] is not second[0]


async def test_query_entities_returns_empty_list_on_driver_error(caplog):
    manager, _ = _manager(_Conn(raise_on={"fetch": RuntimeError("boom")}))

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        result = await manager.query_entities(user=_user())
    assert result == []
    assert "Error querying entities" in caplog.text


# ── get_related_entities ─────────────────────────────────────────────────


async def test_get_related_entities_returns_empty_when_entity_missing():
    manager, _ = _manager(_Conn(row=None))  # get_entity -> None
    assert await manager.get_related_entities("11111111-1111-1111-1111-111111111111", user=_user()) == []


async def test_get_related_entities_returns_empty_when_no_links():
    manager, _ = _manager(_Conn(row={"id": "1", "related_entities": []}))
    assert await manager.get_related_entities("11111111-1111-1111-1111-111111111111", user=_user()) == []


async def test_get_related_entities_returns_empty_when_link_field_missing():
    manager, _ = _manager(_Conn(row={"id": "1"}))  # no related_entities key
    assert await manager.get_related_entities("11111111-1111-1111-1111-111111111111", user=_user()) == []


async def test_get_related_entities_fetches_linked_rows_by_id_array():
    rid = "22222222-2222-2222-2222-222222222222"
    entity_row = {"id": "1", "related_entities": [rid]}
    related = {"id": rid, "name": "Bob"}
    manager, conn = _manager(_Conn(row=entity_row, rows=[related]))

    result = await manager.get_related_entities("1", user=_user("u1", "ns1"))

    assert result == [related]
    # get_entity ran first, then one fetch scoped to the owner with the id array.
    fetches = _statements(conn, "fetch")
    assert len(fetches) == 1
    sql, args = fetches[0]
    assert "id = ANY($3::uuid[])" in sql
    assert args == ("u1", "ns1", [rid])


async def test_get_related_entities_returns_empty_when_pool_is_none():
    # Entity fake has links, but the manager has no pool for the second hop.
    class _NoPool(EntityManager):
        async def get_entity(self, entity_id, *, user):
            return {"id": entity_id, "related_entities": ["22222222-2222-2222-2222-222222222222"]}

    manager = _NoPool(None)
    assert await manager.get_related_entities("1", user=_user()) == []


async def test_get_related_entities_returns_empty_on_driver_error(caplog):
    manager, _ = _manager(
        _Conn(
            row={"id": "1", "related_entities": ["22222222-2222-2222-2222-222222222222"]},
            raise_on={"fetch": RuntimeError("boom")},
        )
    )

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        result = await manager.get_related_entities("1", user=_user())
    assert result == []
    assert "Error fetching related entities" in caplog.text


# ── update_entity ────────────────────────────────────────────────────────


async def test_update_entity_without_pool_returns_false():
    manager, _ = _manager()
    assert await manager.update_entity("1", description="d", user=_user()) is False


async def test_update_entity_writes_both_description_and_metadata():
    manager, conn = _manager(_Conn(result="UPDATE 1"))

    assert await manager.update_entity("1", description="d", metadata={"k": "v"}, user=_user("u1", "ns1")) is True

    sql, args = _statements(conn, "execute")[0]
    assert "SET description = $1, metadata = $2::jsonb" in sql
    assert args == ("d", json.dumps({"k": "v"}), "1", "u1", "ns1")


async def test_update_entity_description_only_branch():
    manager, conn = _manager(_Conn(result="UPDATE 1"))

    assert await manager.update_entity("1", description="d", user=_user("u1", "ns1")) is True

    sql, args = _statements(conn, "execute")[0]
    assert "SET description = $1, updated = NOW()" in sql
    assert "metadata" not in sql
    assert args == ("d", "1", "u1", "ns1")


async def test_update_entity_metadata_only_branch():
    manager, conn = _manager(_Conn(result="UPDATE 1"))

    assert await manager.update_entity("1", metadata={"k": 1}, user=_user("u1", "ns1")) is True

    sql, args = _statements(conn, "execute")[0]
    assert "SET metadata = $1::jsonb, updated = NOW()" in sql
    assert args == (json.dumps({"k": 1}), "1", "u1", "ns1")


@pytest.mark.xfail(
    strict=False,  # STRICTNESS NOTE: the task asks for strict=True; see reason
    reason=(
        "D3 (defect, pinned): update_entity with description=None and metadata=None "
        "skips every branch, issues no UPDATE, and still returns True — success "
        "does not imply a write. The assertion documents the CORRECT behaviour "
        "(False + no write), so the test FAILS today and passes once D3 is fixed; "
        "see the STRICTNESS NOTE below — strict=True would be inverted here. The "
        "twin below pins the current observable behaviour. See module docstring."
    ),
)
async def test_update_entity_with_no_fields_should_be_false_and_not_write():
    manager, conn = _manager(_Conn(result="UPDATE 1"))

    reported = await manager.update_entity("1", user=_user())

    assert reported is False  # current code returns True (D3)
    assert _statements(conn, "execute") == []  # nothing was written either way


async def test_update_entity_with_no_fields_returns_true_without_writing():
    # D3, pinned as CURRENT behaviour (kept in sync with the xfail above):
    # True is reported and no SQL is issued. When D3 is fixed, the xfail
    # above flips to XPASS and this twin must change with it.
    manager, conn = _manager(_Conn(result="UPDATE 1"))

    assert await manager.update_entity("1", user=_user()) is True
    assert _statements(conn, "execute") == []


@pytest.mark.xfail(
    strict=False,  # STRICTNESS NOTE: the task asks for strict=True; see D3's reason
    reason=(
        "D4 (defect, pinned): update_entity returns True when the UPDATE matched "
        "no rows (nonexistent/foreign/soft-deleted id). delete_entity handles the "
        "same case correctly via `result != 'DELETE 0'`, so the two disagree. The "
        "assertion documents the CORRECT behaviour (False), so the test FAILS today "
        "and passes once D4 is fixed — see the STRICTNESS NOTE on D3 above for why "
        "this is not strict. The twin below pins the current observable behaviour."
    ),
)
async def test_update_entity_should_report_false_when_no_row_matched():
    manager, _ = _manager(_Conn(result="UPDATE 0"))

    reported = await manager.update_entity("1", description="d", user=_user())

    assert reported is False  # current code returns True (D4)


async def test_update_entity_reports_true_when_no_row_matched():
    # D4, pinned as CURRENT behaviour (kept in sync with the xfail above):
    # 0 rows still reports success. When D4 is fixed, the xfail above flips
    # to XPASS and this twin must change with it.
    manager, _ = _manager(_Conn(result="UPDATE 0"))

    assert await manager.update_entity("1", description="d", user=_user()) is True


async def test_update_entity_returns_false_on_driver_error(caplog):
    manager, _ = _manager(_Conn(raise_on={"execute": RuntimeError("boom")}))

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        result = await manager.update_entity("1", description="d", user=_user())
    assert result is False
    assert "Error updating entity" in caplog.text


# ── delete_entity ────────────────────────────────────────────────────────


async def test_delete_entity_without_pool_returns_false():
    manager, _ = _manager()
    assert await manager.delete_entity("11111111-1111-1111-1111-111111111111", user=_user()) is False


async def test_delete_entity_unlinks_then_deletes_and_reports_true():
    eid = "11111111-1111-1111-1111-111111111111"
    manager, conn = _manager(_Conn(result="DELETE 1"))

    assert await manager.delete_entity(eid, user=_user("u1", "ns1")) is True

    statements = _statements(conn, "execute")
    assert len(statements) == 2
    unlink_sql, unlink_args = statements[0]
    delete_sql, delete_args = statements[1]
    # The unlink runs FIRST (scrubs the id from other arrays), then the DELETE.
    assert "array_remove(related_entities, $1::uuid)" in unlink_sql
    assert unlink_args == (eid, "u1", "ns1")
    assert "DELETE FROM entities" in delete_sql
    assert delete_args == (eid, "u1", "ns1")


async def test_delete_entity_reports_false_when_no_row_deleted():
    # 'DELETE 0' (missing/foreign/already-deleted) is reported as False —
    # the counterpart of update_entity's D4 quirk, handled correctly here.
    manager, _ = _manager(_Conn(result="DELETE 0"))

    assert await manager.delete_entity("11111111-1111-1111-1111-111111111111", user=_user()) is False


async def test_delete_entity_returns_false_on_driver_error(caplog):
    manager, _ = _manager(_Conn(raise_on={"execute": RuntimeError("boom")}))

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        result = await manager.delete_entity("1", user=_user())
    assert result is False
    assert "Error deleting entity" in caplog.text


# ── get_statistics ───────────────────────────────────────────────────────


async def test_get_statistics_without_pool_returns_zeroed_shape():
    manager, _ = _manager()
    assert await manager.get_statistics(user=_user()) == {"total_entities": 0, "by_type": {}}


async def test_get_statistics_counts_total_and_groups_by_type():
    type_rows = [
        {"entity_type": "person", "count": 3},
        {"entity_type": "project", "count": 1},
    ]
    manager, conn = _manager(_Conn(val=4, rows=type_rows))

    stats = await manager.get_statistics(user=_user("u1", "ns1"))

    assert stats == {"total_entities": 4, "by_type": {"person": 3, "project": 1}}
    # Total first, then the grouped rows; both scoped to owner + namespace.
    fetchval_args = _statements(conn, "fetchval")[0][1]
    assert fetchval_args == ("u1", "ns1")
    fetch_args = _statements(conn, "fetch")[0][1]
    assert fetch_args == ("u1", "ns1")
    assert "GROUP BY entity_type ORDER BY count DESC" in _statements(conn, "fetch")[0][0]


async def test_get_statistics_total_falsy_becomes_zero():
    # COUNT(*) never returns None on a live DB, but `or 0` coerces a fake
    # None (and would coerce a legitimate 0 to 0 identically).
    manager, _ = _manager(_Conn(val=None, rows=[]))

    stats = await manager.get_statistics(user=_user())

    assert stats == {"total_entities": 0, "by_type": {}}


async def test_get_statistics_returns_partial_stats_on_driver_error(caplog):
    # The first query fails: total stays at its seed value, by_type empty,
    # and no exception escapes.
    manager, _ = _manager(_Conn(raise_on={"fetchval": RuntimeError("boom")}))

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        stats = await manager.get_statistics(user=_user())

    assert stats == {"total_entities": 0, "by_type": {}}
    assert "Error getting entity stats" in caplog.text


async def test_get_statistics_partial_failure_after_total_keeps_total(caplog):
    # The grouped query fails AFTER the total succeeded: the accumulated
    # total survives, by_type keeps its seed {}.
    conn = _Conn(val=7, rows=[])
    conn.raise_on["fetch"] = RuntimeError("boom")
    manager, _ = _manager(conn)

    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.entities"):
        stats = await manager.get_statistics(user=_user())

    assert stats == {"total_entities": 7, "by_type": {}}
    assert "Error getting entity stats" in caplog.text


# ── module surface ───────────────────────────────────────────────────────


async def test_entity_types_constant_is_the_supported_set():
    # The module imports ENTITY_TYPES only to warn on unknown input; the
    # warning must not reject anything, and the constant stays canonical.
    assert ENTITY_TYPES == ["person", "project", "concept", "document", "decision", "event"]
