"""MN-02 / CF-05 (SEC F2.4): the stored-row metadata reported by ``import_chunk``.

``DocumentRepository._import_postgres_chunk`` returns an
``ImportedDocumentChunk`` describing the row the INSERT actually persisted.
The route dispatches the ``memory.created`` webhook from that value, so a
wrong ``metadata``/``namespace`` here leaks raw chunk text or the pre-vault
namespace to an external subscriber.

Two pieces of that contract are pinned here:

* ``_metadata_obj`` -- the decode helper that turns the serialised
  (already-classified) ``metadata_json`` back into the dict the caller
  reports. It must never raise on a malformed blob: the write has already
  committed by the time it runs, so degrading to ``{}`` is the only safe
  answer.
* the legacy-resolution branch -- an existing row matched by
  ``import_chunk_key`` is reused, ``classified.metadata`` was never written
  to it, and the call must therefore report no created event rather than
  inventing metadata for a row it did not insert.
"""

from __future__ import annotations

import json

import pytest

from mnemos.domain.document_repo import (
    DocumentRepository,
    _metadata_obj,
)


def test_metadata_obj_round_trips_the_serialised_classification():
    stored = {"source_file": "creds.pdf", "chunk_num": 0, "secret_spans": [{"start": 0, "end": 9}]}
    assert _metadata_obj(json.dumps(stored)) == stored


@pytest.mark.parametrize(
    "blob",
    [
        "",
        "not json at all",
        "{unclosed",
        "[1, 2, 3]",  # valid JSON, wrong shape for a metadata column
        '"a string"',
        "null",
        "42",
    ],
)
def test_metadata_obj_is_total(blob):
    """A malformed or non-object blob degrades to {} instead of raising.

    ``import_chunk`` calls this AFTER the row is committed, so an exception
    here would turn a successful import into a 500 the caller cannot retry
    (the chunk key already exists).
    """
    assert _metadata_obj(blob) == {}


class _LegacyConn:
    """Minimal asyncpg-shaped conn whose legacy UPDATE matches a row."""

    def __init__(self, legacy_id: str | None) -> None:
        self._legacy_id = legacy_id
        self.fetchvals: list[str] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    async def fetchval(self, sql: str, *args):
        self.fetchvals.append(" ".join(sql.split()).upper())
        if "SET IMPORT_CHUNK_KEY" in " ".join(sql.split()).upper():
            return self._legacy_id
        return "canonical-id"


@pytest.mark.asyncio
async def test_legacy_resolution_reports_no_created_event_and_no_invented_metadata():
    """An existing row matched by chunk key is reused, not re-inserted.

    The legacy UPDATE never wrote ``classified.metadata``, so reporting it
    would describe a row the caller did not create. The event must be
    suppressed and no metadata claimed.
    """
    conn = _LegacyConn(legacy_id="existing-memory")

    imported = await DocumentRepository()._import_postgres_chunk(
        conn,
        memory_id="new-memory",
        content="Deployment runbook: restart the worker pool.",
        category="documents",
        subcategory="notes",
        metadata_json=json.dumps({"source_file": "notes.pdf", "chunk_num": 0}),
        owner_id="owner-1",
        namespace="default",
        permission_mode=0,
        chunk_key="chunk-new",
        legacy_chunk_key="chunk-old",
    )

    assert imported.memory_id == "existing-memory"
    assert imported.emit_created_event is False
    assert imported.metadata is None
    # Only the legacy UPDATE ran -- the INSERT was skipped entirely.
    assert len(conn.fetchvals) == 1
