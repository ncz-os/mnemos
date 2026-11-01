"""Static cross-backend parity checks for the F-1/F-5 federation counters.

Driver-free: every backend's ``finish_sync_log`` is exercised through a fake
cursor and the captured statement is checked for two properties that a
runtime smoke test on one backend cannot see:

1. **Arity** — the number of bind placeholders in the SQL equals the number
   of parameters supplied. A mismatch raises at the driver, so it only
   shows up on the backend you happen to run against.
2. **Binding** — each ``<column> = <placeholder>`` is bound to the value
   that column is supposed to carry. This is the check that catches a
   shifted parameter list: the statement still runs, it just writes the
   wrong number into the wrong column.

The F-1 defect was exactly this: ``events_received`` was threaded through
the parameter list of the MySQL/MariaDB ``finish_sync_log`` in the wrong
position, so ``records_seen`` received ``memories_pulled`` and every
column after it was shifted by one.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest


class _CaptureCursor:
    """Records the statement and parameters of one execute() call."""

    description = None

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append({"sql": sql, "params": params})

    async def fetchall(self) -> list[Any]:
        return []

    async def fetchone(self) -> Any:
        return None

    async def close(self) -> None:
        pass


class _AsyncContextCursor(_CaptureCursor):
    """aiomysql/MariaDB style: ``async with conn.cursor() as cursor``."""

    async def __aenter__(self) -> "_AsyncContextCursor":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _MySqlConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> _AsyncContextCursor:
        return _AsyncContextCursor(self._calls)


class _AwaitableCursorConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> _CaptureCursor:
        return _CaptureCursor(self._calls)


class _SqliteConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def execute(self, sql: str, params: Any = None) -> _CaptureCursor:
        self._calls.append({"sql": sql, "params": params})
        return _CaptureCursor(self._calls)


class _PostgresConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def execute(self, sql: str, *args: Any) -> None:
        self._calls.append({"sql": sql, "params": args})

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._calls.append({"sql": sql, "params": args})
        return None


# ── placeholder counting per dialect ────────────────────────────────────────


def _count_placeholders(sql: str, dialect: str) -> int:
    """Number of bind placeholders in one statement, per dialect."""
    if dialect == "sqlite":
        return sql.count("?")
    if dialect in ("mysql", "mariadb"):
        # MariaDB is wire-compatible with MySQL and shares the same
        # ``%s`` placeholder style through aiomysql.
        return sql.count("%s")
    if dialect == "postgres":
        # $1..$n — the highest index is the number of parameters.
        indexes = [int(i) for i in re.findall(r"\$(\d+)", sql)]
        return max(indexes) if indexes else 0
    if dialect == "oracle":
        # :name binds — each distinct name is one parameter.
        return len(set(re.findall(r":(\w+)", sql)))
    if dialect == "db2":
        return sql.count("?")
    raise AssertionError(f"unknown dialect {dialect!r}")


def _bound_columns(sql: str, dialect: str) -> list[str]:
    """The column names, in statement order, that are bound to a placeholder."""
    if dialect == "postgres":
        return re.findall(r"(\w+)\s*=\s*\$\d+", sql)
    if dialect == "oracle":
        return re.findall(r"(\w+)\s*=\s*:\w+", sql)
    return re.findall(r"(\w+)\s*=\s*(?:\?|%s)", sql)


# ── one case per backend ────────────────────────────────────────────────────


FINISH_SYNC_LOG_CASES = (
    pytest.param(
        "sqlite",
        "mnemos.persistence.sqlite",
        "SqliteFederationRepository",
        lambda calls: __import__("mnemos.persistence.sqlite", fromlist=["SqliteTransaction"]).SqliteTransaction(
            _SqliteConn(calls)
        ),
        id="sqlite",
    ),
    pytest.param(
        "mysql",
        "mnemos.persistence.mysql",
        "MysqlFederationRepository",
        lambda calls: __import__("mnemos.persistence.mysql", fromlist=["_MysqlTransaction"])._MysqlTransaction(
            _MySqlConn(calls)
        ),
        id="mysql",
    ),
    pytest.param(
        "mariadb",
        "mnemos.persistence.mariadb",
        "MariadbFederationRepository",
        lambda calls: __import__("mnemos.persistence.mariadb", fromlist=["_MysqlTransaction"])._MysqlTransaction(
            _MySqlConn(calls)
        ),
        id="mariadb",
    ),
    pytest.param(
        "postgres",
        "mnemos.persistence.postgres",
        "PostgresFederationRepository",
        lambda calls: __import__("mnemos.persistence.postgres", fromlist=["PostgresTransaction"]).PostgresTransaction(
            _PostgresConn(calls), None
        ),
        id="postgres",
    ),
    pytest.param(
        "oracle",
        "mnemos.persistence.oracle",
        "OracleFederationRepository",
        lambda calls: SimpleNamespace(conn=_AwaitableCursorConn(calls)),
        id="oracle",
    ),
    pytest.param(
        "db2",
        "mnemos.persistence.db2",
        "Db2FederationRepository",
        lambda calls: SimpleNamespace(conn=_AwaitableCursorConn(calls)),
        id="db2",
    ),
)


async def _capture_finish_sync_log(
    dialect: str,
    module: str,
    class_name: str,
    tx_factory: Any,
) -> dict[str, Any]:
    """Run one backend's finish_sync_log against a fake cursor."""
    calls: list[dict[str, Any]] = []
    module_obj = __import__(module, fromlist=[class_name])
    repo = getattr(module_obj, class_name)()
    await repo.finish_sync_log(
        tx_factory(calls),
        log_id="log-1",
        memories_pulled=10,
        memories_new=4,
        memories_updated=6,
        error=None,
        cursor_after="cursor-pos-2",
        events_received=25,
        rows_without_embedding=3,
        skipped_deleted=2,
    )
    assert calls, f"{dialect}: finish_sync_log issued no statement"
    return calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("dialect", "module", "class_name", "tx_factory"), FINISH_SYNC_LOG_CASES)
async def test_finish_sync_log_binds_one_parameter_per_placeholder(
    dialect: str,
    module: str,
    class_name: str,
    tx_factory: Any,
) -> None:
    """Every backend must supply exactly one parameter per placeholder.

    The MySQL/MariaDB defect: the statement had 11 placeholders and the
    call supplied 12 values, so the driver raised on every real MySQL
    sync-log write. SQLite hid it because its own list happened to line
    up.
    """
    call = await _capture_finish_sync_log(dialect, module, class_name, tx_factory)
    sql = call["sql"]
    params = call["params"]
    # asyncpg takes the parameters as positional varargs; every other
    # driver takes them as one sequence.
    supplied = list(params or ())

    placeholders = _count_placeholders(sql, dialect)
    assert placeholders == len(supplied), (
        f"{dialect}: finish_sync_log has {placeholders} placeholders but supplied {len(supplied)} parameters"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("dialect", "module", "class_name", "tx_factory"), FINISH_SYNC_LOG_CASES)
async def test_finish_sync_log_binds_each_column_to_the_right_value(
    dialect: str,
    module: str,
    class_name: str,
    tx_factory: Any,
) -> None:
    """Each counter column must receive the value that column means.

    F-1: ``memories_pulled`` is the APPLIED count, ``events_received`` is
    the WIRE count, and ``records_seen`` is the wire count too (it is the
    historical name for it). A shifted parameter list keeps the arity
    right but writes every value into the wrong column.
    """
    call = await _capture_finish_sync_log(dialect, module, class_name, tx_factory)
    sql = call["sql"]
    params = call["params"]
    supplied = list(params or ())

    if dialect == "postgres":
        # Postgres binds $1..$n positionally, but the numbering does not
        # start at the SET clause: this statement uses $1 for the WHERE
        # id and numbers the SET columns from $2. Map each column to
        # its own placeholder index rather than to statement order.
        binds = {}
        for column, index in re.findall(r"(\w+)\s*=\s*\$(\d+)", sql):
            binds[column] = supplied[int(index) - 1]
    elif dialect == "oracle":
        # Oracle binds by name: the statement says ``col = :name`` and
        # the parameter dict says ``"name": value``.
        pairs = re.findall(r"(\w+)\s*=\s*:(\w+)", sql)
        mapping = params if isinstance(params, dict) else {}
        binds = {column: mapping.get(name) for column, name in pairs}
    else:
        columns = _bound_columns(sql, dialect)
        binds = dict(zip(columns, supplied))

    assert binds.get("memories_pulled") == 10, (
        f"{dialect}: memories_pulled must carry the APPLIED count (10), got {binds.get('memories_pulled')!r}"
    )
    assert binds.get("memories_new") == 4, f"{dialect}: memories_new must carry 4, got {binds.get('memories_new')!r}"
    assert binds.get("memories_updated") == 6, (
        f"{dialect}: memories_updated must carry 6, got {binds.get('memories_updated')!r}"
    )
    assert binds.get("events_received") == 25, (
        f"{dialect}: events_received must carry the WIRE count (25), got {binds.get('events_received')!r}"
    )
    assert binds.get("rows_without_embedding") == 3, (
        f"{dialect}: rows_without_embedding must carry 3, got {binds.get('rows_without_embedding')!r}"
    )
    assert binds.get("skipped_deleted") == 2, (
        f"{dialect}: skipped_deleted must carry 2, got {binds.get('skipped_deleted')!r}"
    )
    # records_seen is the historical name for the wire count.
    if "records_seen" in binds:
        assert binds.get("records_seen") == 25, (
            f"{dialect}: records_seen must carry the WIRE count (25), got {binds.get('records_seen')!r}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("dialect", "module", "class_name", "tx_factory"), FINISH_SYNC_LOG_CASES)
async def test_finish_sync_log_binds_the_legacy_columns_to_their_historical_meaning(
    dialect: str,
    module: str,
    class_name: str,
    tx_factory: Any,
) -> None:
    """The legacy ``records_seen`` / ``records_written`` columns keep their meaning.

    Only SQLite and MySQL/MariaDB have these columns (Postgres, Oracle
    and Db2 never had them), and nothing in the codebase reads them --
    they are write-only continuity fields. That makes them easy to
    mis-bind: a shifted tuple still passes an arity check while writing
    the wire count into the APPLIED column and vice versa.

    ``records_seen`` is the historical name for the WIRE count, so it
    carries ``events_received``. ``records_written`` is the historical
    name for the APPLIED count, so it carries ``memories_new +
    memories_updated`` -- the same value ``memories_pulled`` carries,
    because the sync only counts rows the durable sequence fence
    actually applied.
    """
    call = await _capture_finish_sync_log(dialect, module, class_name, tx_factory)
    sql = call["sql"]
    params = call["params"]
    supplied = list(params or ())

    if dialect == "postgres":
        binds = {column: supplied[int(index) - 1] for column, index in re.findall(r"(\w+)\s*=\s*\$(\d+)", sql)}
    elif dialect == "oracle":
        pairs = re.findall(r"(\w+)\s*=\s*:(\w+)", sql)
        mapping = params if isinstance(params, dict) else {}
        binds = {column: mapping.get(name) for column, name in pairs}
    else:
        binds = dict(zip(_bound_columns(sql, dialect), supplied))

    if "records_seen" not in binds:
        # Postgres/Oracle/Db2 never had the legacy columns.
        assert "records_written" not in binds, dialect
        return

    assert binds["records_seen"] == 25, (
        f"{dialect}: records_seen is the historical WIRE count and must carry "
        f"events_received (25), got {binds['records_seen']!r}"
    )
    assert binds["records_written"] == 10, (
        f"{dialect}: records_written is the historical APPLIED count and must carry "
        f"memories_new + memories_updated (4 + 6 = 10), got {binds['records_written']!r}"
    )
    # The legacy APPLIED column and the F-1 applied column must agree.
    assert binds["records_written"] == binds["memories_pulled"], (
        f"{dialect}: records_written ({binds['records_written']!r}) and memories_pulled "
        f"({binds['memories_pulled']!r}) are both the APPLIED count and must be equal"
    )


# ── F-2: the soft-deleted replica marker, per dialect ──────────────────────
#
# ``fetch_federated_memory_marker`` is the receiver's existence probe: it
# decides "insert" vs "update" for every inbound event. Postgres, MySQL and
# Db2 filtered ``deleted_at IS NULL`` out of it, so a replica this node had
# soft-deleted looked ABSENT — the insert-or-ignore wrote nothing, the row
# stayed deleted, and the event disappeared with no counter anywhere. SQLite
# never had the filter, so the two families disagreed. The probe must return
# the row, deleted or not, on every dialect (the deleted STATE travels back
# so the receiver can tell).

MARKER_CASES = (
    pytest.param(
        "sqlite",
        "mnemos.persistence.sqlite",
        "SqliteFederationRepository",
        lambda calls: __import__("mnemos.persistence.sqlite", fromlist=["SqliteTransaction"]).SqliteTransaction(
            _SqliteConn(calls)
        ),
        id="sqlite",
    ),
    pytest.param(
        "mysql",
        "mnemos.persistence.mysql",
        "MysqlFederationRepository",
        lambda calls: __import__("mnemos.persistence.mysql", fromlist=["_MysqlTransaction"])._MysqlTransaction(
            _MySqlConn(calls)
        ),
        id="mysql",
    ),
    pytest.param(
        "mariadb",
        "mnemos.persistence.mariadb",
        "MariadbFederationRepository",
        lambda calls: __import__("mnemos.persistence.mariadb", fromlist=["_MysqlTransaction"])._MysqlTransaction(
            _MySqlConn(calls)
        ),
        id="mariadb",
    ),
    pytest.param(
        "postgres",
        "mnemos.persistence.postgres",
        "PostgresFederationRepository",
        lambda calls: __import__("mnemos.persistence.postgres", fromlist=["PostgresTransaction"]).PostgresTransaction(
            _PostgresConn(calls), None
        ),
        id="postgres",
    ),
    pytest.param(
        "oracle",
        "mnemos.persistence.oracle",
        "OracleFederationRepository",
        lambda calls: SimpleNamespace(conn=_AwaitableCursorConn(calls)),
        id="oracle",
    ),
    pytest.param(
        "db2",
        "mnemos.persistence.db2",
        "Db2FederationRepository",
        lambda calls: SimpleNamespace(conn=_AwaitableCursorConn(calls)),
        id="db2",
    ),
)


async def _capture_marker(dialect: str, module: str, class_name: str, tx_factory: Any) -> str:
    """Run one backend's marker lookup against a fake cursor, return the SQL."""
    calls: list[dict[str, Any]] = []
    module_obj = __import__(module, fromlist=[class_name])
    repo = getattr(module_obj, class_name)()
    await repo.fetch_federated_memory_marker(tx_factory(calls), "fed:pythia:m1")
    assert calls, f"{dialect}: fetch_federated_memory_marker issued no statement"
    return calls[0]["sql"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("dialect", "module", "class_name", "tx_factory"), MARKER_CASES)
async def test_marker_lookup_does_not_hide_a_soft_deleted_replica(
    dialect: str,
    module: str,
    class_name: str,
    tx_factory: Any,
) -> None:
    """No dialect may filter ``deleted_at`` out of the existence probe.

    A soft-deleted replica is still a row. Filtering it out makes the
    receiver try an insert the primary key refuses, and the inbound event is
    then applied by nobody and counted by nothing (the SQLite-vs-Postgres
    divergence this pins).
    """
    sql = await _capture_marker(dialect, module, class_name, tx_factory)
    assert "deleted_at is null" not in " ".join(sql.lower().split()), (
        f"{dialect}: the marker lookup must not filter on deleted_at:\n{sql}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("dialect", "module", "class_name", "tx_factory"), MARKER_CASES)
async def test_marker_lookup_returns_the_deleted_state(
    dialect: str,
    module: str,
    class_name: str,
    tx_factory: Any,
) -> None:
    """The probe must carry the deleted state so the receiver can tell.

    The receiver distinguishes "no such replica" (insert) from "replica
    deleted on this node" (count the skip). Both are now reachable branches,
    so the state has to travel with the marker on every dialect.
    """
    sql = " ".join((await _capture_marker(dialect, module, class_name, tx_factory)).split()).lower()
    assert "deleted_at" in sql, f"{dialect}: the marker must select deleted_at:\n{sql}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("dialect", "module", "class_name", "tx_factory"), FINISH_SYNC_LOG_CASES)
async def test_finish_sync_log_writes_the_skipped_deleted_column(
    dialect: str,
    module: str,
    class_name: str,
    tx_factory: Any,
) -> None:
    """``skipped_deleted`` must be a real column of the sync-log write."""
    call = await _capture_finish_sync_log(dialect, module, class_name, tx_factory)
    assert "skipped_deleted" in call["sql"], f"{dialect}: finish_sync_log must write skipped_deleted"
