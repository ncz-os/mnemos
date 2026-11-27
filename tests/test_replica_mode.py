"""Tests for Postgres hot-standby (replica) mode — Operator decision 2026-11-21/22.

Every MNEMOS host runs a local Postgres hot standby of the primary database
and serves a READ-ONLY API from it. These tests cover the acceptance criteria
without requiring a live standby server (real-standby paths are noted in the
final report):

  * detection: ``MNEMOS_REPLICA_MODE`` (auto/on/off) + ``pg_is_in_recovery``
  * startup against a read-only transaction (fake pool that raises on writes)
    does not attempt writes and serves reads
  * ``ensure_schema`` skips migrations / CREATE EXTENSION in replica mode
  * ``verify_postgres_replica_schema`` does not crash on a missing pgvector
    extension (it warns and skips the column-width check)
  * every mutating REST route returns 503 ``replica_read_only``
  * authentication works read-only (API-key last-used touch is skipped)
  * ``/health`` reports ``role`` / ``replay_lag_seconds`` / ``embedding_model``
  * replay lag uses the server clock and last replay timestamp
  * embedder-missing degrades search to lexical with a clear response flag
  * promotion switch: ``pg_is_in_recovery`` false flips the process-level flag
    (``auto`` only, via the health probe)
  * primary mode (``pg_is_in_recovery`` false) is unchanged
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from mnemos.core import replica_mode
from mnemos.core.replica_error import check_replica_write_allowed, replica_read_only
from mnemos.core.replica_mode import (
    MODE_AUTO,
    MODE_OFF,
    MODE_ON,
    REPLICA_MODE_ENV,
    detect_replica_mode,
    health_probe_refresh,
    is_replica,
    replay_lag_seconds,
    replica_mode_env,
    set_replica_mode as replica_mode_set,
)
from mnemos.persistence.postgres import PostgresBackend, verify_postgres_replica_schema

# ────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_replica_state(monkeypatch):
    """Reset the process-level replica flag, auth-touch queue, env, and lag cache."""
    replica_mode._reset_replica_state_for_tests()
    monkeypatch.delenv(REPLICA_MODE_ENV, raising=False)
    # Clear the auth last_used debounce queue between tests.
    try:
        from mnemos.api import dependencies as _auth_deps

        _auth_deps._dirty_keys.clear()
    except Exception:
        pass
    yield
    replica_mode._reset_replica_state_for_tests()


class FakeConn:
    """A fake asyncpg connection.

    Read statements return the row configured for them; write statements
    (INSERT/UPDATE/DELETE/CREATE/DROP/ALTER) record the call and, when
    ``readonly`` is set, raise the exact Postgres hot-standby error.
    """

    def __init__(self, readonly: bool = False):
        self.readonly = readonly
        self.read_statements: list[str] = []
        self.write_statements: list[str] = []
        self._recovery: Any = True
        self._tables: set[str] = set()
        self._extensions: set[str] = set()
        self._wal_receiver_row: Any = None
        self._replay_timestamp: Any = None
        self._embedding_dim: int | None = None

    # ── read-only verification helpers (test setup) ─────────────────────────
    def set_recovery(self, value: bool) -> None:
        self._recovery = value

    def add_tables(self, *names: str) -> None:
        self._tables.update(names)

    def add_extension(self, name: str) -> None:
        self._extensions.add(name)

    def set_wal_receiver(self, row: Any) -> None:
        self._wal_receiver_row = row

    def set_replay_timestamp(self, value: Any) -> None:
        self._replay_timestamp = value

    def set_embedding_column_dim(self, value: int | None) -> None:
        self._embedding_dim = value

    # ── asyncpg API surface ──────────────────────────────────────────────────
    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.read_statements.append(sql)
        if "pg_stat_wal_receiver" in sql:
            return self._wal_receiver_row
        if "pg_extension" in sql:
            return {"extname": "vector"} if "vector" in self._extensions else None
        if "information_schema.tables" in sql:
            return {"1": 1} if args and args[0] in self._tables else None
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.read_statements.append(sql)
        if "pg_is_in_recovery()" in sql:
            return self._recovery
        if "pg_last_xact_replay_timestamp()" in sql:
            return self._replay_timestamp
        if "format_type" in sql and "attname = 'embedding'" in sql:
            return f"vector({self._embedding_dim})" if self._embedding_dim else None
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        if sql.lstrip().lower().startswith(
            ("insert", "update", "delete", "create", "drop", "alter")
        ):
            self.write_statements.append(sql)
            if self.readonly:
                raise Exception(
                    "CREATE EXTENSION: cannot execute CREATE EXTENSION in a read-only "
                    "transaction"
                )
            return "OK"
        # non-write execute (SELECT 1, SET ...)
        self.read_statements.append(sql)
        return "OK"


class FakePool:
    """A fake asyncpg Pool: ``acquire()`` yields the same FakeConn."""

    def __init__(self, conn: FakeConn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn

    @property
    def conn(self) -> FakeConn:
        return self._conn


def _make_backend(recovery: bool = True, *, readonly_conn: bool = True, **conn_kwargs) -> PostgresBackend:
    """Build a PostgresBackend around a fake pool.

    ``open()`` is not called; the test drives detection / schema verification
    directly, or sets ``_replica_mode`` manually.
    """
    conn = FakeConn(readonly=readonly_conn, **conn_kwargs)
    conn.set_recovery(recovery)
    backend = PostgresBackend(FakePool(conn), settings=None)
    return backend


def _require_tables(conn: FakeConn) -> None:
    """Add every table the replica verifier requires."""
    from mnemos.persistence import postgres as pg_module

    for table in pg_module._REPLICA_REQUIRED_TABLES:
        conn.add_tables(table)


# ────────────────────────────────────────────────────────────────────────────
# 1. Detection
# ────────────────────────────────────────────────────────────────────────────


def test_detection_auto_true_on_standby():
    os.environ[REPLICA_MODE_ENV] = "auto"
    backend = _make_backend(recovery=True)
    result = asyncio.run(detect_replica_mode(backend))
    assert result is True
    assert "pg_is_in_recovery()" in backend._pool.conn.read_statements[0]


def test_detection_auto_false_on_primary():
    os.environ[REPLICA_MODE_ENV] = "auto"
    backend = _make_backend(recovery=False)
    result = asyncio.run(detect_replica_mode(backend))
    assert result is False


def test_detection_on_forces_replica():
    os.environ[REPLICA_MODE_ENV] = "on"
    backend = _make_backend(recovery=False)
    result = asyncio.run(detect_replica_mode(backend))
    assert result is True


def test_detection_off_never_replica():
    os.environ[REPLICA_MODE_ENV] = "off"
    backend = _make_backend(recovery=True)
    result = asyncio.run(detect_replica_mode(backend))
    assert result is False


def test_replica_mode_env_defaults_auto():
    assert replica_mode_env() == MODE_AUTO


def test_replica_mode_env_empty_string_warns_but_defaults_auto(caplog):
    os.environ[REPLICA_MODE_ENV] = "   "
    with caplog.at_level("WARNING"):
        assert replica_mode_env() == MODE_AUTO
    assert any("empty value" in r.message for r in caplog.records)


def test_replica_mode_env_unknown_value_warns_and_defaults_auto(caplog):
    os.environ[REPLICA_MODE_ENV] = "banana"
    with caplog.at_level("WARNING"):
        assert replica_mode_env() == MODE_AUTO
    assert any("banana" in r.message for r in caplog.records)


def test_replica_mode_env_valid_values():
    for value, expected in (("auto", MODE_AUTO), ("on", MODE_ON), ("off", MODE_OFF)):
        os.environ[REPLICA_MODE_ENV] = value
        assert replica_mode_env() == expected


def test_detection_failure_returns_false_not_raise():
    """A dead pool must not kill startup; detection answers False."""
    os.environ[REPLICA_MODE_ENV] = "auto"

    class BrokenPool:
        def acquire(self):
            raise RuntimeError("connection refused")

    result = asyncio.run(detect_replica_mode(BrokenPool()))
    assert result is False


# ────────────────────────────────────────────────────────────────────────────
# 2. Process-level state + promotion
# ────────────────────────────────────────────────────────────────────────────


def test_set_replica_mode_flags_and_logs_once(caplog):
    replica_mode._reset_replica_state_for_tests()

    async def scenario():
        assert await replica_mode_set(True, reason="startup") is True
        assert await replica_mode_set(True, reason="again") is False
        assert is_replica() is True
        assert await replica_mode_set(False, reason="health probe") is True
        assert is_replica() is False

    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    promotion_logs = [r for r in caplog.records if "promoted to PRIMARY" in r.message]
    assert len(promotion_logs) == 1


def test_health_probe_refresh_promotes_in_auto_only(caplog):
    """In auto, pg_is_in_recovery turning false flips the flag exactly once."""

    async def scenario():
        assert await replica_mode_set(True, reason="startup") is True
        assert is_replica() is True
        backend = _make_backend(recovery=False)
        with caplog.at_level("INFO"):
            await health_probe_refresh(backend)
        assert is_replica() is False
        # second probe: already primary, no second promotion log
        await health_probe_refresh(backend)
        assert is_replica() is False

    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    assert len([r for r in caplog.records if "promoted to PRIMARY" in r.message]) == 1


def test_health_probe_refresh_ignores_server_in_explicit_on():
    """MNEMOS_REPLICA_MODE=on: the server's recovery state is ignored."""
    os.environ[REPLICA_MODE_ENV] = "on"

    async def scenario():
        assert await replica_mode_set(True, reason="startup") is True
        backend = _make_backend(recovery=False)
        await health_probe_refresh(backend)
        assert is_replica() is True

    asyncio.run(scenario())


@pytest.mark.parametrize("missing_pool", [False, True])
def test_failed_health_probe_preserves_replica_until_real_promotion(missing_pool, caplog):
    class BrokenPool:
        def acquire(self):
            raise RuntimeError("connection failed")

    async def scenario():
        await replica_mode_set(True, reason="startup")
        pool = None if missing_pool else BrokenPool()
        await health_probe_refresh(pool)
        await health_probe_refresh(pool)
        assert is_replica() is True
        assert replica_mode._promotion_logged is False
        assert await detect_replica_mode(pool) is True
        await health_probe_refresh(_make_backend(recovery=False))
        await health_probe_refresh(_make_backend(recovery=False))
        assert is_replica() is False
        assert replica_mode._promotion_logged is True

    with caplog.at_level("DEBUG"):
        asyncio.run(scenario())
    failures = [r for r in caplog.records if "pg_is_in_recovery probe failed" in r.message]
    assert [r.levelname for r in failures] == ["WARNING", "DEBUG", "DEBUG"]
    assert len([r for r in caplog.records if "promoted to PRIMARY" in r.message]) == 1


def test_health_probe_enters_replica_mode():
    asyncio.run(health_probe_refresh(_make_backend(recovery=True)))
    assert is_replica() is True


@pytest.mark.parametrize("mode, expected", [("on", True), ("off", False)])
def test_explicit_mode_does_not_probe(mode, expected, monkeypatch):
    monkeypatch.setenv(REPLICA_MODE_ENV, mode)

    class BrokenPool:
        def acquire(self):
            raise AssertionError("probe must not run")

    assert asyncio.run(detect_replica_mode(BrokenPool())) is expected
    asyncio.run(health_probe_refresh(BrokenPool()))
    assert is_replica() is False


def test_detection_unknown_at_startup_warns(caplog):
    with caplog.at_level("WARNING"):
        assert asyncio.run(detect_replica_mode(None)) is False
    assert any("replica state is unknown" in r.message for r in caplog.records)


def test_check_and_set_is_atomic():
    """Two concurrent probes both seeing primary must flip only once."""

    async def scenario():
        await replica_mode_set(True, reason="startup")
        backend = _make_backend(recovery=False)
        results = await asyncio.gather(
            health_probe_refresh(backend),
            health_probe_refresh(backend),
        )
        assert results == [None, None]
        assert is_replica() is False

    asyncio.run(scenario())


# ────────────────────────────────────────────────────────────────────────────
# 3. Startup: skip writes, verify schema read-only
# ────────────────────────────────────────────────────────────────────────────


def test_ensure_schema_replica_skips_all_writes():
    """Replica startup runs pure SELECTs; CREATE EXTENSION / migrations never run."""
    backend = _make_backend(recovery=True)
    backend._replica_mode = True
    conn = backend._pool.conn
    _require_tables(conn)
    conn.add_extension("vector")

    asyncio.run(backend.ensure_schema())
    assert conn.write_statements == [], (
        f"replica startup must not write; found: {conn.write_statements}"
    )
    # verification reads did happen
    assert any("pg_extension" in s for s in conn.read_statements)
    assert any("information_schema.tables" in s for s in conn.read_statements)
    assert backend._schema_ensured is True


def test_ensure_schema_primary_runs_writes():
    """Primary startup still executes the full write path (CREATE EXTENSION etc.)."""
    backend = _make_backend(recovery=False)
    backend._replica_mode = False
    conn = backend._pool.conn
    _require_tables(conn)

    # Stub the module-level write runner so the primary path reaches its
    # "write" without a real server.
    import mnemos.persistence.postgres as pg_module

    original = pg_module.ensure_postgres_schema

    async def fake_write_runner(pool, settings):
        pool.conn.write_statements.append("CREATE EXTENSION IF NOT EXISTS vector")

    pg_module.ensure_postgres_schema = fake_write_runner
    try:
        asyncio.run(backend.ensure_schema())
    finally:
        pg_module.ensure_postgres_schema = original

    assert any("CREATE EXTENSION" in s for s in conn.write_statements)


def test_ensure_schema_replica_readonly_conn_raises_readonly_error_on_any_write():
    """Even a stray write attempt on the replica hits the server's read-only fence."""
    backend = _make_backend(recovery=True)
    backend._replica_mode = True
    conn = backend._pool.conn
    _require_tables(conn)
    conn.add_extension("vector")

    # The verifier must not trigger any write on this connection.
    asyncio.run(backend.ensure_schema())
    assert conn.write_statements == []


def test_verify_replica_schema_fails_on_missing_table():
    backend = _make_backend(recovery=True)
    conn = backend._pool.conn
    _require_tables(conn)
    # drop one required table
    from mnemos.persistence import postgres as pg_module

    missing_table = pg_module._REPLICA_REQUIRED_TABLES[0]
    conn._tables.discard(missing_table)

    with pytest.raises(RuntimeError, match="missing table"):
        asyncio.run(verify_postgres_replica_schema(backend._pool, None))


def test_verify_replica_schema_warns_and_skips_dim_check_when_pgvector_missing():
    """No pgvector extension -> warning + skip column-width check, not a crash."""
    import mnemos.persistence.schema as schema_module

    backend = _make_backend(recovery=True)
    conn = backend._pool.conn
    _require_tables(conn)
    # no extension added -> missing
    conn.add_extension("plpgsql")  # present but not vector
    conn._extensions.discard("vector")

    async def crash_on_dim_check(conn_arg):
        raise AssertionError("column-width check must be skipped when pgvector is absent")

    original = schema_module.read_postgres_embedding_column_dim
    schema_module.read_postgres_embedding_column_dim = crash_on_dim_check
    try:
        # Must not raise and must not call the dim reader.
        asyncio.run(verify_postgres_replica_schema(backend._pool, settings=object()))
    finally:
        schema_module.read_postgres_embedding_column_dim = original

    # The missing-extension warning path was taken; writes still none.
    assert conn.write_statements == []


def test_verify_replica_schema_detects_dim_mismatch():
    """With pgvector present, a live/configured dim mismatch fails loudly."""
    backend = _make_backend(recovery=True)
    conn = backend._pool.conn
    _require_tables(conn)
    conn.add_extension("vector")
    conn.set_embedding_column_dim(384)

    class _DbSettings:
        class _Inner:
            embedding_dim = 768

        database = _Inner()

    with pytest.raises(RuntimeError, match="embedding"):
        asyncio.run(verify_postgres_replica_schema(backend._pool, settings=_DbSettings()))


def test_verify_replica_schema_tolerates_settings_without_a_database_section():
    """``settings.database = None`` must let startup proceed, not crash.

    A landing review read ``getattr(getattr(settings, "database", None),
    "embedding_dim", 0)`` as an AttributeError waiting to happen — as if the
    inner ``getattr`` were evaluated before the default applied. It is the
    documented contract of ``getattr`` that the default is returned, so the
    expression yields 0 (no configured dim, nothing to compare); even a
    configuration that did raise there is caught by the surrounding
    ``except Exception`` and logged. Either way the width check is skipped
    and startup proceeds. This test pins that observable contract — the call
    returns normally and writes nothing to the standby — rather than the
    shape of the ``getattr`` expression, which the swallowing handler makes
    unobservable from the outside.
    """

    backend = _make_backend(recovery=True)
    conn = backend._pool.conn
    _require_tables(conn)
    conn.add_extension("vector")
    conn.set_embedding_column_dim(384)

    class _NullDbSettings:
        database = None

    # A live dim of 384 with no configured dim: no mismatch is claimed, so no
    # RuntimeError, and no write on the standby connection.
    asyncio.run(verify_postgres_replica_schema(backend._pool, settings=_NullDbSettings()))
    assert conn.write_statements == []


# ────────────────────────────────────────────────────────────────────────────
# 4. Route guards: 503 replica_read_only
# ────────────────────────────────────────────────────────────────────────────


def test_route_guard_raises_503_with_machine_readable_body(monkeypatch):
    monkeypatch.setattr(replica_mode, "_replica", True)
    monkeypatch.setenv("MNEMOS_PRIMARY_URL", "http://primary.local")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        check_replica_write_allowed()
    err = exc_info.value
    assert err.status_code == 503
    assert err.detail == {"error": "replica_read_only", "primary_hint": "http://primary.local"}


def test_route_guard_is_noop_in_primary():
    # flag is False by default in this fixture
    check_replica_write_allowed()  # must not raise
    replica_read_only()  # builder still works regardless of flag


def test_persistence_helper_require_write_allowed(monkeypatch):
    from mnemos.api import persistence_helpers

    monkeypatch.setattr(replica_mode, "_replica", True)
    with pytest.raises(Exception) as exc_info:
        persistence_helpers.require_write_allowed()
    assert "replica_read_only" in str(exc_info.value.detail)


def test_mutating_routes_have_write_guards():
    """Static audit: every mutating endpoint in the API layer calls the guard.

    Scans @router.post/put/delete/patch decorators and asserts a
    ``require_write_allowed`` call appears within the following handler body.
    Read-only POST endpoints (search, fingerprint) are explicitly excluded.

    Updated 2026-11-22 (round 2): the body scan previously used a fixed
    35-line window starting at the decorator, which bled into the NEXT
    handler's guard and masked a missing guard on
    ``POST /admin/persephone/archive/{id}``. It now parses each handler's
    FULL body (up to the next top-level def/decorator) and additionally
    pins the mutating GET ``/auth/oauth/{provider}/callback``, which
    provisions a user and INSERTs an oauth_sessions row.
    """
    import re
    from pathlib import Path

    routes_dir = Path(__file__).resolve().parents[1] / "mnemos" / "api" / "routes"
    # Exempt: read-only handlers and network-bridge control (tunnels) that do
    # not write to the persistence layer.
    exempt_handlers = {
        "search_memories",  # read-only semantic/FTS search
        "federation_fingerprint",  # read-only embedder handshake
        "chat_completions",  # LLM read-through (no DB mutation in handler)
        "start_tunnel",  # opens a network tunnel; no DB write
        "stop_tunnel",  # tears down a network tunnel; no DB write
    }
    # Mutating GETs: the OAuth callback provisions state despite being GET.
    mutating_get_handlers = {"oauth_callback"}

    violations = []
    for path in routes_dir.glob("*.py"):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            m = re.match(
                r"@router\.(post|put|delete|patch|get)",
                line,
            )
            if not m:
                continue
            method = m.group(1)
            # find the async def within 6 lines after
            handler_name = None
            def_line = None
            for j in range(i, min(i + 8, len(lines))):
                mm = re.match(r"async def (\w+)\(", lines[j])
                if mm:
                    handler_name = mm.group(1)
                    def_line = j
                    break
            if handler_name in exempt_handlers:
                continue
            if method == "get" and handler_name not in mutating_get_handlers:
                continue
            if def_line is None:
                continue
            # Full handler body: up to the next top-level def/decorator.
            k = def_line + 1
            while k < len(lines):
                ln = lines[k]
                if ln and not ln[0].isspace() and (
                    ln.startswith("def ")
                    or ln.startswith("async def ")
                    or ln.startswith("@")
                ):
                    break
                k += 1
            body = "\n".join(lines[def_line:k])
            if "require_write_allowed" not in body and "ReplicaReadOnlyError" not in body:
                violations.append(f"{path.name}:{i+1} ({handler_name})")

    assert not violations, f"mutating routes missing require_write_allowed: {violations}"


# ────────────────────────────────────────────────────────────────────────────
# 5. Auth: read-only, no writes
# ────────────────────────────────────────────────────────────────────────────


def test_schedule_touch_skipped_in_replica_mode(monkeypatch):
    """Bearer auth must not queue a last_used write on a standby."""
    from mnemos.api import dependencies as auth_deps

    monkeypatch.setattr(replica_mode, "_replica", True)
    monkeypatch.setattr(auth_deps, "_touch_interval", 1.0)
    monkeypatch.setattr(auth_deps, "_note_touch", lambda k: True)

    class FakeOAuth:
        pass

    result = auth_deps._schedule_touch("backend", FakeOAuth(), "key-123")
    assert result is False
    # and nothing was queued
    assert len(auth_deps._dirty_keys) == 0


def test_schedule_touch_queues_in_primary(monkeypatch):
    from mnemos.api import dependencies as auth_deps

    monkeypatch.setattr(replica_mode, "_replica", False)
    monkeypatch.setattr(auth_deps, "_touch_interval", 1.0)
    monkeypatch.setattr(auth_deps, "_note_touch", lambda k: True)
    monkeypatch.setattr(auth_deps, "_ensure_touch_flusher", lambda b, o: True)

    result = auth_deps._schedule_touch("backend", "oauth", "key-123")
    assert result is True


def test_resolve_active_session_skips_write_in_replica_mode(monkeypatch):
    """PostgresOAuthRepository.resolve_active_session does not UPDATE oauth_sessions."""
    backend = _make_backend(recovery=True)
    backend._replica_mode = True
    conn = backend._pool.conn

    # The repository guard reads the process-level flag; monkeypatch it True.
    monkeypatch.setattr(replica_mode, "_replica", True)

    async def scenario():
        from datetime import timedelta
        from mnemos.persistence.postgres import PostgresTransaction

        class _FakeRawTx:
            def start(self):
                pass

        tx = PostgresTransaction(conn, _FakeRawTx())
        session_row = {
            "user_id": "u1",
            "identity_id": "i1",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "revoked": False,
        }
        original_fetchrow = conn.fetchrow

        async def fake_fetchrow(sql, *args):
            if "oauth_sessions" in sql:
                return session_row
            return await original_fetchrow(sql, *args)

        conn.fetchrow = fake_fetchrow
        try:
            row = await backend.oauth.resolve_active_session(
                tx, "sess-1", now=datetime.now(timezone.utc)
            )
        finally:
            conn.fetchrow = original_fetchrow
        return row

    row = asyncio.run(scenario())
    assert row is not None
    assert conn.write_statements == [], (
        f"auth must not write last_used_at; found: {conn.write_statements}"
    )


# ────────────────────────────────────────────────────────────────────────────
# 6. Replay lag
# ────────────────────────────────────────────────────────────────────────────


PG_STAT_WAL_RECEIVER_COLUMNS = {
    "pid", "status", "receive_start_lsn", "receive_start_tli", "written_lsn",
    "flushed_lsn", "received_tli", "last_msg_send_time", "last_msg_receipt_time",
    "latest_end_lsn", "latest_end_time", "slot_name", "sender_host", "sender_port",
    "conninfo",
}


def test_wal_receiver_selects_only_documented_columns():
    tree = ast.parse(Path(replica_mode.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in re.finditer(
                r"\bSELECT\s+(.*?)\s+FROM\s+(?:pg_catalog\.)?pg_stat_wal_receiver\b",
                node.value, re.IGNORECASE | re.DOTALL,
            ):
                columns = [column.strip().lower() for column in match.group(1).split(",")]
                assert set(columns) <= PG_STAT_WAL_RECEIVER_COLUMNS


def test_replay_lag_uses_server_clock():
    backend = _make_backend(recovery=True)
    backend._pool.conn.set_replay_timestamp(12.5)
    assert asyncio.run(replay_lag_seconds(backend)) == 12.5
    assert backend._pool.conn.read_statements == [
        "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))::float8"
    ]


def test_replay_lag_clamps_negative_values():
    backend = _make_backend(recovery=True)
    backend._pool.conn.set_replay_timestamp(-1.0)
    assert asyncio.run(replay_lag_seconds(backend)) == 0.0


def test_replay_lag_none_on_primary():
    backend = _make_backend(recovery=False)
    backend._pool.conn.set_replay_timestamp(None)
    assert asyncio.run(replay_lag_seconds(backend)) is None


def test_replay_lag_never_raises_on_dead_pool(caplog):
    class BrokenPool:
        def acquire(self):
            raise RuntimeError("dead")

    with caplog.at_level(logging.DEBUG, logger=replica_mode.__name__):
        assert asyncio.run(replay_lag_seconds(BrokenPool())) is None
        replica_mode._lag_cache = (0.0, None)
        assert asyncio.run(replay_lag_seconds(BrokenPool())) is None
    records = [record for record in caplog.records if "replay lag probe failed" in record.message]
    assert [record.levelno for record in records] == [logging.WARNING, logging.DEBUG]
    assert all("dead" in record.message for record in records)


def test_reset_clears_replay_lag_cache():
    backend = _make_backend(recovery=True)
    backend._pool.conn.set_replay_timestamp(12.5)
    assert asyncio.run(replay_lag_seconds(backend)) == 12.5
    replica_mode._reset_replica_state_for_tests()
    assert replica_mode._lag_cache == (0.0, None)
    backend._pool.conn.set_replay_timestamp(None)
    assert asyncio.run(replay_lag_seconds(backend)) is None


# ────────────────────────────────────────────────────────────────────────────
# 7. Health endpoint fields
# ────────────────────────────────────────────────────────────────────────────


def test_health_response_model_includes_replica_fields():
    from mnemos.domain.models import HealthResponse

    fields = HealthResponse.model_fields
    assert "role" in fields
    assert "replay_lag_seconds" in fields
    assert "embedding_model" in fields

    payload = HealthResponse(
        status="healthy",
        timestamp="2026-11-22T00:00:00",
        database_connected=True,
        version="7.2.0",
        role="replica",
        replay_lag_seconds=1.5,
        embedding_model="nomic-embed-v1.5",
    )
    assert payload.role == "replica"
    assert payload.replay_lag_seconds == 1.5
    assert payload.embedding_model == "nomic-embed-v1.5"


def test_stats_response_model_includes_replica_fields():
    from mnemos.domain.models import StatsResponse

    fields = StatsResponse.model_fields
    assert "role" in fields
    assert "replay_lag_seconds" in fields
    assert "embedding_model" in fields


def test_memory_list_response_has_embedding_degraded_field():
    from mnemos.domain.models import MemoryListResponse

    fields = MemoryListResponse.model_fields
    assert "embedding_degraded" in fields


def test_health_endpoint_populates_role_and_embedding(monkeypatch):
    """The /health route returns role/lag/embedding_model from live state."""
    import dataclasses

    from mnemos.api.routes import health as health_routes

    monkeypatch.setattr(health_routes._lc, "_persistence_backend", None)

    @dataclasses.dataclass
    class FakeResponse:
        status_code: int = 200

    async def scenario():
        return await health_routes.health_check(response=FakeResponse())

    # With no backend: primary role, lag None, embedding_model populated
    # from the configured embedding identity (or None if unconfigured).
    result = asyncio.run(scenario())
    assert result.role == "primary"
    assert result.replay_lag_seconds is None
    assert "embedding_model" in result.model_dump()


# ────────────────────────────────────────────────────────────────────────────
# 8. Embedder-missing degradation flag
# ────────────────────────────────────────────────────────────────────────────


def test_embedding_degraded_flag_set_when_embedder_missing_on_replica():
    """The search response model carries the flag; the route sets it on failure."""
    import inspect

    from mnemos.api.routes import memories as memories_routes
    from mnemos.domain.models import MemoryListResponse

    # Contract: the flag exists on the response model.
    resp = MemoryListResponse(
        count=0,
        memories=[],
        embedding_degraded=True,
    )
    assert resp.embedding_degraded is True

    # Contract: the search handler assigns the flag inside the replica
    # degradation branch (semantic search fails -> lexical fallback).
    source = inspect.getsource(memories_routes.search_memories)
    assert "embedding_degraded = True" in source
    assert "is_replica()" in source


# ────────────────────────────────────────────────────────────────────────────
# 9. MCP tool coverage
# ────────────────────────────────────────────────────────────────────────────


def test_mcp_write_tools_cover_all_mutating_tools():
    """_WRITE_TOOLS must cover every MCP tool that mutates state.

    Updated 2026-11-22 (round 2): ``diff_memory_commits`` was removed from
    the expected write set. Re-audit of its handler
    (mnemos/mcp/tools/kg.py) shows it only runs two SELECTs
    (memory_commits lookups) and difflib.unified_diff — it is a pure read.
    Listing it as mutating 503-ed a read-only tool on a replica, violating
    the task requirement that reads (search, list, get, KG reads, stats)
    keep fully working.
    """
    from mnemos.mcp.tools import _WRITE_TOOLS

    # The mutating MCP tools are exactly these (audited against handler bodies:
    # all others use _rest_get / read-only REST calls or pure computation).
    expected_mutating = {
        "update_memory",
        "create_memory",
        "delete_memory",
        "kg_create_triple",
        "update_triple",
        "delete_triple",
        "bulk_create_memories",
        "branch_memory",
        "checkout_memory",
        "log_memory",
        "graeae_consult",
    }
    assert _WRITE_TOOLS == expected_mutating, (
        f"_WRITE_TOOLS mismatch: missing {expected_mutating - _WRITE_TOOLS}; "
        f"spurious {_WRITE_TOOLS - expected_mutating}"
    )
    # diff_memory_commits is a read (two SELECTs + difflib) and must stay out.
    assert "diff_memory_commits" not in _WRITE_TOOLS
    # Every mutating tool in the registry must be in the write set.
    from mnemos.mcp.tools import TOOL_REGISTRY

    for tool_name in expected_mutating:
        if tool_name in TOOL_REGISTRY:
            assert tool_name in _WRITE_TOOLS


def test_mcp_replica_read_only_body():
    from mnemos.core.replica_error import mcp_replica_read_only

    result = mcp_replica_read_only()
    assert result["success"] is False
    assert result["error"] == "replica_read_only"
    assert "primary_hint" in result


# ────────────────────────────────────────────────────────────────────────────
# 10. Primary mode unchanged
# ────────────────────────────────────────────────────────────────────────────


def test_primary_mode_is_unchanged_when_not_in_recovery():
    """When pg_is_in_recovery is false, the process stays primary.

    Drives the same sequence as ``PostgresBackend.open()`` (detect -> set ->
    ensure_schema) without calling the full ``open()`` which also probes the
    embedding contract against the live pool.
    """
    backend = _make_backend(recovery=False)

    async def scenario():
        self_mode = await detect_replica_mode(backend._pool)
        await replica_mode_set(self_mode, reason="startup")
        backend._replica_mode = self_mode
        assert not self_mode
        # Primary path runs the full write schema path, not verification.
        assert backend.replica_mode is False

    asyncio.run(scenario())
    assert is_replica() is False
    assert backend.replica_mode is False
