"""FB235 — archive retrieval and corruption.

Drives the real Persephone write/restore pair
(`mnemos.domain.persephone.runner.archive_memory` / `restore_memory`)
and the shared `archive_format` helpers over fake storage (no backend).

zstandard is an optional `persephone` extra, so the module skips cleanly
where it is absent (importorskip before any archive import).

Deviations pinned (verified by execution, do NOT "fix" them here):
- A truncated archive raises the raw ``zstd.ZstdError``: no wrapper names
  the row, but nothing partial is ever returned or restored.
- A legacy *uncompressed* body does NOT still read: the helpers reject
  non-zstd bytes and both restore paths reject ``compression_algo !=
  'zstd'`` with ValueError (fail-closed, opposite of "still reads").
- Concatenated zstd frames are undocumented; one-shot ``decompress``
  returns the FIRST frame silently and drops the rest.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

zstd = pytest.importorskip("zstandard")

from mnemos.domain.persephone.runner import archive_memory, restore_memory
from mnemos.persistence.archive_format import (
    ARCHIVE_CONTENT_PREFIX,
    ARCHIVE_SCHEMA_VERSION,
    _archive_payload,
    _compress_payload,
    _decompress_payload,
)

BODY = "100%_it's\\x\nsnowman \u2603 rocket \U0001f680 end"


def _live_row(memory_id: str = "m1", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": memory_id,
        "content": BODY,
        "category": "facts",
        "subcategory": None,
        "metadata": {"k": "v"},
        "quality_rating": 75,
        "verbatim_content": BODY,
        "owner_id": "owner-1",
        "group_id": None,
        "namespace": "default",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "source_memories": None,
        "provenance": None,
        "morpheus_run_id": None,
        "consolidated_into": None,
        "triples_extracted_at": None,
        "recall_count": 3,
        "last_recalled_at": datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc),
        "created": datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc),
        "updated": datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc),
        "archived_at": None,
    }
    row.update(over)
    return row


class _TxCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    """Fake asyncpg-style connection: fetchrow serves a row store, execute records."""

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _TxCM:
        return _TxCM()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.statements.append((sql, args))
        return self.rows.get(args[0]) if args else None

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append((sql, args))
        if sql.strip().upper().startswith("UPDATE"):
            return "UPDATE 1"
        return "SELECT 1"

    def writes(self, needle: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(s, a) for s, a in self.statements if needle in s]


@pytest.fixture
def quiet_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNEMOS_AUDIT_CHAIN", raising=False)


# --- 1. write-path round trip ----------------------------------------------


def test_write_path_roundtrip_byte_identical() -> None:
    payload = _archive_payload(_live_row())
    assert payload["schema_version"] == ARCHIVE_SCHEMA_VERSION
    compressed, original_size = _compress_payload(payload)
    assert isinstance(compressed, bytes) and original_size > len(compressed)
    memory = _decompress_payload(compressed)["memory"]
    assert memory["content"] == BODY
    assert memory["verbatim_content"] == BODY
    assert memory["metadata"] == {"k": "v"}
    # Datetimes cross the archive as ISO strings (coercion, not loss).
    assert memory["created"] == "2026-11-01T00:00:00+00:00"


# --- 2. archive -> restore over fake storage --------------------------------


@pytest.mark.asyncio
async def test_archive_then_restore_fake_storage(quiet_audit: None) -> None:
    conn = _FakeConn({"m1": _live_row()})
    await archive_memory(conn, "m1", archived_by="tester")
    inserts = conn.writes("INSERT INTO memory_archive")
    assert len(inserts) == 1
    sql, args = inserts[0]
    assert args[0] == "m1" and args[1] == "tester"
    # 'zstd' is a SQL literal in the INSERT, not a bound param.
    assert "'zstd'" in sql
    blob, original_size, compressed_size, version = args[2:6]
    assert version == ARCHIVE_SCHEMA_VERSION
    assert compressed_size == len(blob) and original_size > compressed_size
    live_updates = conn.writes("UPDATE memories")
    assert len(live_updates) == 1
    assert live_updates[0][1][1] == f"{ARCHIVE_CONTENT_PREFIX}m1"

    # The archive row now exists; the live row is archived.
    conn2 = _FakeConn(
        {
            "m1": {
                "id": "m1",
                "archived_at": datetime.now(timezone.utc),
                "compressed_content": blob,
                "compression_algo": "zstd",
            }
        }
    )
    await restore_memory(conn2, "m1", restored_by="tester")
    restores = conn2.writes("UPDATE memories")
    assert len(restores) == 1
    assert restores[0][1][1] == BODY, "restored content must be byte-identical"
    assert restores[0][1][2] == BODY
    deletes = conn2.writes("DELETE FROM memory_archive")
    assert len(deletes) == 1 and deletes[0][1] == ("m1",)


# --- 3. truncation fails closed ---------------------------------------------


def test_truncated_archive_raises_never_partial() -> None:
    blob, _ = _compress_payload(_archive_payload(_live_row()))
    with pytest.raises(zstd.ZstdError):
        _decompress_payload(blob[: len(blob) // 2])
    with pytest.raises(zstd.ZstdError):
        _decompress_payload(b"")


@pytest.mark.asyncio
async def test_truncated_archive_restores_nothing(quiet_audit: None) -> None:
    blob, _ = _compress_payload(_archive_payload(_live_row()))
    conn = _FakeConn(
        {
            "m1": {
                "id": "m1",
                "archived_at": datetime.now(timezone.utc),
                "compressed_content": blob[: len(blob) // 2],
                "compression_algo": "zstd",
            }
        }
    )
    # Deviation pinned: the raw zstd error propagates — no wrapper names
    # the row. What holds: the restore writes nothing, deletes nothing,
    # and returns no partial content.
    with pytest.raises(Exception):
        await restore_memory(conn, "m1")
    assert conn.writes("UPDATE memories") == []
    assert conn.writes("DELETE FROM memory_archive") == []


# --- 4. legacy uncompressed body fails closed ---------------------------------


def test_legacy_uncompressed_body_rejected() -> None:
    # Deviation pinned: an uncompressed JSON body from a hypothetical
    # older writer does NOT still read — the helper only accepts zstd.
    legacy = ('{"schema_version": 1, "memory": {"content": "old plain body"}}').encode()
    with pytest.raises(Exception):
        _decompress_payload(legacy)


@pytest.mark.asyncio
async def test_restore_rejects_non_zstd_algo(quiet_audit: None) -> None:
    conn = _FakeConn(
        {
            "m1": {
                "id": "m1",
                "archived_at": datetime.now(timezone.utc),
                "compressed_content": b"not zstd at all",
                "compression_algo": "none",
            }
        }
    )
    with pytest.raises(ValueError, match="compression_algo"):
        await restore_memory(conn, "m1")
    assert conn.writes("UPDATE memories") == []


# --- 5. concatenated frames: first wins, silently ------------------------------


def test_concatenated_frames_first_frame_wins() -> None:
    first, _ = _compress_payload(_archive_payload(_live_row("a", content="first")))
    second, _ = _compress_payload(_live_row("b", content="second"))
    # Deviation pinned: undocumented — one-shot decompress returns the
    # first frame and silently drops the second.
    memory = _decompress_payload(first + second)["memory"]
    assert memory["content"] == "first"


# --- 6. corrupt payloads -------------------------------------------------------


def test_corrupt_payloads_rejected() -> None:
    bad_version, _ = _compress_payload({"schema_version": 999, "memory": {"content": "x"}})
    with pytest.raises(ValueError, match="schema_version"):
        _decompress_payload(bad_version)
    no_content, _ = _compress_payload({"schema_version": 1, "memory": {}})
    with pytest.raises(ValueError, match="invalid memory archive"):
        _decompress_payload(no_content)


# --- 7. archive is idempotent ----------------------------------------------------


@pytest.mark.asyncio
async def test_archive_already_archived_is_noop(quiet_audit: None) -> None:
    row = _live_row(archived_at=datetime.now(timezone.utc))
    row["content"] = f"{ARCHIVE_CONTENT_PREFIX}m1"
    conn = _FakeConn({"m1": row})
    await archive_memory(conn, "m1")
    assert conn.writes("INSERT INTO memory_archive") == []
    assert conn.writes("UPDATE memories") == []
