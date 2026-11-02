"""Cross-backend content-hash consistency for the federation digest.

``list_namespace_digest_rows`` is the server side of the anti-entropy digest:
two nodes compare bucket Merkle roots built from SHA-256 of each memory's
content. That comparison is only meaningful if every backend hashes the *same
bytes for the same content*. A backend that receives ``content`` as raw bytes
(or a LOB object) must normalise it to text before hashing -- otherwise it
hashes the repr ``"b'...'"`` while another backend hashes the decoded text,
and two nodes running different backends can never agree on a bucket.

These tests pin that contract on SQLite (runnable in-process) and assert the
normalisation helper's behaviour for the byte/LOB cases the CLOBs and BLOBs of
the other backends can produce.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from mnemos.persistence.sqlite import SqliteBackend


class _Settings:
    class database:
        embedding_dim = 1024


async def _backend_with_bytes_content(tmp_path, *, content_bytes: bytes, memory_id: str):
    backend = SqliteBackend(tmp_path / "digest-hash.db", _Settings())
    await backend.open()
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content="placeholder",
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=3,
            owner_id="default",
            namespace="default",
            permission_mode=644,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=None,
            embedding=None,
            created=datetime.now(timezone.utc),
            updated=datetime.now(timezone.utc),
        )
        # Force the stored content to raw bytes, emulating a driver that hands
        # back a BLOB/bytearray for this column.  Bind the raw value directly
        # on the underlying connection so the str-coercing helper is bypassed.
        cur = await tx.conn.execute(
            "UPDATE memories SET content = ? WHERE id = ?",
            (content_bytes, memory_id),
        )
        await cur.close()
    return backend


@pytest.mark.asyncio
async def test_digest_hashes_decoded_bytes_not_the_repr(tmp_path):
    """A bytes/LOB content column must hash as decoded text, not ``b'...'``."""
    text = "café note — digest"
    backend = await _backend_with_bytes_content(tmp_path, content_bytes=text.encode("utf-8"), memory_id="m-bytes")
    try:
        async with backend.transactional() as tx:
            rows = await backend.federation.list_namespace_digest_rows(tx, namespace="default")
    finally:
        await backend.close()

    assert len(rows) == 1
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    repr_hash = hashlib.sha256(str(text.encode("utf-8")).encode("utf-8")).hexdigest()
    assert rows[0]["content_hash"] == expected, (
        "bytes content was not normalised before hashing: got the hash of "
        f"{repr_hash == rows[0]['content_hash'] and 'the bytes repr' or 'something else'}, "
        "expected the hash of the decoded text"
    )
    assert rows[0]["content_hash"] != repr_hash


@pytest.mark.asyncio
async def test_digest_str_and_bytes_content_agree(tmp_path):
    """Identical content stored as str vs bytes yields the same digest hash."""
    text = "same logical content"

    backend_a = SqliteBackend(tmp_path / "a.db", _Settings())
    await backend_a.open()
    async with backend_a.transactional() as tx:
        await backend_a.memories.insert_memory(
            tx,
            memory_id="m-a",
            content=text,
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=3,
            owner_id="default",
            namespace="default",
            permission_mode=644,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=None,
            embedding=None,
            created=datetime.now(timezone.utc),
            updated=datetime.now(timezone.utc),
        )
    try:
        async with backend_a.transactional() as tx:
            rows_str = await backend_a.federation.list_namespace_digest_rows(tx, namespace="default")
    finally:
        await backend_a.close()

    backend_b = await _backend_with_bytes_content(tmp_path, content_bytes=text.encode("utf-8"), memory_id="m-b")
    try:
        async with backend_b.transactional() as tx:
            rows_bytes = await backend_b.federation.list_namespace_digest_rows(tx, namespace="default")
    finally:
        await backend_b.close()

    assert rows_str[0]["content_hash"] == rows_bytes[0]["content_hash"]
