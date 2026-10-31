"""M8 — startup embedding-dimension agreement on every backend.

The check is "configured dim == embedder output dim == existing column dim".
Three of those four words were already enforced (the embedder's own output
width is validated on write); the column side was not, so a deployment could
change ``MNEMOS_EMBEDDING_DIM`` and keep running against a column whose
declared width never moved. MySQL/MariaDB also sized their DDL from the
*environment only* at import time, which made a config.toml-only
``database.embedding_dim`` invisible to the schema they created.

These tests drive each backend's ``open()`` (or the schema helper it calls)
against a fake pool that records the SQL, so they run without a live server.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# The helpers below (the shared mismatch wording and the Oracle/Db2 catalog
# checks) do not exist on the base commit; skip the whole module there instead
# of failing collection, so the file can be run against either tree to show
# which tests are regressions.
schema = pytest.importorskip("mnemos.persistence.schema")
for _required in ("embedding_dimension_mismatch", "check_oracle_embedding_dim", "check_db2_embedding_dim"):
    if not hasattr(schema, _required):
        pytest.skip(f"M8 helper {_required} is not present on this tree", allow_module_level=True)

from mnemos.persistence import mariadb, mysql  # noqa: E402
from mnemos.persistence.mariadb import MariadbBackend
from mnemos.persistence.mysql import MysqlBackend


pytestmark = pytest.mark.asyncio


# ─── fake aiomysql pool ──────────────────────────────────────────────────────


class _CursorContext:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self._cursor

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeCursor:
    """Records every statement and replays canned response queues.

    ``column_type`` is what ``information_schema.COLUMNS`` reports for the
    vector column; ``has_vectors`` drives the "is the column populated?" probe.
    """

    def __init__(self, *, column_type: str | None = None, has_vectors: bool = False):
        self.column_type = column_type
        self.has_vectors = has_vectors
        self.executed: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))

    async def fetchone(self):
        last = self.executed[-1][0] if self.executed else ""
        if "information_schema.columns" in last.lower():
            return (self.column_type,) if self.column_type is not None else None
        if last.lower().startswith("select 1 from "):
            return (1,) if self.has_vectors else None
        return (1,)

    async def fetchall(self):
        return []

    async def close(self):
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return _CursorContext(self._cursor)

    async def commit(self):
        return None


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _CursorContext(self._conn)


def _settings(dim: int):
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=dim))


def _ddl_vector_width(statements: list[tuple[str, object]], table: str) -> str | None:
    """Return the VECTOR(n) declaration the DDL used for ``table.embedding``."""
    for sql, _params in statements:
        if f"create table if not exists {table}" not in sql.lower():
            continue
        # Statements were whitespace-collapsed by the fake cursor, so match on
        # the declarator rather than on physical layout.
        match = re.search(r"\bembedding\s+VECTOR\((\d+)\)", sql, re.IGNORECASE)
        if match:
            return f"VECTOR({match.group(1)})"
    return None


# ─── MySQL: DDL is sized from the settings, not the environment at import ────


async def test_mysql_ddl_uses_settings_dim_not_the_import_time_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config.toml-only dim must reach CREATE TABLE.

    The env is set to a *different* value than the settings object; the DDL has
    to follow the settings (that is the live configuration), proving the width
    is no longer baked in at import time.
    """
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "768")
    cursor = _FakeCursor(column_type=None)
    backend = MysqlBackend(_FakePool(_FakeConn(cursor)), _settings(1024))

    await backend.open()

    assert _ddl_vector_width(cursor.executed, "memories") == "VECTOR(1024)"


@pytest.mark.parametrize(
    ("column_type", "has_vectors", "expect_error"),
    [
        # Fresh install: DDL just created the column at the configured width.
        ("VECTOR(1024)", False, False),
        # Legacy column, empty: retype in place (same posture as Postgres).
        ("VECTOR(768)", False, False),
        # Legacy column holding vectors: refuse to start.
        ("VECTOR(768)", True, True),
    ],
)
async def test_mysql_startup_check_on_populated_column(
    column_type: str,
    has_vectors: bool,
    expect_error: bool,
) -> None:
    cursor = _FakeCursor(column_type=column_type, has_vectors=has_vectors)
    backend = MysqlBackend(_FakePool(_FakeConn(cursor)), _settings(1024))

    if expect_error:
        with pytest.raises(RuntimeError, match="MNEMOS_EMBEDDING_DIM=1024"):
            await backend.open()
        assert not any("modify column" in sql.lower() for sql, _ in cursor.executed)
        return

    await backend.open()
    retyped = any("modify column" in sql.lower() for sql, _ in cursor.executed)
    assert retyped is (column_type != "VECTOR(1024)")


async def test_mysql_startup_check_tolerates_absent_catalog_row() -> None:
    """An unsupported server / missing catalog row must not fail startup."""
    cursor = _FakeCursor(column_type=None)
    backend = MysqlBackend(_FakePool(_FakeConn(cursor)), _settings(1024))
    await backend.open()  # no raise
    assert not any("modify column" in sql.lower() for sql, _ in cursor.executed)


# ─── MariaDB: same check, aimed at the join table ────────────────────────────


async def test_mariadb_ddl_and_check_target_the_join_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "768")
    cursor = _FakeCursor(column_type="vector(1024)")
    backend = MariadbBackend(_FakePool(_FakeConn(cursor)), _settings(1024))

    await backend.open()

    assert _ddl_vector_width(cursor.executed, "memory_embeddings") == "VECTOR(1024)"
    # The catalog probe must ask about memory_embeddings, not memories: MariaDB
    # keeps no embedding column on memories at all.
    # ``open`` also probes information_schema for the idempotent column
    # backfill, so select the dimension probe by its shape: it reads
    # COLUMN_TYPE (the backfill reads COLUMN_NAME) for one table+column pair.
    probes = [
        params
        for sql, params in cursor.executed
        if "information_schema.columns" in sql.lower() and "column_type" in sql.lower()
    ]
    assert probes == [("memory_embeddings", "embedding")]


async def test_mariadb_populated_join_table_refuses_to_start() -> None:
    cursor = _FakeCursor(column_type="vector(768)", has_vectors=True)
    backend = MariadbBackend(_FakePool(_FakeConn(cursor)), _settings(1024))
    with pytest.raises(RuntimeError, match="memory_embeddings.embedding"):
        await backend.open()


async def test_mariadb_module_imports_the_schema_helpers_it_uses() -> None:
    """Regression: mariadb.py called schema helpers it never imported.

    ``MariadbBackend.open`` resolved its DDL width through the schema reader
    while the module imported only ``types``/``visibility`` from its siblings,
    so importing mariadb raised NameError before any backend could start.
    """
    assert mariadb.resolve_embedding_dim is schema.resolve_embedding_dim
    assert mariadb.warn_embedding_dimension_mismatch is schema.warn_embedding_dimension_mismatch


async def test_mysql_module_imports_the_mismatch_warner() -> None:
    """Regression: the mysql startup check called an unimported symbol."""
    assert mysql.warn_embedding_dimension_mismatch is schema.warn_embedding_dimension_mismatch


# ─── Oracle / Db2: catalog width vs configured width ─────────────────────────


class _CatalogCursor:
    def __init__(self, row):
        self._row = row
        self.executed: list[str] = []

    async def execute(self, sql, params=None):
        self.executed.append(" ".join(str(sql).split()))

    async def fetchone(self):
        return self._row

    async def close(self):
        return None


class _CatalogConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.commits += 1


@pytest.mark.parametrize(
    ("checker", "reported_type"),
    [
        (schema.check_oracle_embedding_dim, "VECTOR(1024, FLOAT32)"),
        (schema.check_db2_embedding_dim, "VECTOR(1024, FLOAT32)"),
    ],
)
async def test_oracle_and_db2_accept_a_matching_column(checker, reported_type: str) -> None:
    conn = _CatalogConn(_CatalogCursor((reported_type,)))
    await checker(conn, 1024)  # no raise


@pytest.mark.parametrize("checker", [schema.check_oracle_embedding_dim, schema.check_db2_embedding_dim])
async def test_oracle_and_db2_reject_a_stale_column_width(checker) -> None:
    conn = _CatalogConn(_CatalogCursor(("VECTOR(768, FLOAT32)",)))
    with pytest.raises(RuntimeError, match="MNEMOS_EMBEDDING_DIM=1024"):
        await checker(conn, 1024)


@pytest.mark.parametrize("checker", [schema.check_oracle_embedding_dim, schema.check_db2_embedding_dim])
async def test_oracle_and_db2_skip_when_the_table_is_absent(checker) -> None:
    """A fresh schema (migration just created it) or a thin catalog → no row."""
    conn = _CatalogConn(_CatalogCursor(None))
    await checker(conn, 1024)  # no raise


@pytest.mark.parametrize("checker", [schema.check_oracle_embedding_dim, schema.check_db2_embedding_dim])
async def test_oracle_and_db2_warn_and_continue_when_the_probe_fails(checker, caplog) -> None:
    """A catalog privilege error is not evidence of a mismatch."""
    cursor = _CatalogCursor(None)
    cursor.execute = AsyncMock(side_effect=PermissionError("ORA-00942"))
    conn = _CatalogConn(cursor)
    with caplog.at_level("WARNING"):
        await checker(conn, 1024)  # no raise
    assert any("catalog probe failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("checker", [schema.check_oracle_embedding_dim, schema.check_db2_embedding_dim])
async def test_oracle_and_db2_accept_a_mapping_row(checker) -> None:
    """oracledb can hand back a dict-like row; the width must still be read."""
    conn = _CatalogConn(_CatalogCursor({"DATA_TYPE": "VECTOR(768, FLOAT32)"}))
    with pytest.raises(RuntimeError, match="768"):
        await checker(conn, 1024)


# ─── the shared mismatch wording ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("configured", "column", "warns"),
    [
        (1024, "VECTOR(1024)", False),
        (1024, "vector(1024)", False),
        (1024, "VECTOR(768)", True),
        (1024, "float[768]", True),
        (1024, 768, True),
        (1024, 1024, False),
        (1024, None, False),
        (1024, "unparsable", False),
        (None, "VECTOR(768)", True),
    ],
)
@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_embedding_dimension_mismatch_table(configured, column, warns: bool) -> None:
    """Table-driven: one message shape for every backend's column reading."""
    message = schema.embedding_dimension_mismatch(configured, column)
    assert (message is not None) is warns


# ─── MariaDB/MySQL DDL placeholder (robustness of the render step) ───────────
#
# Regression: ``_init_ddls`` injects the configured width through the single
# literal placeholder ``{vector_column}``. A typo'd or duplicated placeholder
# silently produced a malformed CREATE TABLE. Assert the contract directly:
# exactly one DDL entry carries the placeholder, and no rendered statement
# keeps a ``{...}`` token.


def test_mariadb_ddl_placeholder_renders():
    holders = [ddl for ddl in mariadb._INIT_DDLS if "{vector_column}" in ddl]
    assert len(holders) == 1, f"expected exactly one DDL carrying {{vector_column}}, got {len(holders)}"
    assert holders[0] is mariadb._DDL_MEMORY_EMBEDDINGS

    rendered = mariadb._init_ddls(1024)
    assert len(rendered) == len(mariadb._INIT_DDLS)
    for stmt in rendered:
        assert "{vector_column}" not in stmt
        assert "{" not in stmt, f"unrendered template token left in DDL: {stmt[:200]!r}"
    embeddings = next(s for s in rendered if "memory_embeddings" in s)
    assert "VECTOR(1024) NOT NULL" in embeddings
    assert "VECTOR INDEX (embedding)" in embeddings


def test_mariadb_ddl_placeholder_renders_mysql_too():
    holders = [ddl for ddl in mysql._INIT_DDLS if "{vector_column}" in ddl]
    assert len(holders) == 1, f"expected exactly one DDL carrying {{vector_column}}, got {len(holders)}"
    for stmt in mysql._init_ddls(768):
        assert "{" not in stmt, f"unrendered template token left in DDL: {stmt[:200]!r}"
