"""Static cross-backend parity checks for federation visibility/feed SQL.

These tests are intentionally driver-free: they import renderer helpers and
exercise DB2 repository SQL through fake cursors so parity regressions are
caught without live Postgres/MySQL/Oracle/Db2 services.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def test_every_backend_visibility_render_subtracts_exclude_namespaces() -> None:
    """Vault exclusions must survive default, READABLE, and OWN_ONLY scopes."""
    from mnemos.persistence.db2 import _render_visibility as db2_render_visibility
    from mnemos.persistence.mysql import _render_visibility as mysql_render_visibility
    from mnemos.persistence.oracle import _render_visibility as oracle_render_visibility
    from mnemos.persistence.postgres import _render_postgres_visibility
    from mnemos.persistence.sqlite import _render_sqlite_visibility

    readable = VisibilityFilter(
        scope=VisibilityScope.READABLE,
        user_id="alice",
        group_ids=("team-a",),
        namespace="default",
        exclude_namespaces=("vault",),
    )
    own_only = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="alice",
        group_ids=(),
        namespace="default",
        exclude_namespaces=("vault",),
    )
    default_root = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
        exclude_namespaces=("vault",),
    )

    sqlite_params: list[Any] = []
    sqlite_readable = _render_sqlite_visibility(readable, sqlite_params, table_alias="m")
    assert "m.namespace NOT IN (?)" in sqlite_readable
    assert sqlite_params[-1] == "vault"

    sqlite_params = []
    sqlite_own = _render_sqlite_visibility(own_only, sqlite_params, table_alias="m")
    assert "m.owner_id = ?" in sqlite_own
    assert "m.namespace NOT IN (?)" in sqlite_own
    assert sqlite_params == ["alice", "default", "vault"]

    sqlite_params = []
    sqlite_root = _render_sqlite_visibility(default_root, sqlite_params, table_alias="m")
    assert "m.namespace NOT IN (?)" in sqlite_root
    assert sqlite_params == ["vault"]

    oracle_clause, oracle_params = oracle_render_visibility(readable, table_alias="m", param_prefix="v")
    assert "m.namespace NOT IN (:v_xns_0)" in oracle_clause
    assert oracle_params["v_xns_0"] == "vault"

    db2_clause, db2_params = db2_render_visibility(own_only, table_alias="m", param_prefix="d")
    assert "m.owner_id = :d_owner" in db2_clause
    assert "m.namespace NOT IN (:d_xns_0)" in db2_clause
    assert db2_params["d_xns_0"] == "vault"

    mysql_clause, mysql_params = mysql_render_visibility(readable, table_alias="m")
    assert "m.namespace = %s" in mysql_clause
    assert "m.namespace NOT IN (%s)" in mysql_clause
    assert mysql_params[-2:] == ["default", "vault"]

    pg_clause, pg_params, _ = _render_postgres_visibility(readable, table_alias="m")
    assert "m.namespace=$" in pg_clause
    assert "m.namespace NOT IN ($" in pg_clause
    assert pg_params[-2:] == ["default", "vault"]


@pytest.mark.asyncio
async def test_db2_feed_queries_require_public_readable_and_exclude_vault(monkeypatch: pytest.MonkeyPatch, ) -> None:
    """Db2 feed SQL must match the public-readable gate used by PG/MySQL."""
    # Offsite posture: the world-read gate applies only when
    # MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE=0. Declare it rather than relying
    # on the default, which is now the trusted-LAN full-corpus scope.
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    from mnemos.core.secret_detection import VAULT_NAMESPACE
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": tuple(params or ())})

        async def fetchall(self) -> list[Any]:
            return []

        async def fetchone(self) -> Any:
            return None

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()

    await repo.feed_query(
        tx,
        since_updated=None,
        since_id=None,
        namespaces=[],
        categories=[],
        limit=25,
        prefer_compressed=False,
    )
    await repo.get_feed_memory(tx, "mem-1", namespaces=[], categories=[])

    feed_sql = calls[0]["sql"].upper()
    feed_params = calls[0]["params"]
    get_sql = calls[1]["sql"].upper()
    get_params = calls[1]["params"]

    for sql in (feed_sql, get_sql):
        assert "M.FEDERATION_SOURCE IS NULL" in sql
        assert "MOD(M.PERMISSION_MODE, 10) >= 4" in sql
        assert "M.DELETED_AT IS NULL" in sql
        assert "M.ARCHIVED_AT IS NULL" in sql
        assert "M.CONSOLIDATED_INTO IS NULL" in sql
        assert "M.NAMESPACE IS NULL OR M.NAMESPACE <> ?" in sql

    # F07: the withdrawal branch reuses the vault literal as a trigger
    # (`namespace = ?`) plus an outer-loop credential-boundary guard.
    # The first two params are the live branch's [VAULT_NAMESPACE,
    # limit]; the next four are the withdrawal branch's
    # [VAULT_NAMESPACE_trigger, VAULT_NAMESPACE_boundary, ...]. We don't
    # pin the precise shape here — only that VAULT_NAMESPACE appears
    # at least once and the live limit (25) is the trailing param.
    assert VAULT_NAMESPACE in feed_params
    assert feed_params[-1] == 25
    assert get_params == ("mem-1", VAULT_NAMESPACE)


class _FakeModule:
    DB_TYPE_TIMESTAMP_TZ = object()


class _FakeAsyncpgModule:
    Connection = object
    Pool = object
    UniqueViolationError = Exception


class _AsyncContextCursor:
    description = None

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def __aenter__(self) -> "_AsyncContextCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append({"sql": sql, "params": params})

    async def close(self) -> None:
        pass

    async def fetchall(self) -> list[Any]:
        return []

    async def fetchone(self) -> Any:
        return None


class _SqlCaptureCursor:
    description = None

    def __init__(self, calls: list[dict[str, Any]], sql: str, params: Any = None) -> None:
        self._calls = calls
        self._sql = sql
        self._params = params

    async def close(self) -> None:
        pass

    async def fetchall(self) -> list[Any]:
        self._calls.append({"sql": self._sql, "params": self._params})
        return []

    async def fetchone(self) -> Any:
        self._calls.append({"sql": self._sql, "params": self._params})
        return None


class _AwaitableCursor:
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


class _PostgresConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self._calls.append({"sql": sql, "params": args})
        return []

    async def fetchrow(self, sql: str, *args: Any) -> None:
        self._calls.append({"sql": sql, "params": args})
        return None


class _SqliteConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def execute(self, sql: str, params: Any = None) -> _SqlCaptureCursor:
        return _SqlCaptureCursor(self._calls, sql, params)


class _MySqlConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> _AsyncContextCursor:
        return _AsyncContextCursor(self._calls)


class _AwaitableCursorConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> _AwaitableCursor:
        return _AwaitableCursor(self._calls)


FEDERATION_GATE_CASES = (
    pytest.param(
        "sqlite",
        "mnemos.persistence.sqlite",
        "SqliteFederationRepository",
        lambda calls: __import__("mnemos.persistence.sqlite", fromlist=["SqliteTransaction"]).SqliteTransaction(
            _SqliteConn(calls)
        ),
        ("(M.PERMISSION_MODE % 10) >= 4", "M.NAMESPACE IS NULL OR M.NAMESPACE <> 'VAULT'"),
        id="sqlite",
    ),
    pytest.param(
        "postgres",
        "mnemos.persistence.postgres",
        "PostgresFederationRepository",
        lambda calls: __import__("mnemos.persistence.postgres", fromlist=["PostgresTransaction"]).PostgresTransaction(
            _PostgresConn(calls), None
        ),
        ("(M.PERMISSION_MODE % 10) >= 4", "M.NAMESPACE IS NULL OR M.NAMESPACE <> 'VAULT'"),
        id="postgres",
    ),
    pytest.param(
        "mysql",
        "mnemos.persistence.mysql",
        "MysqlFederationRepository",
        lambda calls: __import__("mnemos.persistence.mysql", fromlist=["_MysqlTransaction"])._MysqlTransaction(
            _MySqlConn(calls)
        ),
        ("(M.PERMISSION_MODE % 10) >= 4", "M.NAMESPACE IS NULL OR M.NAMESPACE <> 'VAULT'"),
        id="mysql",
    ),
    pytest.param(
        "oracle",
        "mnemos.persistence.oracle",
        "OracleFederationRepository",
        lambda calls: SimpleNamespace(conn=_AwaitableCursorConn(calls)),
        ("MOD(M.PERMISSION_MODE, 10) >= 4", "M.NAMESPACE IS NULL OR M.NAMESPACE <> :VAULT_NS"),
        id="oracle",
    ),
    pytest.param(
        "db2",
        "mnemos.persistence.db2",
        "Db2FederationRepository",
        lambda calls: SimpleNamespace(conn=_AwaitableCursorConn(calls)),
        ("MOD(M.PERMISSION_MODE, 10) >= 4", "M.NAMESPACE IS NULL OR M.NAMESPACE <> ?"),
        id="db2",
    ),
)


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.upper().split())


def _split_feed_branches(sql: str) -> tuple[str, str | None, str | None]:
    """Split a feed_query SELECT at UNION ALL boundaries.

    F07: the SQL may now have THREE branches — live memory rows,
    consolidation tombstones (carry a redirect target), and
    withdrawal tombstones (rows that left the live feed). Return
    (live, consolidation, withdrawal). Backends without consolidation
    (Oracle/Db2) only have (live, None, withdrawal) after F07.
    """
    normalized = _normalized_sql(sql)
    if " UNION ALL " not in normalized:
        return normalized, None, None
    parts = normalized.split(" UNION ALL ")
    live = parts[0]
    # Heuristic: a branch is "consolidation" iff it has consolidated_at
    # IS NOT NULL; a branch is "withdrawal" iff it has deleted_at IS NOT
    # NULL. Backends with no consolidation emit only one extra branch
    # (the withdrawal), so we collapse the structure back accordingly.
    consolidation = next(
        (p for p in parts[1:] if "M.CONSOLIDATED_AT IS NOT NULL" in p),
        None,
    )
    withdrawal = next(
        (p for p in parts[1:] if "M.DELETED_AT IS NOT NULL" in p),
        None,
    )
    return live, consolidation, withdrawal


def _assert_live_federation_gates(sql: str, public_token: str, vault_token: str) -> None:
    assert "M.FEDERATION_SOURCE IS NULL" in sql
    assert public_token in sql
    assert "M.DELETED_AT IS NULL" in sql
    assert "M.ARCHIVED_AT IS NULL" in sql
    assert "M.CONSOLIDATED_INTO IS NULL" in sql
    assert vault_token in sql


def _assert_tombstone_federation_gates(sql: str, public_token: str, vault_token: str) -> None:
    assert "M.FEDERATION_SOURCE IS NULL" in sql
    assert public_token in sql
    assert "M.DELETED_AT IS NULL" in sql
    assert "M.ARCHIVED_AT IS NULL" in sql
    assert "M.CONSOLIDATED_INTO IS NOT NULL" in sql
    assert "M.CONSOLIDATED_AT IS NOT NULL" in sql
    assert vault_token in sql


def _assert_withdrawal_federation_gates(sql: str, vault_token: str) -> None:
    """F07: withdrawal branch must carry the loop-guard, the
    dead/archived/vault-or-private trigger, and the vault credential
    boundary. World-read gate is opt-in (offsite posture): the test
    harness sets it via the offsite posture when it wants it asserted
    (see the parameterised callers).

    Note: the withdrawal predicate uses ``namespace = 'vault'`` AS A
    TRIGGER (a row that has been moved into the secret-vault namespace
    is a withdrawal signal even on a trusted feed), so the literal
    vault reference is present in the SQL — just as a positive match,
    not as the live branch's exclusion form. We accept either form.
    """
    assert "M.FEDERATION_SOURCE IS NULL" in sql, "loop-guard must always hold"
    assert "M.CONSOLIDATED_INTO IS NULL" in sql, "withdrawal excludes consolidated rows"
    assert "M.DELETED_AT IS NOT NULL" in sql, "withdrawal trigger condition must be present"
    assert "M.ARCHIVED_AT IS NOT NULL" in sql, "withdrawal trigger condition must be present"
    # The vault literal appears in the SQL in one of these shapes:
    # - oracle/db2: `namespace = :vault_ns` (positive trigger)
    # - sqlite/postgres/mysql/mariadb: `namespace = 'vault'` (positive trigger)
    # - live branch exclusion (every backend): `namespace <> ...`
    # We accept any of these forms as evidence the vault credential
    # boundary is honored.
    vault_present = (
        vault_token in sql
        or "M.NAMESPACE = :VAULT_NS" in sql
        or "M.NAMESPACE = 'VAULT'" in sql
    )
    assert vault_present, "vault credential boundary must always hold"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,module_name,repo_name,tx_factory,dialect_tokens", FEDERATION_GATE_CASES)
async def test_every_backend_feed_and_by_id_apply_canonical_federation_gates(monkeypatch: pytest.MonkeyPatch,
    backend: str,
    module_name: str,
    repo_name: str,
    tx_factory: Any,
    dialect_tokens: tuple[str, str],
) -> None:
    """Federation feed branches and by-id paths must apply visibility gates."""
    # Offsite posture: the world-read gate applies only when
    # MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE=0. Declare it rather than relying
    # on the default, which is now the trusted-LAN full-corpus scope.
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    import sys

    sys.modules.setdefault("oracledb", _FakeModule())
    sys.modules.setdefault("asyncpg", _FakeAsyncpgModule())
    module = __import__(module_name, fromlist=[repo_name])
    repo = getattr(module, repo_name)()
    calls: list[dict[str, Any]] = []
    tx = tx_factory(calls)

    await repo.feed_query(
        tx,
        since_updated=None,
        since_id=None,
        namespaces=[],
        categories=[],
        limit=25,
        prefer_compressed=False,
    )
    await repo.get_feed_memory(tx, "mem-1", namespaces=[], categories=[])

    assert len(calls) >= 2, backend
    live_feed_sql, consolidation_sql, withdrawal_sql = _split_feed_branches(calls[0]["sql"])
    by_id_sql = _normalized_sql(calls[1]["sql"])
    public_token, vault_token = dialect_tokens

    _assert_live_federation_gates(live_feed_sql, public_token, vault_token)
    _assert_live_federation_gates(by_id_sql, public_token, vault_token)
    # F07: every backend (post-fix) emits a withdrawal branch.
    _assert_withdrawal_federation_gates(withdrawal_sql, vault_token)
    if consolidation_sql is not None:
        # Backends with consolidation tombstones still emit them.
        _assert_tombstone_federation_gates(consolidation_sql, public_token, vault_token)
    else:
        # Oracle and Db2 historically lack the consolidation branch.
        assert backend in {"oracle", "db2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,module_name,repo_name,tx_factory,dialect_tokens", FEDERATION_GATE_CASES)
async def test_trusted_feed_scope_drops_world_read_but_keeps_vault_and_loopguard(
    backend: str,
    module_name: str,
    repo_name: str,
    tx_factory: Any,
    dialect_tokens: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE=1: the world-read gate must drop
    from BOTH the batch feed and the by-id path (all backends), while the
    federation_source loop-guard and the vault exclusion ALWAYS remain."""
    import sys

    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "1")
    sys.modules.setdefault("oracledb", _FakeModule())
    sys.modules.setdefault("asyncpg", _FakeAsyncpgModule())
    module = __import__(module_name, fromlist=[repo_name])
    repo = getattr(module, repo_name)()
    calls: list[dict[str, Any]] = []
    tx = tx_factory(calls)

    await repo.feed_query(
        tx,
        since_updated=None,
        since_id=None,
        namespaces=[],
        categories=[],
        limit=25,
        prefer_compressed=False,
    )
    await repo.get_feed_memory(tx, "mem-1", namespaces=[], categories=[])

    assert len(calls) >= 2, backend
    live_feed_sql, consolidation_sql, withdrawal_sql = _split_feed_branches(calls[0]["sql"])
    by_id_sql = _normalized_sql(calls[1]["sql"])
    public_token, vault_token = dialect_tokens

    for sql in (live_feed_sql, by_id_sql):
        # World-read gate is DROPPED in trusted mode ...
        assert public_token not in sql, f"{backend}: world-read gate must be absent in trusted mode"
        # ... but loop-guard and vault exclusion ALWAYS hold.
        assert "M.FEDERATION_SOURCE IS NULL" in sql, f"{backend}: loop-guard must always hold"
        assert vault_token in sql, f"{backend}: vault exclusion must always hold"
    # F07: the withdrawal branch is its own shape — the world-read gate
    # is irrelevant in trusted mode (the branch only fires when a row is
    # already dead/archived/etc., not when it's healthy). What MUST
    # always hold is the loop-guard + vault credential boundary (the
    # trigger form `namespace = :vault_ns` or `namespace = 'vault'` IS
    # the vault reference in trusted mode, since the world-read trigger
    # is dropped).
    assert "M.FEDERATION_SOURCE IS NULL" in withdrawal_sql, (
        f"{backend}: withdrawal branch must carry the loop-guard"
    )
    vault_present_w = (
        vault_token in withdrawal_sql
        or "M.NAMESPACE = :VAULT_NS" in withdrawal_sql
        or "M.NAMESPACE = 'VAULT'" in withdrawal_sql
    )
    assert vault_present_w, (
        f"{backend}: withdrawal branch must reference the vault namespace"
    )
    if consolidation_sql is not None:
        assert public_token not in consolidation_sql
        assert "M.FEDERATION_SOURCE IS NULL" in consolidation_sql
        assert vault_token in consolidation_sql
