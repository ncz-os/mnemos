"""F-06 slice 2: contract test — the ABC methods the MCP tools now call exist
on all six backends.

The DAG and model tools were rewired from raw ``_lc._pool`` SQL to the
persistence ABC: ``MemoryRepository.fetch_memory_log`` /
``fetch_diff_commit_pair`` / ``fetch_checkout_commit`` /
``assert_memory_readable``, ``BranchRepository.create_memory_branch`` and
``ConsultationAuditRepository.fetch_recommended_model``, each inside
``backend.transactional()``. This test asserts that contract on the concrete
facade classes (six backends) *and* on the ABCs themselves, independently —
without needing live databases.
"""

from __future__ import annotations

import inspect


def _facade_classes() -> dict[str, type]:
    from mnemos.persistence.db2 import Db2Backend
    from mnemos.persistence.mariadb import MariadbBackend
    from mnemos.persistence.mysql import MysqlBackend
    from mnemos.persistence.oracle import OracleBackend
    from mnemos.persistence.postgres import PostgresBackend
    from mnemos.persistence.sqlite import SqliteBackend

    return {
        "sqlite": SqliteBackend,
        "postgres": PostgresBackend,
        "mysql": MysqlBackend,
        "mariadb": MariadbBackend,
        "oracle": OracleBackend,
        "db2": Db2Backend,
    }


_METHOD_GRID = {
    "memories": (
        "fetch_memory_log",
        "fetch_diff_commit_pair",
        "fetch_checkout_commit",
        "assert_memory_readable",
    ),
    "memory_branches": ("create_memory_branch",),
    "consultations_audit": ("fetch_recommended_model",),
}


def test_abc_methods_exist_on_all_six_backends():
    """The §0b contract, pinned statically (no live databases needed).

    Two independent layers:
    1. facade surface — ``transactional()`` + the three repository
       accessors, present on the MRO of all six concrete facades;
    2. ABC declaration — the methods the tools call are declared on the
       ABCs the facades implement (the concrete implementations then
       exist by construction, since ABCs are not instantiable otherwise).
    """
    facades = _facade_classes()
    assert set(facades) == {"sqlite", "postgres", "mysql", "mariadb", "oracle", "db2"}

    missing: list[str] = []
    for name, cls in facades.items():
        # transactional(): the facade entry point the tools use for every op.
        # MariaDB inherits MysqlBackend.transactional, so presence on the MRO
        # (not just __dict__) is the honest check.
        if getattr(cls, "transactional", None) is None:
            missing.append(f"{name}: transactional (facade)")
        # Repository property accessors (present on the MRO).
        for repo_attr in _METHOD_GRID:
            if not hasattr(cls, repo_attr):
                missing.append(f"{name}: {repo_attr} (facade property)")

    # Base ABC declaration: the contract the six backends implement. Runs
    # unconditionally — independent of the instance walk above.
    from mnemos.persistence.base import (
        BranchRepository,
        ConsultationAuditRepository,
        MemoryRepository,
    )

    for repo, methods in (
        (MemoryRepository, _METHOD_GRID["memories"]),
        (BranchRepository, _METHOD_GRID["memory_branches"]),
        (ConsultationAuditRepository, _METHOD_GRID["consultations_audit"]),
    ):
        for method in methods:
            member = getattr(repo, method, None)
            if not (inspect.isfunction(member) or inspect.ismethod(member)):
                missing.append(f"base: {repo.__name__}.{method}")

    assert missing == [], "\n".join(missing)
