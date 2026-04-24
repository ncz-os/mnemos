"""MPF /v1/export and /v1/import contract tests (v3.2).

Direct-handler tests with a mocked asyncpg connection. Verifies:

  * Export is scoped to the caller's owner_id + namespace for non-root.
  * Root can target any owner/namespace via query params.
  * Non-root passing cross-owner/ns query params -> 403 (explicit
    rejection, not silent narrowing).
  * Envelope shape matches MPF v0.1: mpf_version, records[] with
    kind='memory', payload_version='mnemos-3.1'.
  * Import stamps the caller's owner_id + namespace on every record
    by default (non-root can't smuggle other owners' rows in).
  * Root with preserve_owner=true honors envelope's owner/namespace.
  * Non-root with preserve_owner=true -> 403.
  * Unknown record kinds counted under unsupported_kinds and skipped.
  * Payload-version mismatch counted as skipped with an error.
  * Empty content counted as failed.
  * Envelope mpf_version mismatch -> 415.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from api.auth import UserContext
from api.handlers import portability


def _alice() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="alice-ns", authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return self._rows

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        # Default: INSERT successful. Tests can override the conn to
        # simulate ON CONFLICT DO NOTHING (INSERT 0 0) or failures.
        return "INSERT 0 1"

    def transaction(self):
        class _NullCtx:
            async def __aenter__(self_): return self_
            async def __aexit__(self_, *a): return False
        return _NullCtx()


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import api.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def _memory_row(
    id: str = "mem_alice1",
    owner_id: str = "alice",
    namespace: str = "alice-ns",
    category: str = "solutions",
    content: str = "hello",
):
    return {
        "id": id, "content": content, "category": category, "subcategory": None,
        "created": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "owner_id": owner_id, "namespace": namespace, "permission_mode": 600,
        "quality_rating": 75,
        "source_model": None, "source_provider": None,
        "source_session": None, "source_agent": None,
        "metadata": {"imported_from": "test"},
    }


# ─── /v1/export ──────────────────────────────────────────────────────────────


def test_export_filters_by_caller_owner_and_namespace_for_non_root(monkeypatch):
    conn = _Conn(rows=[_memory_row()])
    _install(monkeypatch, conn)

    asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, user=_alice(),
    ))

    sql, args = conn.fetch_calls[-1]
    assert "owner_id = $" in sql
    assert "namespace = $" in sql
    assert "alice" in args
    assert "alice-ns" in args


def test_export_non_root_cross_owner_param_rejected(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.export_memories(
            category=None, limit=1000, offset=0,
            owner_id="bob", namespace=None, user=_alice(),
        ))
    assert exc.value.status_code == 403
    # No DB fetch should have happened
    assert not conn.fetch_calls


def test_export_non_root_cross_namespace_param_rejected(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.export_memories(
            category=None, limit=1000, offset=0,
            owner_id=None, namespace="other-ns", user=_alice(),
        ))
    assert exc.value.status_code == 403


def test_export_root_may_target_arbitrary_slice(monkeypatch):
    conn = _Conn(rows=[_memory_row(owner_id="bob", namespace="bob-ns")])
    _install(monkeypatch, conn)

    result = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id="bob", namespace="bob-ns", user=_root(),
    ))
    sql, args = conn.fetch_calls[-1]
    assert "bob" in args
    assert "bob-ns" in args
    assert result.record_count == 1


def test_export_envelope_shape(monkeypatch):
    row = _memory_row()
    conn = _Conn(rows=[row])
    _install(monkeypatch, conn)

    env = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, user=_alice(),
    ))

    assert env.mpf_version == "0.1.0"
    assert env.source_system == "mnemos"
    assert len(env.records) == 1
    rec = env.records[0]
    assert rec.kind == "memory"
    assert rec.payload_version == "mnemos-3.1"
    assert rec.id == "mem_alice1"
    assert rec.payload["content"] == "hello"
    # Timestamps ISO-serialized
    assert "2026-01" in rec.payload["created"]


def test_export_strips_none_payload_fields(monkeypatch):
    row = _memory_row()
    row["source_model"] = None
    row["source_provider"] = None
    conn = _Conn(rows=[row])
    _install(monkeypatch, conn)

    env = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, user=_alice(),
    ))
    payload = env.records[0].payload
    assert "source_model" not in payload
    assert "source_provider" not in payload


# ─── /v1/import ──────────────────────────────────────────────────────────────


def _envelope(records):
    return portability.MPFEnvelope(
        mpf_version="0.1.0",
        source_system="mnemos",
        records=records,
    )


def _memory_record(
    id: str = "mem_1",
    content: str = "body",
    owner_id: str = "alice",
    namespace: str = "alice-ns",
    category: str = "solutions",
    payload_version: str = "mnemos-3.1",
):
    return portability.MPFRecord(
        id=id,
        kind="memory",
        payload_version=payload_version,
        payload={
            "content": content,
            "category": category,
            "owner_id": owner_id,
            "namespace": namespace,
        },
    )


def test_import_forces_caller_owner_for_non_root(monkeypatch):
    """Non-root imports rewrite owner_id + namespace on every record
    so a malicious envelope can't smuggle bob's rows into alice's
    account by labeling them with bob's id."""
    conn = _Conn()
    _install(monkeypatch, conn)

    env = _envelope([_memory_record(owner_id="bob", namespace="bob-ns")])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 1
    # The INSERT args should bind alice's identity, not bob's
    insert = next(e for e in conn.executes if "INSERT INTO memories" in e[0])
    args = insert[1]
    assert "alice" in args
    assert "alice-ns" in args
    assert "bob" not in args


def test_import_root_with_preserve_owner_honors_envelope(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = _envelope([_memory_record(owner_id="bob", namespace="bob-ns")])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=True, user=_root(),
    ))
    assert stats.imported == 1
    insert = next(e for e in conn.executes if "INSERT INTO memories" in e[0])
    args = insert[1]
    assert "bob" in args
    assert "bob-ns" in args


def test_import_non_root_preserve_owner_rejected(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = _envelope([_memory_record()])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.import_memories(
            envelope=env, preserve_owner=True, user=_alice(),
        ))
    assert exc.value.status_code == 403


def test_import_counts_unsupported_kinds(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = portability.MPFEnvelope(records=[
        portability.MPFRecord(id="doc_1", kind="document", payload_version="1.10.0", payload={}),
        portability.MPFRecord(id="fact_1", kind="fact", payload_version="mpf-0.1", payload={}),
        _memory_record(),
    ])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 1
    assert stats.unsupported_kinds == {"document": 1, "fact": 1}


def test_import_payload_version_mismatch_skipped(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = _envelope([_memory_record(payload_version="mnemos-2.4")])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 0
    assert stats.skipped == 1
    assert any("mnemos-2.4" in e for e in stats.errors)


def test_import_empty_content_fails(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = _envelope([_memory_record(content="  ")])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 0
    assert stats.failed == 1
    assert any("empty content" in e for e in stats.errors)


def test_import_wrong_mpf_version_returns_415(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = portability.MPFEnvelope(mpf_version="999.0.0", records=[])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.import_memories(
            envelope=env, preserve_owner=False, user=_alice(),
        ))
    assert exc.value.status_code == 415


def test_import_idempotent_on_id_collision(monkeypatch):
    """ON CONFLICT DO NOTHING surfaces as INSERT 0 0, which the handler
    counts as skipped (not imported). Re-importing the same envelope
    should not double-count."""
    class _DupeConn(_Conn):
        async def execute(self, sql, *args):
            self.executes.append((sql, args))
            return "INSERT 0 0"  # always conflict
    conn = _DupeConn()
    _install(monkeypatch, conn)

    env = _envelope([_memory_record(id="mem_dupe")])
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 0
    assert stats.skipped == 1
