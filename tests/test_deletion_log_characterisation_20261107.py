"""Characterisation of ``mnemos/db/deletion_log.py`` (FB1122).

Pins the three GDPR deletion-log helpers as SQL and argument contracts: which
row set each writer touches, how the dynamic pagination placeholders are
numbered, and what happens to an invalid request kind. Behaviour that looks
wrong is pinned and named as a divergence in the summary, never repaired here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mnemos.db import deletion_log

FROM = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
TO = datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc)


class _Conn:
    """Records every statement and its positional arguments."""

    def __init__(self, *, count=None, rows=None) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.count = count
        self.rows = [] if rows is None else rows

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "INSERT 0 1"

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self.count

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows

    def only(self, kind: str) -> tuple[str, tuple]:
        matches = [(sql, args) for called, sql, args in self.calls if called == kind]
        assert len(matches) == 1, f"expected one {kind} call, got {self.calls}"
        return matches[0]

    def names(self) -> list[str]:
        return [called for called, _, _ in self.calls]


def _compact(sql: str) -> str:
    return " ".join(sql.split())


# ── The request-kind vocabulary ──────────────────────────────────────────────


def test_the_valid_request_kinds_are_exactly_three() -> None:
    assert deletion_log.VALID_REQUEST_KINDS == {
        "gdpr_wipe",
        "admin_purge",
        "tombstone_collected",
    }


@pytest.mark.parametrize("kind", ["gdpr_wipe", "admin_purge", "tombstone_collected"])
def test_validate_returns_the_kind_it_was_given(kind: str) -> None:
    assert deletion_log._validate_request_kind(kind) == kind


@pytest.mark.parametrize("kind", ["", "GDPR_WIPE", "wipe", "tombstone", None])
def test_validate_rejects_anything_else_with_the_repr_in_the_message(kind) -> None:
    with pytest.raises(ValueError) as info:
        deletion_log._validate_request_kind(kind)  # type: ignore[arg-type]

    assert "invalid deletion_log request_kind:" in str(info.value)
    assert repr(kind) in str(info.value)


def test_source_array_passes_none_through_as_none() -> None:
    assert deletion_log._source_array(None) is None


def test_source_array_copies_a_list_and_stringifies_each_item() -> None:
    original = ["cli", 5]

    converted = deletion_log._source_array(original)  # type: ignore[list-item]

    assert converted == ["cli", "5"]
    assert converted is not original


def test_source_array_accepts_a_tuple_and_an_empty_list() -> None:
    assert deletion_log._source_array(("a", "b")) == ["a", "b"]
    assert deletion_log._source_array([]) == []


# ── log_target_memory_deletions ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_target_logger_writes_only_soft_deleted_rows_in_the_scope() -> None:
    conn = _Conn()

    await deletion_log.log_target_memory_deletions(conn, "alice", "tenant-a", requested_by="root")

    sql, args = conn.only("execute")
    compact = _compact(sql)
    assert "INSERT INTO deletion_log" in compact
    assert "FROM memories" in compact
    assert "WHERE owner_id = $1" in compact
    assert "($2::text IS NULL OR namespace = $2::text)" in compact
    assert "AND deleted_at IS NOT NULL" in compact
    assert args == ("alice", "tenant-a", "root", None, "tombstone_collected", None, None)


@pytest.mark.asyncio
async def test_target_logger_defaults_kind_to_tombstone_collected_and_no_source() -> None:
    conn = _Conn()

    await deletion_log.log_target_memory_deletions(conn, "alice", None, requested_by="root")

    _, args = conn.only("execute")
    assert args[1] is None
    assert args[4] == "tombstone_collected"
    assert args[6] is None


@pytest.mark.asyncio
async def test_target_logger_hashes_content_in_sql_not_in_python() -> None:
    conn = _Conn()

    await deletion_log.log_target_memory_deletions(conn, "alice", None, requested_by="root")

    sql, _ = conn.only("execute")
    assert "encode(digest(COALESCE(content, ''), 'sha256'), 'hex')" in _compact(sql)


@pytest.mark.asyncio
async def test_target_logger_passes_every_option_through_in_placeholder_order() -> None:
    conn = _Conn()
    requested_at = datetime(2026, 5, 1, 11, 30, tzinfo=timezone.utc)

    await deletion_log.log_target_memory_deletions(
        conn,
        "alice",
        "tenant-a",
        requested_by="root-admin",
        requested_at=requested_at,
        request_kind="gdpr_wipe",
        reason="ticket 42",
        source=("cli", "test"),
    )

    _, args = conn.only("execute")
    assert args == (
        "alice",
        "tenant-a",
        "root-admin",
        requested_at,
        "gdpr_wipe",
        "ticket 42",
        ["cli", "test"],
    )


@pytest.mark.asyncio
async def test_target_logger_falls_back_to_now_in_sql_when_requested_at_is_null() -> None:
    conn = _Conn()

    await deletion_log.log_target_memory_deletions(conn, "alice", None, requested_by="root")

    sql, _ = conn.only("execute")
    assert "COALESCE($4::timestamptz, NOW())" in _compact(sql)


@pytest.mark.asyncio
async def test_target_logger_validates_the_kind_before_running_anything() -> None:
    conn = _Conn()

    with pytest.raises(ValueError):
        await deletion_log.log_target_memory_deletions(conn, "alice", None, requested_by="root", request_kind="wipe")

    assert conn.calls == []


# ── log_morpheus_run_memory_deletions ───────────────────────────────────────


@pytest.mark.asyncio
async def test_morpheus_logger_writes_only_live_run_owned_rows() -> None:
    conn = _Conn()

    await deletion_log.log_morpheus_run_memory_deletions(conn, "run-1", requested_by="worker", reason="rollback")

    sql, args = conn.only("execute")
    compact = _compact(sql)
    assert "INSERT INTO deletion_log" in compact
    assert "WHERE morpheus_run_id = $1::uuid" in compact
    assert "AND provenance = 'morpheus_local'" in compact
    assert "AND deleted_at IS NULL" in compact
    assert args == ("run-1", "worker", None, "admin_purge", "rollback", None)


@pytest.mark.asyncio
async def test_morpheus_logger_casts_the_run_id_and_leaves_now_to_sql() -> None:
    conn = _Conn()

    await deletion_log.log_morpheus_run_memory_deletions(conn, "run-1", requested_by="worker")

    sql, _ = conn.only("execute")
    compact = _compact(sql)
    assert ("$1::uuid") in compact
    assert "COALESCE($3::timestamptz, NOW())" in compact
    assert "$6::text[]" in compact


@pytest.mark.asyncio
async def test_morpheus_logger_takes_the_remaining_options() -> None:
    conn = _Conn()
    requested_at = datetime(2026, 5, 1, 11, 30, tzinfo=timezone.utc)

    await deletion_log.log_morpheus_run_memory_deletions(
        conn,
        "run-1",
        requested_by="worker",
        requested_at=requested_at,
        request_kind="gdpr_wipe",
        reason="rollback",
        source=["scheduler"],
    )

    _, args = conn.only("execute")
    assert args == ("run-1", "worker", requested_at, "gdpr_wipe", "rollback", ["scheduler"])


@pytest.mark.asyncio
async def test_morpheus_logger_validates_the_kind_before_running_anything() -> None:
    conn = _Conn()

    with pytest.raises(ValueError):
        await deletion_log.log_morpheus_run_memory_deletions(conn, "run-1", requested_by="worker", request_kind="nope")

    assert conn.calls == []


@pytest.mark.asyncio
async def test_the_two_writers_select_opposite_deletion_states() -> None:
    # The target writer audits rows already soft-deleted; the morpheus writer
    # audits rows that are still live because it runs before the rollback
    # deletes them. Pinned as the current split.
    target_conn = _Conn()
    morpheus_conn = _Conn()

    await deletion_log.log_target_memory_deletions(target_conn, "alice", None, requested_by="r")
    await deletion_log.log_morpheus_run_memory_deletions(morpheus_conn, "run-1", requested_by="r")

    target_sql, _ = target_conn.only("execute")
    morpheus_sql, _ = morpheus_conn.only("execute")

    assert "deleted_at IS NOT NULL" in _compact(target_sql)
    assert "deleted_at IS NULL" in _compact(morpheus_sql)


# ── fetch_deletion_log ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_counts_then_pages_with_the_time_window_only() -> None:
    conn = _Conn(count=7, rows=[("row",)])

    rows, total = await deletion_log.fetch_deletion_log(
        conn, from_ts=FROM, to_ts=TO, owner_id=None, page=1, page_size=50
    )

    count_sql, count_args = conn.only("fetchval")
    page_sql, page_args = conn.only("fetch")

    assert _compact(count_sql) == ("SELECT COUNT(*) FROM deletion_log WHERE requested_at >= $1 AND requested_at <= $2")
    assert count_args == (FROM, TO)
    assert total == 7
    assert page_args == (FROM, TO, 50, 0)
    assert rows == [("row",)]

    compact = _compact(page_sql)
    assert "FROM deletion_log WHERE requested_at >= $1 AND requested_at <= $2" in compact
    assert "ORDER BY requested_at DESC, executed_at DESC, id DESC" in compact
    assert "LIMIT $3 OFFSET $4" in compact


@pytest.mark.asyncio
async def test_fetch_adds_the_owner_filter_and_shifts_both_page_placeholders() -> None:
    conn = _Conn(count=1)

    await deletion_log.fetch_deletion_log(conn, from_ts=FROM, to_ts=TO, owner_id="alice", page=3, page_size=10)

    count_sql, count_args = conn.only("fetchval")
    page_sql, page_args = conn.only("fetch")

    assert "owner_id = $3" in _compact(count_sql)
    assert count_args == (FROM, TO, "alice")
    assert page_args == (FROM, TO, "alice", 10, 20)
    assert "LIMIT $4 OFFSET $5" in _compact(page_sql)


@pytest.mark.asyncio
async def test_fetch_reads_an_empty_string_owner_as_no_filter() -> None:
    # The filter is a truthiness test, so "" means "every owner" rather than
    # "the owner whose id is empty". Pinned as observed.
    conn = _Conn()

    await deletion_log.fetch_deletion_log(conn, from_ts=FROM, to_ts=TO, owner_id="", page=1, page_size=10)

    count_sql, count_args = conn.only("fetchval")
    assert "owner_id = $" not in _compact(count_sql)
    assert count_args == (FROM, TO)


@pytest.mark.asyncio
async def test_fetch_collapses_a_null_count_to_zero() -> None:
    conn = _Conn(count=None)

    _, total = await deletion_log.fetch_deletion_log(conn, from_ts=FROM, to_ts=TO, owner_id=None, page=1, page_size=50)

    assert total == 0
    assert isinstance(total, int)


@pytest.mark.asyncio
async def test_fetch_stringifies_a_numeric_string_count() -> None:
    conn = _Conn(count="12")

    _, total = await deletion_log.fetch_deletion_log(conn, from_ts=FROM, to_ts=TO, owner_id=None, page=1, page_size=50)

    assert total == 12


@pytest.mark.asyncio
async def test_fetch_returns_a_list_even_when_the_connection_returns_a_tuple() -> None:
    conn = _Conn(rows=(("a",), ("b",)))

    rows, _ = await deletion_log.fetch_deletion_log(conn, from_ts=FROM, to_ts=TO, owner_id=None, page=1, page_size=50)

    assert rows == [("a",), ("b",)]
    assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_fetch_does_not_validate_the_page_numbers() -> None:
    # page=0 asks for a negative offset and page_size=0 for LIMIT 0; both go to
    # the database unchanged. Pinned as observed, not repaired.
    conn = _Conn()

    await deletion_log.fetch_deletion_log(conn, from_ts=FROM, to_ts=TO, owner_id=None, page=0, page_size=0)

    _, page_args = conn.only("fetch")
    assert page_args == (FROM, TO, 0, -0)


@pytest.mark.asyncio
async def test_fetch_offset_is_page_minus_one_times_page_size() -> None:
    conn = _Conn()

    await deletion_log.fetch_deletion_log(conn, from_ts=FROM, to_ts=TO, owner_id=None, page=4, page_size=25)

    _, page_args = conn.only("fetch")
    assert page_args[2] == 25
    assert page_args[3] == 75
