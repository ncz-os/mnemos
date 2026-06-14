from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(rel: str) -> str:
    return (ROOT / rel).read_text()


def _feed_query_body(source: str) -> str:
    start = source.index("    async def feed_query(")
    end = source.index("    async def get_feed_memory", start)
    return source[start:end]


def _get_feed_memory_body(source: str) -> str:
    start = source.index("    async def get_feed_memory")
    match = re.search(r"\n    async def \w+", source[start + 1 :])
    if match is None:
        return source[start:]
    return source[start : start + 1 + match.start()]


def test_federation_feed_static_parity_across_backends():
    # PostgreSQL, Oracle, Db2, MySQL, SQLite: every backend feed must expose
    # the same security/variant/cursor shape rather than backend-specific
    # stubs or ignored flags.
    backends = {
        "postgres": "mnemos/persistence/postgres.py",
        "oracle": "mnemos/persistence/oracle.py",
        "db2": "mnemos/persistence/db2.py",
        "mysql": "mnemos/persistence/mysql.py",
        "sqlite": "mnemos/persistence/sqlite.py",
    }
    for name, rel in backends.items():
        body = _feed_query_body(_source(rel)).lower()
        assert "_ = prefer_compressed" not in body, name
        assert "does not support prefer_compressed" not in body, name
        assert "left join memory_compressed_variants" in body, name
        assert "case when" in body, name
        assert "compressed_content" in body, name
        assert "consolidation" in body, name
        assert "vault_namespace" in body, name
        assert "federation_source is null" in body, name
        assert "consolidated_into is null" in body or "eligible_for_federation" in body, name
        assert "consolidated_into is not null" in body, name


def test_federation_get_memory_static_parity_across_backends():
    # Per-memory fetches carry the same federation eligibility and vault
    # exclusion as paged feeds on all five backends.
    backends = {
        "postgres": "mnemos/persistence/postgres.py",
        "oracle": "mnemos/persistence/oracle.py",
        "db2": "mnemos/persistence/db2.py",
        "mysql": "mnemos/persistence/mysql.py",
        "sqlite": "mnemos/persistence/sqlite.py",
    }
    for name, rel in backends.items():
        source = _source(rel)
        body = _get_feed_memory_body(source).lower()
        assert "vault_namespace" in body, name
        assert "federation_source is null" in body or "eligible_for_federation" in body, name
        assert "archived_at is null" in body or "eligible_for_federation" in body, name
        assert "consolidated_into is null" in body or "eligible_for_federation" in body, name
        assert "permission_mode" in body, name


def test_persistence_sources_do_not_leave_federation_compressed_stubs():
    # Static guard for the exact parity regression fixed here: no backend may
    # silently ignore prefer_compressed or raise the SQLite federation stub.
    for rel in (
        "mnemos/persistence/postgres.py",
        "mnemos/persistence/oracle.py",
        "mnemos/persistence/db2.py",
        "mnemos/persistence/mysql.py",
        "mnemos/persistence/sqlite.py",
    ):
        source = _source(rel)
        assert "_ = prefer_compressed" not in source
        assert "does not support prefer_compressed" not in source
        assert "sync paths stubbed" not in source
