"""Compression artifacts in hot retrieval paths.

Codex memory-OS audit 019dbd11 flagged: "compression remains mostly
out of the runtime retrieval path; both search and rehydrate
explicitly defer real mnemos.domain.compression." This commit wires the v3.1
contest winner into:
  - /v1/chat/completions context injection (_search_mnemos_context)
  - /v1/memories/rehydrate

Both now LEFT JOIN memory_compressed_variants and prefer the winner's
compressed_content over raw content.

Tests verify:
  - gateway SQL JOINs the variants table and COALESCEs
  - rehydrate SQL JOINs the variants table and flags variant_used
  - rehydrate response reports compression_applied=True and a
    compression_ratio < 1.0 when variant content is present
  - rehydrate response reports compression_applied=False when
    every row returned only raw content
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

from mnemos.api.dependencies import UserContext
from tests._fake_backend import install_fake_postgres_backend


def _alice() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.fetches: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetches.append((sql, args))
        return self._rows


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import mnemos.core.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def _is_context_query(sql: str) -> bool:
    """True only for the gateway's memory-context SELECT.

    Identified by the variant JOIN *and* the COALESCE projection together:
    either token alone also appears in the rehydrate statement, and matching
    on a fragment alone would answer the wrong call.
    """
    normalized = " ".join(sql.split()).lower()
    return "left join memory_compressed_variants" in normalized and "coalesce(v.compressed_content" in normalized


class _GatewayContextConn(_Conn):
    """Fake asyncpg connection that answers the gateway's memory-context query.

    Serves the rows the real context statement returns — ``id`` and the
    ``COALESCE(v.compressed_content, m.content) AS content`` winner — while
    recording every statement so the JOIN itself stays asserted. This is what
    an asyncpg connection looks like from the repository's side; the
    production SQL is executed for real by ``fetch_memory_context``.

    Any statement that is not the context query raises: an unrecognised call
    means the repository changed, and answering it with ``[]`` would hide
    that behind a green test.
    """

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        if not _is_context_query(sql):
            raise AssertionError(
                f"unexpected statement on the gateway context connection: {' '.join(sql.split())[:120]!r}"
            )
        return [
            {
                "id": row["id"],
                "category": row.get("category"),
                "content": row["content"],
                "namespace": row.get("namespace"),
            }
            for row in self._rows
        ]


def _install_gateway(monkeypatch, conn):
    """Wire ``conn`` underneath the REAL Postgres memory repository.

    The gateway's context search goes through the persistence ABC (F-06), so
    a fake pool on its own is never consulted. ``conn`` is asyncpg-shaped
    while ``fetch_memory_context`` is the production implementation, so the
    SQL asserted below is exactly the SQL the Postgres backend emits.
    """
    return install_fake_postgres_backend(monkeypatch, conn)


# ─── gateway _search_mnemos_context ─────────────────────────────────────────


def test_gateway_joins_variants_table(monkeypatch):
    from mnemos.api.routes import openai_compat

    conn = _GatewayContextConn()
    _install_gateway(monkeypatch, conn)
    asyncio.run(openai_compat._search_mnemos_context("hello", _alice()))

    # The gateway runs exactly one memory-context query; the fake raises on
    # anything else, so this list is the whole conversation with storage.
    context_sqls = [sql for sql, _args in conn.fetches if _is_context_query(sql)]
    assert len(context_sqls) == 1, f"expected one context query, got {conn.fetches!r}"
    sql = " ".join(context_sqls[0].split())
    assert "LEFT JOIN memory_compressed_variants" in sql
    assert "COALESCE(v.compressed_content, m.content) AS content" in sql
    assert "m.content" in sql  # fallback tier in the COALESCE chain


def test_gateway_uses_variant_content_when_available(monkeypatch):
    """The SQL COALESCEs (v.compressed_content, m.content) into a column
    aliased as ``content``; the gateway must pass the winning value through
    untouched (modulo the untrusted-data framing)."""
    from mnemos.api.routes import openai_compat

    compressed = "short variant form"
    raw = "very long raw content " * 40
    rows = [
        # What the SQL COALESCE returns: the variant won for mem_1, the raw
        # column won for mem_2 (no compressed variant for that memory).
        {"id": "mem_1", "category": "solutions", "content": compressed, "namespace": "default"},
        {"id": "mem_2", "category": "patterns", "content": raw, "namespace": "default"},
    ]

    conn = _GatewayContextConn(rows=rows)
    _install_gateway(monkeypatch, conn)

    out = asyncio.run(openai_compat._search_mnemos_context("query", _alice(), limit=5))

    context_sql = " ".join(
        next(sql for sql, _args in conn.fetches if _is_context_query(sql)).split()
    )
    assert "LEFT JOIN memory_compressed_variants" in context_sql, (
        "the served rows must come from a variant-joining query"
    )
    assert "COALESCE(v.compressed_content, m.content) AS content" in context_sql
    assert len(out) == 2
    assert compressed in out[0]["content"]
    assert out[1]["content"].count("very long raw content") == 40


# ─── search ─────────────────────────────────────────────────────────────────


def test_search_large_results_do_not_probe_legacy_backend(monkeypatch):
    """Large search result sets keep the normal response shape without
    probing the removed DISTILLATION_BACKEND/OLLAMA_HOST path.

    Slice 1d migrated search dispatch to ``backend.memories.fts_search``;
    the test uses the FakeBackend test helper to feed a configured row
    set and asserts the response shape — no SQL-shape coupling.
    """
    from mnemos.api.routes import memories
    from mnemos.domain.models import MemorySearchRequest

    from tests._fake_backend import install_fake_backend

    async def _noop_recall_bump(_ids):
        return None

    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [
        {
            "id": "mem_big",
            "content": "x" * (51 * 1024),
            "category": "general",
            "subcategory": None,
            "created": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "updated": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "metadata": None,
            "quality_rating": 90,
            "compressed_content": None,
            "verbatim_content": None,
            "owner_id": "alice",
            "group_id": None,
            "namespace": "default",
            "permission_mode": 600,
            "source_model": None,
            "source_provider": None,
            "source_session": None,
            "source_agent": None,
        },
    ])
    monkeypatch.setattr(memories, "_bump_recall_counters", _noop_recall_bump)

    req = MemorySearchRequest(query="needle", limit=1)
    resp = asyncio.run(memories.search_memories(req, user=_alice()))

    assert resp.count == 1
    assert resp.compression_applied is False
    assert resp.compression_metadata is None


# ─── rehydrate ──────────────────────────────────────────────────────────────


def test_rehydrate_joins_variants_and_tracks_variant_used(monkeypatch):
    """SQL must LEFT JOIN memory_compressed_variants and expose
    `variant_used` so the handler can report compression_applied."""
    from mnemos.api.routes import memories
    from mnemos.domain.models import RehydrationRequest

    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    req = RehydrationRequest(query="hello", limit=10)
    asyncio.run(memories.rehydrate_memories(req, user=_alice()))

    assert conn.fetches, "expected a fetch for rehydrate"
    sql = conn.fetches[-1][0]
    assert "LEFT JOIN memory_compressed_variants" in sql
    assert "variant_used" in sql
    assert "v.compressed_content AS compressed_content" in sql


def test_rehydrate_compression_applied_true_when_variant_present(monkeypatch):
    """If any row comes back with variant_used=True, the response
    reports compression_applied=True and a compression_ratio<1.0."""
    from mnemos.api.routes import memories
    from mnemos.domain.models import RehydrationRequest

    conn = _Conn(rows=[
        {
            "id": "mem_1", "category": "solutions",
            "created": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "quality_rating": 80,
            "raw_content": "original content that is quite long " * 5,
            "compressed_content": "compressed short",  # shorter than raw
            "variant_used": True,
            "rank": 0.9,
        },
    ])
    _install(monkeypatch, conn)

    req = RehydrationRequest(query="q", limit=10)
    resp = asyncio.run(memories.rehydrate_memories(req, user=_alice()))

    assert resp.compression_applied is True
    assert resp.compression_ratio < 1.0
    assert resp.memories_included == 1


def test_rehydrate_compression_applied_false_when_no_variants(monkeypatch):
    """No variant rows -> compression_applied=False, ratio=1.0."""
    from mnemos.api.routes import memories
    from mnemos.domain.models import RehydrationRequest

    conn = _Conn(rows=[
        {
            "id": "mem_1", "category": "general",
            "created": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "quality_rating": 70,
            "raw_content": "raw unchanged content",
            "compressed_content": None,
            "variant_used": False,
            "rank": 0.5,
        },
    ])
    _install(monkeypatch, conn)

    req = RehydrationRequest(query="q", limit=10)
    resp = asyncio.run(memories.rehydrate_memories(req, user=_alice()))

    assert resp.compression_applied is False
    assert resp.compression_ratio == 1.0
