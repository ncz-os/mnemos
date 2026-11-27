"""Characterisation tests for ``mnemos/db/deletion_log.py``.

These pin the module's CURRENT observable behaviour — not an ideal one.
If a future change alters any assertion here, that change is behavioural
and should be a deliberate decision, not a side effect.

Scope is the module only (161 lines, zero test references before this
file): the two async audit writers (``log_target_memory_deletions``,
``log_morpheus_run_memory_deletions``), the read helper
(``fetch_deletion_log``), and the two private helpers they use
(``_validate_request_kind``, ``_source_array``) plus the
``VALID_REQUEST_KINDS`` constant they validate against.

Everything external is faked: no network is touched, no live database is
contacted, and no clock is read — the writers only *build* SQL and hand it
to a recording connection, and ``fetch_deletion_log`` only *reads* counts
and rows back out of one. Each test is deterministic.

Three suspected defects are pinned as ``xfail(strict=True)`` — see the
"XFAIL" section at the bottom:

1. a bare ``str`` passed as ``source`` is silently split into characters
   instead of being rejected;
2. ``fetch_deletion_log`` builds a negative ``OFFSET`` for ``page < 1``
   instead of rejecting the page;
3. ``fetch_deletion_log`` treats ``owner_id=""`` as "no owner filter",
   silently widening the audit query to every owner.
"""

from __future__ import annotations

import pytest

from mnemos.db.deletion_log import (
    VALID_REQUEST_KINDS,
    fetch_deletion_log,
    log_morpheus_run_memory_deletions,
    log_target_memory_deletions,
    _source_array,
    _validate_request_kind,
)

# ── fakes ───────────────────────────────────────────────────────────────


class _RecordingConn:
    """Records every ``execute``/``fetchval``/``fetch`` call it receives.

    ``fetch`` returns a generator so the ``list(rows)`` materialisation
    inside ``fetch_deletion_log`` is pinned too. Errors, when configured,
    are raised from the matching method.
    """

    def __init__(
        self,
        *,
        execute_result: object = "OK",
        fetchval_result: object = 0,
        fetch_rows: object = (),
        execute_error: BaseException | None = None,
        fetchval_error: BaseException | None = None,
        fetch_error: BaseException | None = None,
    ) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self._execute_result = execute_result
        self._fetchval_result = fetchval_result
        self._fetch_rows = list(fetch_rows)
        self._execute_error = execute_error
        self._fetchval_error = fetchval_error
        self._fetch_error = fetch_error

    async def execute(self, sql: str, *args: object) -> object:
        self.execute_calls.append((sql, args))
        if self._execute_error is not None:
            raise self._execute_error
        return self._execute_result

    async def fetchval(self, sql: str, *args: object) -> object:
        self.fetchval_calls.append((sql, args))
        if self._fetchval_error is not None:
            raise self._fetchval_error
        return self._fetchval_result

    async def fetch(self, sql: str, *args: object) -> object:
        self.fetch_calls.append((sql, args))
        if self._fetch_error is not None:
            raise self._fetch_error
        return iter(self._fetch_rows)


# ── VALID_REQUEST_KINDS + _validate_request_kind ────────────────────────


def test_valid_request_kinds_set_is_pinned():
    """The accepted set is exactly the three audit kinds — nothing more."""
    assert VALID_REQUEST_KINDS == {"gdpr_wipe", "admin_purge", "tombstone_collected"}


@pytest.mark.parametrize("kind", sorted(VALID_REQUEST_KINDS))
def test_validate_request_kind_returns_registered_kind_unchanged(kind):
    """A registered kind passes through untouched (identity, not a copy)."""
    result = _validate_request_kind(kind)
    assert result == kind


@pytest.mark.parametrize("kind", ["", "gdpr", "GDPR_WIPE", "Tombstone_Collected", "admin-purge"])
def test_validate_request_kind_rejects_unknown_kinds(kind):
    """Anything outside the set raises, with the value repr'd in the message."""
    with pytest.raises(ValueError) as excinfo:
        _validate_request_kind(kind)
    assert str(excinfo.value) == f"invalid deletion_log request_kind: {kind!r}"


@pytest.mark.parametrize("value", [None, 1, object()])
def test_validate_request_kind_rejects_non_string_values(value):
    """Non-strings are rejected too — membership in the set is the only test."""
    with pytest.raises(ValueError) as excinfo:
        _validate_request_kind(value)
    assert str(excinfo.value) == f"invalid deletion_log request_kind: {value!r}"


# ── _source_array ───────────────────────────────────────────────────────


def test_source_array_none_stays_none():
    """``source=None`` stays ``None`` (the SQL ``$n::text[]`` gets a NULL)."""
    assert _source_array(None) is None


def test_source_array_list_items_are_stringified():
    """Non-string items are coerced with ``str()`` rather than rejected."""
    assert _source_array([1, "a", None]) == ["1", "a", "None"]


def test_source_array_tuple_becomes_a_list():
    """A tuple source is accepted and materialised as a list."""
    assert _source_array(("a", "b")) == ["a", "b"]
    assert isinstance(_source_array(("a",)), list)


def test_source_array_returns_a_fresh_list_not_the_input():
    """The result never aliases the caller's list (safe against later mutation)."""
    original = ["a"]
    result = _source_array(original)
    result.append("b")
    assert original == ["a"]


# ── log_target_memory_deletions ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_target_passes_documented_arguments_in_order():
    """The writer hands the SQL and seven positional args to ``conn.execute``."""
    conn = _RecordingConn(execute_result="INSERT 0 2")
    result = await log_target_memory_deletions(
        conn,
        "alice",
        "tenant-a",
        requested_by="root-admin",
        requested_at="2026-05-01T11:30:00+00:00",
        request_kind="gdpr_wipe",
        reason="GDPR ticket 42",
        source=["tool-x"],
    )
    assert result is None, "the writer returns nothing, not the driver's status string"
    assert len(conn.execute_calls) == 1
    sql, args = conn.execute_calls[0]
    assert args == (
        "alice",
        "tenant-a",
        "root-admin",
        "2026-05-01T11:30:00+00:00",
        "gdpr_wipe",
        "GDPR ticket 42",
        ["tool-x"],
    )
    # The SQL is the live Postgres-only INSERT ... SELECT the audit row from.
    assert "INSERT INTO deletion_log" in sql
    assert "encode(digest(COALESCE(content, ''), 'sha256'), 'hex')" in sql
    assert "WHERE owner_id = $1" in sql
    assert "AND ($2::text IS NULL OR namespace = $2::text)" in sql
    assert "AND deleted_at IS NOT NULL" in sql
    assert "$7::text[]" in sql


@pytest.mark.asyncio
async def test_log_target_defaults_match_the_signature():
    """Only the three required positional args are needed: everything else defaults."""
    conn = _RecordingConn()
    await log_target_memory_deletions(conn, "alice", None, requested_by="worker")
    (sql, args), = conn.execute_calls
    assert args == ("alice", None, "worker", None, "tombstone_collected", None, None)
    # A NULL namespace matches every namespace: the SQL carries the guard.
    assert "($2::text IS NULL OR namespace = $2::text)" in sql


@pytest.mark.asyncio
async def test_log_target_invalid_kind_raises_before_any_execute():
    """Validation runs while the arguments are evaluated: no partial write."""
    conn = _RecordingConn()
    with pytest.raises(ValueError) as excinfo:
        await log_target_memory_deletions(
            conn, "alice", None, requested_by="worker", request_kind="nope"
        )
    assert str(excinfo.value) == "invalid deletion_log request_kind: 'nope'"
    assert conn.execute_calls == [], "the failing call must not reach the connection"


@pytest.mark.asyncio
async def test_log_target_coerces_tuple_source_to_list_of_str():
    """A tuple source reaches the driver as a list of strings."""
    conn = _RecordingConn()
    await log_target_memory_deletions(
        conn, "alice", None, requested_by="worker", source=(1, "x")
    )
    (_, args), = conn.execute_calls
    assert args[6] == ["1", "x"]


@pytest.mark.asyncio
async def test_log_target_returns_none_regardless_of_execute_result():
    """Whatever ``execute`` answers with, the writer's return value is None."""
    for status in ("INSERT 0 0", "INSERT 0 5", None, object()):
        conn = _RecordingConn(execute_result=status)
        assert await log_target_memory_deletions(conn, "alice", None, requested_by="w") is None


# ── log_morpheus_run_memory_deletions ───────────────────────────────────


@pytest.mark.asyncio
async def test_log_morpheus_passes_documented_arguments_in_order():
    """The run-scoped writer takes the run id first, then the audit kwargs."""
    conn = _RecordingConn()
    await log_morpheus_run_memory_deletions(
        conn,
        "11111111-2222-3333-4444-555555555555",
        requested_by="morpheus",
        requested_at="2026-05-02T10:00:00+00:00",
        request_kind="admin_purge",
        reason="rollback",
        source=["runner"],
    )
    (sql, args), = conn.execute_calls
    assert args == (
        "11111111-2222-3333-4444-555555555555",
        "morpheus",
        "2026-05-02T10:00:00+00:00",
        "admin_purge",
        "rollback",
        ["runner"],
    )
    assert "INSERT INTO deletion_log" in sql
    assert "WHERE morpheus_run_id = $1::uuid" in sql
    assert "AND provenance = 'morpheus_local'" in sql
    assert "AND deleted_at IS NULL" in sql
    assert "$6::text[]" in sql


@pytest.mark.asyncio
async def test_log_morpheus_defaults_match_the_signature():
    """The run id and ``requested_by`` are required; the rest defaults."""
    conn = _RecordingConn()
    await log_morpheus_run_memory_deletions(
        conn, "11111111-2222-3333-4444-555555555555", requested_by="morpheus"
    )
    (_, args), = conn.execute_calls
    assert args == (
        "11111111-2222-3333-4444-555555555555",
        "morpheus",
        None,
        "admin_purge",
        None,
        None,
    )


@pytest.mark.asyncio
async def test_log_morpheus_invalid_kind_raises_before_any_execute():
    """Same fail-closed validation as the scope writer: no SQL is issued."""
    conn = _RecordingConn()
    with pytest.raises(ValueError):
        await log_morpheus_run_memory_deletions(
            conn, "11111111-2222-3333-4444-555555555555",
            requested_by="morpheus", request_kind="bogus",
        )
    assert conn.execute_calls == []


@pytest.mark.asyncio
async def test_log_morpheus_returns_none():
    """The run writer returns nothing either."""
    conn = _RecordingConn(execute_result="INSERT 0 1")
    assert (
        await log_morpheus_run_memory_deletions(
            conn, "11111111-2222-3333-4444-555555555555", requested_by="morpheus"
        )
        is None
    )


# ── fetch_deletion_log ──────────────────────────────────────────────────


_ROWS = [
    {"id": "row-1", "memory_id": "mem-1", "requested_at": "2026-05-02T00:00:00+00:00"},
    {"id": "row-2", "memory_id": "mem-2", "requested_at": "2026-05-01T00:00:00+00:00"},
]


@pytest.mark.asyncio
async def test_fetch_with_owner_filters_and_paginates():
    """owner_id is appended as $3; LIMIT/OFFSET shift to $4/$5."""
    conn = _RecordingConn(fetchval_result=7, fetch_rows=_ROWS)
    rows, total = await fetch_deletion_log(
        conn,
        from_ts="2026-05-01T00:00:00+00:00",
        to_ts="2026-05-02T00:00:00+00:00",
        owner_id="alice",
        page=2,
        page_size=10,
    )
    assert total == 7
    assert rows == _ROWS
    assert len(conn.fetchval_calls) == 1
    assert len(conn.fetch_calls) == 1

    count_sql, count_args = conn.fetchval_calls[0]
    assert count_sql == (
        "SELECT COUNT(*) FROM deletion_log WHERE "
        "requested_at >= $1 AND requested_at <= $2 AND owner_id = $3"
    )
    assert count_args == ("2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00", "alice")

    fetch_sql, fetch_args = conn.fetch_calls[0]
    assert "WHERE requested_at >= $1 AND requested_at <= $2 AND owner_id = $3" in fetch_sql
    assert "ORDER BY requested_at DESC, executed_at DESC, id DESC" in fetch_sql
    assert "LIMIT $4 OFFSET $5" in fetch_sql
    assert fetch_args == (
        "2026-05-01T00:00:00+00:00",
        "2026-05-02T00:00:00+00:00",
        "alice",
        10,
        10,  # page 2 of size 10 -> offset 10
    )


@pytest.mark.asyncio
async def test_fetch_without_owner_drops_the_owner_clause():
    """A None owner means "every owner": no clause, only two bound args."""
    conn = _RecordingConn(fetchval_result=3, fetch_rows=[])
    rows, total = await fetch_deletion_log(
        conn,
        from_ts="t1",
        to_ts="t2",
        owner_id=None,
        page=1,
        page_size=50,
    )
    assert (rows, total) == ([], 3)
    count_sql, count_args = conn.fetchval_calls[0]
    assert count_sql == "SELECT COUNT(*) FROM deletion_log WHERE requested_at >= $1 AND requested_at <= $2"
    assert count_args == ("t1", "t2")
    assert "owner_id" not in count_sql
    fetch_sql, fetch_args = conn.fetch_calls[0]
    assert "LIMIT $3 OFFSET $4" in fetch_sql
    assert fetch_args == ("t1", "t2", 50, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fetchval_result", "expected_total"),
    [(0, 0), (None, 0), ("0", 0), ("12", 12), (7, 7)],
)
async def test_fetch_total_is_the_int_of_the_count_query(fetchval_result, expected_total):
    """``or 0`` absorbs falsy counts (0/None) before ``int()`` runs."""
    conn = _RecordingConn(fetchval_result=fetchval_result, fetch_rows=[])
    _, total = await fetch_deletion_log(
        conn, from_ts="t1", to_ts="t2", owner_id=None, page=1, page_size=10
    )
    assert total == expected_total


@pytest.mark.asyncio
@pytest.mark.parametrize(("page", "page_size", "expected_offset"), [(1, 10, 0), (3, 10, 20), (2, 1, 1)])
async def test_fetch_offset_is_page_minus_one_times_page_size(page, page_size, expected_offset):
    """OFFSET is ``(page - 1) * page_size``; LIMIT is ``page_size``."""
    conn = _RecordingConn(fetch_rows=[])
    await fetch_deletion_log(
        conn, from_ts="t1", to_ts="t2", owner_id=None, page=page, page_size=page_size
    )
    fetch_sql, fetch_args = conn.fetch_calls[0]
    # limit_param = len(args)+1, offset_param = len(args)+2 where args excludes
    # the trailing page_size/offset values — i.e. fetch_args minus those two.
    assert f"LIMIT ${len(fetch_args) - 1} OFFSET ${len(fetch_args)}" in fetch_sql
    assert fetch_args[-2:] == (page_size, expected_offset)


@pytest.mark.asyncio
async def test_fetch_materialises_generator_rows_into_a_list():
    """``list(rows)`` is a real list even though the driver hands back an iterator."""
    conn = _RecordingConn(fetchval_result=0, fetch_rows=_ROWS)
    rows, _ = await fetch_deletion_log(
        conn, from_ts="t1", to_ts="t2", owner_id=None, page=1, page_size=10
    )
    assert isinstance(rows, list)
    assert rows == _ROWS


@pytest.mark.asyncio
async def test_fetch_propagates_fetchval_failure():
    """A failing COUNT query surfaces as-is; the row query is never attempted."""
    conn = _RecordingConn(fetchval_error=RuntimeError("count exploded"))
    with pytest.raises(RuntimeError, match="count exploded"):
        await fetch_deletion_log(
            conn, from_ts="t1", to_ts="t2", owner_id=None, page=1, page_size=10
        )
    assert conn.fetch_calls == []


@pytest.mark.asyncio
async def test_fetch_propagates_fetch_failure():
    """A failing row query surfaces as-is."""
    conn = _RecordingConn(fetch_error=RuntimeError("rows exploded"))
    with pytest.raises(RuntimeError, match="rows exploded"):
        await fetch_deletion_log(
            conn, from_ts="t1", to_ts="t2", owner_id=None, page=1, page_size=10
        )


# ── XFAIL: suspected defects, pinned so a fix pops the marker ───────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defect: _source_array accepts a bare str and silently splits it into "
        "characters ('gdpr' -> ['g','d','p','r']) instead of rejecting a value "
        "outside its declared list|tuple|None type. xfail(strict=True): the test "
        "passes only when a str source is rejected."
    ),
)
def test_source_array_rejects_a_bare_string_source():
    """A bare str source must be rejected, not char-split."""
    with pytest.raises(TypeError):
        _source_array("gdpr_wipe")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defect: fetch_deletion_log does no input validation, so page=0 (or a "
        "negative page) builds OFFSET (page-1)*page_size < 0, which Postgres "
        "rejects ('OFFSET must not be negative'). The /admin/deletion-log route "
        "hides this behind Query(ge=1), but the helper itself is unguarded. "
        "xfail(strict=True): the test passes only when page < 1 is rejected."
    ),
)
async def test_fetch_rejects_page_below_one():
    """page < 1 must raise ValueError rather than emit a negative OFFSET."""
    for bad_page in (0, -1):
        conn = _RecordingConn(fetch_rows=[])
        with pytest.raises(ValueError):
            await fetch_deletion_log(
                conn, from_ts="t1", to_ts="t2", owner_id=None, page=bad_page, page_size=10
            )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defect: `if owner_id:` treats owner_id='' (FastAPI gives '' for "
        "?owner_id=) the same as None, silently dropping the owner clause and "
        "widening the root audit query to EVERY owner. An empty string must "
        "either filter owner_id='' or be rejected — never mean 'all owners'. "
        "xfail(strict=True): the test passes only when the clause is kept."
    ),
)
async def test_fetch_empty_owner_id_must_not_widen_the_query():
    """owner_id='' must not be silently treated as 'no owner filter'."""
    conn = _RecordingConn(fetchval_result=1, fetch_rows=_ROWS)
    await fetch_deletion_log(
        conn, from_ts="t1", to_ts="t2", owner_id="", page=1, page_size=10
    )
    count_sql, count_args = conn.fetchval_calls[0]
    assert "owner_id = $3" in count_sql, "an empty owner_id must still constrain the query"
    assert count_args[2] == ""
