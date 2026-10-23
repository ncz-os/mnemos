"""F-06 contract test: ``Transaction.raw_connection()`` on all six backends.

Per the §0b governing rule (ABC method -> implementation on all SIX
backends -> facade exposure -> caller uses the ABC), the escape-hatch
method ``NestableTransaction.raw_connection()`` — added so the last
``_lc._pool`` reference outside ``mnemos/persistence/`` can route
through the facade — must exist and work identically on every
shipped backend: postgres, sqlite, oracle, db2, mysql, mariadb.

Three layers of proof:

1. PROTOCOL: ``NestableTransaction`` declares ``raw_connection``.

2. STATIC (six arms): the transaction classes
   (``PostgresTransaction``, ``SqliteTransaction``,
   ``_OracleTransaction``, ``_MysqlTransaction``) define
   ``raw_connection`` in their OWN class body. Db2 inherits its
   transaction class from the oracle module and MariaDB from the
   mysql module, so those four classes cover all six backends.

3. LIVE (SQLite): a real ``SqliteBackend.transactional()`` handle
   returns the SAME object from both ``raw_connection()`` and the
   existing ``conn`` surface.

The facade side of the contract (``backend.pool`` — what
``require_postgres_pool_or_503`` resolves through, and how a backend
that cannot supply a raw pool reports ``None`` rather than a
fabricated default) is pinned by
``tests/test_persistence_helpers.py``.
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# (persistence module file, class name, backend label) — every shipped
# backend's transaction class, in its own file.
_BACKEND_TX_CLASSES = [
    ("postgres.py", "PostgresTransaction", "postgres"),
    ("sqlite.py", "SqliteTransaction", "sqlite"),
    ("oracle.py", "_OracleTransaction", "oracle"),
    ("mysql.py", "_MysqlTransaction", "mysql"),
]


def _class_defines_method(module_path: pathlib.Path, class_name: str, method: str) -> bool:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == method
                for item in node.body
            )
    raise AssertionError(f"class {class_name} not found in {module_path.name}")


@pytest.mark.parametrize(
    ("module_file", "class_name", "backend"),
    _BACKEND_TX_CLASSES,
    ids=[b[2] for b in _BACKEND_TX_CLASSES],
)
def test_transaction_class_defines_raw_connection(module_file: str, class_name: str, backend: str) -> None:
    """Each backend's transaction class defines raw_connection in its
    OWN class body (not via inheritance or monkey-patching).

    The db2 and mariadb arms: Db2Backend inherits its transaction
    class from the oracle module and MariadbBackend from the mysql
    module (verified statically below), so the four classes above
    ARE the six backends' transaction handles.
    """
    path = REPO_ROOT / "mnemos" / "persistence" / module_file
    assert _class_defines_method(path, class_name, "raw_connection"), (
        f"{class_name} in {path.name} must define raw_connection() "
        f"(F-06 six-backend contract, backend: {backend})"
    )


@pytest.mark.parametrize(
    ("module_file", "class_name", "parent_module_file", "parent_class_name", "backend"),
    [
        ("db2.py", "Db2Backend", "oracle.py", "OracleBackend", "db2"),
        ("mariadb.py", "MariadbBackend", "mysql.py", "MysqlBackend", "mariadb"),
    ],
    ids=["db2", "mariadb"],
)
def test_inheriting_backends_share_the_parent_transaction(
    module_file: str,
    class_name: str,
    parent_module_file: str,
    parent_class_name: str,
    backend: str,
) -> None:
    """Db2 / MariaDB inherit their parent facade, hence its
    transaction class — the static closure of the six-backend
    contract."""
    path = REPO_ROOT / "mnemos" / "persistence" / module_file
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            assert parent_class_name in bases, (
                f"{class_name} in {module_file} must inherit from "
                f"{parent_class_name} (F-06 contract, backend: {backend})"
            )
            return
    pytest.fail(f"{class_name} not found in {module_file}")


def test_protocol_declares_raw_connection() -> None:
    """The NestableTransaction protocol declares raw_connection, so the
    contract is discoverable on the ABC layer itself."""
    tree = ast.parse((REPO_ROOT / "mnemos/persistence/base.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "NestableTransaction":
            names = [
                item.name for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            assert "raw_connection" in names
            assert "savepoint" in names  # pre-existing member, sanity
            return
    pytest.fail("NestableTransaction not found in mnemos/persistence/base.py")


@pytest.mark.asyncio
async def test_sqlite_live_raw_connection_matches_conn_surface(tmp_path):
    """Live SQLite arm: raw_connection() returns the SAME driver
    handle the conn property exposes — the escape hatch hands back
    the real connection, never a proxy or a fabricated stand-in."""
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "raw-conn-contract.db", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            assert tx.raw_connection() is tx.conn
            # It is an actual aiosqlite connection, usable for the
            # escape-hatch purpose the guard exists for.
            import aiosqlite

            assert isinstance(tx.raw_connection(), aiosqlite.Connection)
    finally:
        await backend.close()
