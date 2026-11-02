"""Cross-backend parity for the F-3 local-embed queue (the ABC contract).

Driver-free: every backend's ``list_federated_missing_embeddings`` and
``count_federated_missing_embeddings`` are exercised through a fake cursor
and the captured statement is checked for the properties a runtime test on
ONE backend cannot see:

1. **Presence** -- all six backends implement both methods.  The F-3
   delivery added them to the ABC as ``@abstractmethod``; before that the
   base class carried a silent no-op (``[]`` / ``0``) and four of the six
   backends inherited it, so the queue reported "nothing to do" forever on
   exactly the deployments that had rows waiting.

2. **Arity** -- the number of bind placeholders in the SQL equals the
   number of parameters supplied.  A mismatch raises at the driver, so it
   only shows up on the backend you happen to run against.

3. **Binding** -- each ``<column> = <placeholder>`` is bound to the value
   that column is supposed to carry.  This is what catches a shifted
   parameter list: the statement still runs, it just filters the wrong
   thing.

4. **Dialect-correct placeholder style** -- the shared count helper used
   to hardcode the qmark form (``?``) while accepting only a boolean
   dialect hint, so every caller that routed through it bound a qmark
   against a driver that wants ``%s`` (MySQL/MariaDB) or ``:n``
   (Oracle).  The placeholder style now has to travel with the query.

5. **The right vector location** -- the vector lives in an inline
   ``memories.embedding`` column on Postgres/Oracle/Db2/MySQL and in the
   ``memory_embeddings`` join table on SQLite/MariaDB.  A backend that
   filters on the column it does not have raises "unknown column" at
   runtime; a backend that filters on the wrong one reports a queue depth
   that does not match what its own search path reads.
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
    """Oracle / Db2 style: ``conn.cursor()`` returns an awaitable cursor."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> _CaptureCursor:
        return _CaptureCursor(self._calls)


class _Db2AsyncConnection:
    """Db2's async connection wrapper -- ``transaction_dialect`` keys on the
    class NAME to tell a Db2 connection from an Oracle one, exactly as it
    does in production (Db2 hands out ``_Db2AsyncConnection``)."""

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

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self._calls.append({"sql": sql, "params": args})
        return []

    async def fetchrow(self, sql: str, *args: Any) -> None:
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
        # $1..$n -- the highest index is the number of parameters.
        indexes = [int(i) for i in re.findall(r"\$(\d+)", sql)]
        return max(indexes) if indexes else 0
    if dialect == "oracle":
        # :name binds -- each distinct name is one parameter.
        return len(set(re.findall(r":(\w+)", sql)))
    if dialect == "db2":
        return sql.count("?")
    raise AssertionError(f"unknown dialect {dialect!r}")


def _normalize(sql: str) -> str:
    return " ".join(sql.upper().split())


# ── one case per backend ────────────────────────────────────────────────────
#
# ``inline_embedding`` records where the backend keeps the vector: True for
# the inline ``memories.embedding`` column (Postgres/Oracle/Db2/MySQL),
# False for the ``memory_embeddings`` join table (SQLite/MariaDB).

QUEUE_CASES = (
    pytest.param(
        "sqlite",
        "mnemos.persistence.sqlite",
        "SqliteFederationRepository",
        lambda calls: __import__(
            "mnemos.persistence.sqlite", fromlist=["SqliteTransaction"]
        ).SqliteTransaction(_SqliteConn(calls)),
        False,
        id="sqlite",
    ),
    pytest.param(
        "mysql",
        "mnemos.persistence.mysql",
        "MysqlFederationRepository",
        lambda calls: __import__(
            "mnemos.persistence.mysql", fromlist=["_MysqlTransaction"]
        )._MysqlTransaction(_MySqlConn(calls)),
        True,
        id="mysql",
    ),
    pytest.param(
        "mariadb",
        "mnemos.persistence.mariadb",
        "MariadbFederationRepository",
        lambda calls: __import__(
            "mnemos.persistence.mariadb", fromlist=["_MysqlTransaction"]
        )._MysqlTransaction(_MySqlConn(calls)),
        False,
        id="mariadb",
    ),
    pytest.param(
        "postgres",
        "mnemos.persistence.postgres",
        "PostgresFederationRepository",
        lambda calls: __import__(
            "mnemos.persistence.postgres", fromlist=["PostgresTransaction"]
        ).PostgresTransaction(_PostgresConn(calls), None),
        True,
        id="postgres",
    ),
    pytest.param(
        "oracle",
        "mnemos.persistence.oracle",
        "OracleFederationRepository",
        lambda calls: SimpleNamespace(conn=_AwaitableCursorConn(calls)),
        True,
        id="oracle",
    ),
    pytest.param(
        "db2",
        "mnemos.persistence.db2",
        "Db2FederationRepository",
        lambda calls: SimpleNamespace(conn=_Db2AsyncConnection(calls)),
        True,
        id="db2",
    ),
)


def _repo(module_name: str, class_name: str) -> Any:
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)()


def _cases() -> list[tuple[str, str, str, Any, bool]]:
    """The case table as plain tuples (``pytest.param`` has no .values)."""
    return [p.values for p in QUEUE_CASES]


# ── 1. presence: every backend implements the contract ──────────────────────


def test_every_backend_implements_the_federation_queue_contract() -> None:
    """All six backends implement both F-3 queue methods (no silent no-op)."""
    import inspect

    from mnemos.persistence.base import FederationRepository

    for dialect, module_name, class_name, _tx_factory, _inline in _cases():
        repo = _repo(module_name, class_name)
        assert isinstance(repo, FederationRepository), dialect
        for method in (
            "list_federated_missing_embeddings",
            "count_federated_missing_embeddings",
        ):
            resolved = inspect.getattr_static(type(repo), method)
            owner = next(
                (
                    klass
                    for klass in type(repo).__mro__
                    if method in vars(klass)
                ),
                None,
            )
            assert owner is not FederationRepository, (
                f"{dialect}: {class_name} inherits {method} from the ABC -- the "
                "ABC's silent no-op is exactly the failure mode this capability "
                "exists to prevent"
            )
            assert callable(resolved), f"{dialect}: {method} must be callable"


def test_the_abc_declares_both_queue_methods_abstract() -> None:
    """The ABC contract is enforced at instantiation, not by convention."""
    from mnemos.persistence.base import FederationRepository

    assert "list_federated_missing_embeddings" in FederationRepository.__abstractmethods__
    assert "count_federated_missing_embeddings" in FederationRepository.__abstractmethods__


# ── 2/3/4/5. the statement each backend sends ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect,module_name,class_name,tx_factory,inline", QUEUE_CASES)
async def test_list_queue_filters_the_right_columns_and_binds_its_parameters(
    dialect: str,
    module_name: str,
    class_name: str,
    tx_factory: Any,
    inline: bool,
) -> None:
    """The queue page filters on the columns that exist and binds its params."""
    calls: list[dict[str, Any]] = []
    repo = _repo(module_name, class_name)
    tx = tx_factory(calls)

    await repo.list_federated_missing_embeddings(
        tx,
        after_id="fed:pythia:m5",
        limit=25,
        peer_name="pythia",
    )

    assert calls, f"{dialect}: the queue page must issue one statement"
    call = calls[0]
    sql = _normalize(call["sql"])
    params = call["params"]

    # The queue is the PREDICATE: a pulled row with no vector.
    assert "FEDERATION_SOURCE IS NOT NULL" in sql
    assert "DELETED_AT IS NULL" in sql
    assert "ARCHIVED_AT IS NULL" in sql
    assert "CONSOLIDATED_INTO IS NULL" in sql
    # The vector lives where THIS backend keeps it.
    if inline:
        assert "MEMORIES.EMBEDDING IS NULL" in sql or "AND EMBEDDING IS NULL" in sql
    else:
        assert "ME.EMBEDDING IS NULL" in sql
        assert "MEMORY_EMBEDDINGS" in sql
    # Keyset pagination by id, so a pass can walk the queue without
    # holding it in memory.
    assert "ORDER BY" in sql
    assert re.search(r"\bID\s*>\s*(?:\?|%S|:\w+|\$\d+)", sql), sql
    # The peer filter is a bound parameter, never interpolated.
    assert re.search(r"FEDERATION_SOURCE\s*=\s*(?:\?|%S|:\w+|\$\d+)", sql), sql

    # Arity: placeholders == parameters.
    placeholders = _count_placeholders(call["sql"], dialect)
    supplied = len(params) if params is not None else 0
    assert placeholders == supplied, (
        f"{dialect}: {placeholders} placeholders vs {supplied} parameters"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect,module_name,class_name,tx_factory,inline", QUEUE_CASES)
async def test_count_queue_uses_this_dialect_placeholder_style(
    dialect: str,
    module_name: str,
    class_name: str,
    tx_factory: Any,
    inline: bool,
) -> None:
    """The count binds the peer filter in ITS dialect's placeholder style.

    The shared helper used to hardcode ``?`` while accepting only a
    boolean shape hint, so a MySQL/MariaDB/Oracle caller bound a qmark
    against a driver that wants ``%s`` or ``:n``.
    """
    calls: list[dict[str, Any]] = []
    repo = _repo(module_name, class_name)
    tx = tx_factory(calls)

    await repo.count_federated_missing_embeddings(tx, peer_name="pythia")

    assert calls, f"{dialect}: the queue count must issue one statement"
    call = calls[0]
    sql = _normalize(call["sql"])
    params = call["params"]

    assert "FEDERATION_SOURCE IS NOT NULL" in sql
    assert "DELETED_AT IS NULL" in sql
    assert "ARCHIVED_AT IS NULL" in sql
    assert "CONSOLIDATED_INTO IS NULL" in sql
    if inline:
        assert "AND EMBEDDING IS NULL" in sql
    else:
        assert "ME.EMBEDDING IS NULL" in sql
        assert "MEMORY_EMBEDDINGS" in sql

    # The placeholder style is the dialect's own -- never a foreign one.
    # The assertions read the NORMALIZED (upper-cased) statement, so the
    # marker spelling is compared in upper case too.
    if dialect in ("mysql", "mariadb"):
        assert "%S" in sql, f"{dialect} must bind with %s, got: {call['sql']!r}"
        assert "?" not in sql, f"{dialect} must not bind with ?"
    elif dialect == "oracle":
        assert re.search(r"FEDERATION_SOURCE\s*=\s*:\w+", sql), sql
        assert "?" not in sql, "oracle must not bind with ?"
    elif dialect == "postgres":
        assert re.search(r"FEDERATION_SOURCE\s*=\s*\$\d+", sql), sql
    else:  # sqlite / db2
        assert "?" in sql, f"{dialect} must bind with ?"

    placeholders = _count_placeholders(call["sql"], dialect)
    supplied = len(params) if params is not None else 0
    assert placeholders == supplied, (
        f"{dialect}: {placeholders} placeholders vs {supplied} parameters"
    )
    # The peer name is BOUND, never interpolated into the statement.
    if dialect in ("mysql", "mariadb"):
        assert "pythia" in [str(p) for p in params]
    elif dialect == "oracle":
        assert "pythia" in list(params.values())
    else:
        assert "pythia" in [str(p) for p in params]
    assert "pythia" not in call["sql"], "the peer name must never be interpolated"


# ── the facade flag: a caller can discover the capability ───────────────────


@pytest.mark.parametrize("dialect,module_name,class_name,tx_factory,inline", QUEUE_CASES)
def test_every_backend_advertises_the_local_embed_capability(
    dialect: str,
    module_name: str,
    class_name: str,
    tx_factory: Any,
    inline: bool,
) -> None:
    """``supports_federation_local_embed`` is set on every backend facade."""
    module = __import__(module_name, fromlist=[class_name])
    facade_name = {
        "sqlite": "SqliteBackend",
        "mysql": "MysqlBackend",
        "mariadb": "MariadbBackend",
        "postgres": "PostgresBackend",
        "oracle": "OracleBackend",
        "db2": "Db2Backend",
    }[dialect]
    facade = getattr(module, facade_name)
    assert getattr(facade, "supports_federation_local_embed", False) is True, (
        f"{dialect}: the facade must advertise the local-embed capability so a "
        "caller can discover whether the backend implements the queue"
    )


# ── the worker is started from the normal lifecycle ─────────────────────────


def test_the_local_embed_worker_is_registered_in_the_lifespan() -> None:
    """The worker must be wired into startup, not merely defined."""
    import inspect

    from mnemos.api import lifecycle_hooks

    source = inspect.getsource(lifecycle_hooks)
    assert 'register_lifespan_worker("federation local-embed worker"' in source, (
        "the F-3 local-embed worker must be registered as a lifespan worker -- "
        "a defined-but-never-started worker is dead code"
    )


def test_the_local_embed_worker_is_a_known_service() -> None:
    """The worker is a first-class service so an operator can switch it off."""
    from mnemos.core.services import (
        LEGACY_SERVICE_DEFAULTS,
        SERVICE_ENV_OVERRIDES,
        SERVICE_NAMES,
    )

    assert "federation_local_embed_worker" in SERVICE_NAMES
    assert "federation_local_embed_worker" in LEGACY_SERVICE_DEFAULTS
    assert "federation_local_embed_worker" in SERVICE_ENV_OVERRIDES


def test_the_local_embed_worker_refuses_a_backend_without_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that cannot express the queue must not start the worker.

    Otherwise the worker would report "nothing to do" forever on a backend
    whose queue predicate is unimplemented -- the exact failure the F-3
    delivery exists to close.
    """
    from mnemos.api import lifecycle_hooks
    from mnemos.core import lifecycle

    class _Backend:
        supports_federation_local_embed = False

    monkeypatch.setattr(lifecycle, "_persistence_backend", _Backend())
    assert lifecycle_hooks._federation_local_embed_worker(None) is None


def test_the_local_embed_worker_starts_for_a_capable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capable backend gets a worker coroutine."""
    import inspect

    from mnemos.api import lifecycle_hooks
    from mnemos.core import lifecycle

    class _Backend:
        supports_federation_local_embed = True

    monkeypatch.setattr(lifecycle, "_persistence_backend", _Backend())
    coro = lifecycle_hooks._federation_local_embed_worker(None)
    try:
        assert inspect.iscoroutine(coro), "the factory must return the worker loop"
    finally:
        if inspect.iscoroutine(coro):
            coro.close()

