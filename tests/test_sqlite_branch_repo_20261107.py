"""FB1003: ``SqliteBranchRepository`` contract + schema acceptance.

Contract assertions (bases, method set, parameter parity, dialect guard) plus a real SQLite
roundtrip against a temporary database: the backend applies its migrations, a memory row is
created, a branch row is inserted, and the repository under test deletes it.

DIVERGENCE (brief premise vs code): the class is not a thin ABC shell — it adds three
private helpers (`_fetch_existing_branch`, `_fetch_main_branch_start`,
`_fetch_branch_start_by_commit`) on top of the four documented methods, and
`fetch_memory_branch_heads` reads `memory_versions` rather than `memory_branches`, so a
branch row with no version rows never appears in its result.
"""

from __future__ import annotations

import inspect

import pytest
import pytest_asyncio

from mnemos.persistence.base import BranchRepository
from mnemos.persistence.sqlite import (
    SqliteBackend,
    SqliteBranchRepository,
    SqliteTransaction,
    _SqliteRepository,
    _execute,
    _fetch_all,
)

METHODS = (
    "create_memory_branch",
    "delete_memory_branches_for_memories",
    "fetch_memory_branch_heads",
    "upsert_memory_branch_head",
)


def _params(func: object) -> list[tuple[str, object, object]]:
    return [(param.name, param.kind, param.default) for param in inspect.signature(func).parameters.values()]


class _Settings:
    class database:
        embedding_dim = 1024


@pytest_asyncio.fixture
async def backend(tmp_path):
    be = SqliteBackend(tmp_path / "branches.db", _Settings())
    await be.open()
    yield be
    await be.close()


async def _add_memory(be: SqliteBackend, memory_id: str) -> None:
    async with be.transactional() as tx:
        await be.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content="branch fixture",
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=75,
            owner_id="alice",
            namespace="alice-ns",
            permission_mode=0,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content="branch fixture",
            created=None,
            updated=None,
        )


def test_subclasses_the_documented_mixin_and_base() -> None:
    assert SqliteBranchRepository.__bases__ == (_SqliteRepository, BranchRepository)
    mro = SqliteBranchRepository.__mro__
    assert _SqliteRepository in mro
    assert BranchRepository in mro


def test_repository_is_concrete() -> None:
    assert inspect.isabstract(SqliteBranchRepository) is False
    assert SqliteBranchRepository.__abstractmethods__ == frozenset()


def test_construction_is_argument_free_and_inert() -> None:
    repo = SqliteBranchRepository()

    assert type(repo) is SqliteBranchRepository
    assert vars(repo) == {}


def test_documented_methods_are_defined_concrete_and_async() -> None:
    for name in METHODS:
        assert name in SqliteBranchRepository.__dict__, name
        method = SqliteBranchRepository.__dict__[name]
        assert inspect.iscoroutinefunction(method), name
        assert getattr(method, "__isabstractmethod__", False) is False, name


def test_parameter_contracts_match_the_base_declaration() -> None:
    for name in METHODS:
        assert _params(SqliteBranchRepository.__dict__[name]) == _params(getattr(BranchRepository, name)), name


def test_dialect_guard_rejects_a_foreign_transaction() -> None:
    with pytest.raises(TypeError, match="SqliteTransaction"):
        SqliteBranchRepository._conn(object())


def test_no_attribute_fallback_is_defined() -> None:
    assert "__getattr__" not in SqliteBranchRepository.__dict__
    repo = SqliteBranchRepository()
    with pytest.raises(AttributeError):
        repo.merge_branches


@pytest.mark.asyncio
async def test_sqlite_schema_accepts_the_documented_branch_table(backend) -> None:
    async with backend.transactional() as tx:
        tables = await _fetch_all(
            tx.conn,
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_branches'",
        )
        columns = await _fetch_all(tx.conn, "PRAGMA table_info(memory_branches)")

    assert tables == [{"name": "memory_branches"}]
    names = [column["name"] for column in columns]
    # The migrations file declares five columns; ``open()`` then appends
    # ``deleted_at`` through ``_ensure_repository_columns``, so the live schema has six.
    assert names == [
        "memory_id",
        "name",
        "head_version_id",
        "created_by",
        "created_at",
        "deleted_at",
    ]
    primary_key = {column["name"]: column["pk"] for column in columns if column["pk"]}
    assert primary_key == {"memory_id": 1, "name": 2}


@pytest.mark.asyncio
async def test_delete_removes_the_branch_row(backend) -> None:
    await _add_memory(backend, "mem-branch-1")
    await _add_memory(backend, "mem-branch-2")

    async with backend.transactional() as tx:
        for memory_id in ("mem-branch-1", "mem-branch-2"):
            await _execute(
                tx.conn,
                "INSERT INTO memory_branches (memory_id, name, head_version_id, created_by) VALUES (?, ?, NULL, ?)",
                (memory_id, "main", "tester"),
            )
        before = await _fetch_all(tx.conn, "SELECT memory_id, name FROM memory_branches")
        assert before == [{"memory_id": "mem-branch-1", "name": "main"}, {"memory_id": "mem-branch-2", "name": "main"}]

        await backend.memory_branches.delete_memory_branches_for_memories(tx, ["mem-branch-1"])
        after = await _fetch_all(tx.conn, "SELECT memory_id, name FROM memory_branches")
        # The memory rows themselves survive: the repository delete is scoped to the
        # requested memory_id, not a cascade triggered by removing the memory.
        memories = await _fetch_all(tx.conn, "SELECT id FROM memories ORDER BY id")

    # mem-branch-2's row is untouched, so the deletion tracks the requested memory_id
    # and cannot be explained by table-wide cleanup or an FK cascade.
    assert after == [{"memory_id": "mem-branch-2", "name": "main"}]
    assert memories == [{"id": "mem-branch-1"}, {"id": "mem-branch-2"}]


@pytest.mark.asyncio
async def test_empty_inputs_short_circuit(backend) -> None:
    async with backend.transactional() as tx:
        assert await backend.memory_branches.fetch_memory_branch_heads(tx, []) == []
        assert await backend.memory_branches.delete_memory_branches_for_memories(tx, []) is None


@pytest.mark.asyncio
async def test_repository_accepts_a_real_sqlite_transaction(backend) -> None:
    async with backend.transactional() as tx:
        assert isinstance(tx, SqliteTransaction)
        assert SqliteBranchRepository._conn(tx) is tx.conn
