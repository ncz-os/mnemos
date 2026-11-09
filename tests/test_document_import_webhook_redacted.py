"""F2: document-import ``memory.created`` webhooks must not leak raw secrets.

The document-import route classifies every chunk through
``DocumentRepository.import_chunk`` (which routes through
``classify_persisted_text_fields``), so a chunk carrying a credential is
stored in the vault namespace with the secret spans recorded in its
metadata.  The ``memory.created`` webhook emitted right after the write used
to send the RAW chunk content and the CALLER's namespace, leaking both the
credential and the pre-vault namespace to an external HTTP endpoint.

These tests use a real ``SqliteBackend`` so the actual classification and
storage happen, stub only the document parser, and capture the payload the
route hands to the transactional outbox (``dispatch_event``).  That payload
is exactly what leaves the server, so asserting on it asserts on the leak.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from mnemos.api.routes import document_import
from mnemos.core.secret_detection import VAULT_NAMESPACE

pytestmark = pytest.mark.asyncio

# Synthetic, non-functional credentials. The chunk is a single text span so
# the classifier records both spans and vaults the record (a credential-record
# memory rather than incidental prose).
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
GITHUB_TOKEN = "ghp_" + "A" * 36
SECRET_CHUNK = f"deploy notes: aws key {AWS_KEY} and github token {GITHUB_TOKEN} end"


class _User:
    """Minimal UserContext: a non-root caller in a non-vault namespace."""

    user_id = "alice"
    namespace = "team"
    role = "user"
    group_ids: list[str] = []
    authenticated = True
    api_key_id = None


class _Upload:
    """Stand-in for ``fastapi.UploadFile`` (only what the route reads)."""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


class _CapturingWebhooks:
    """Wrap a real webhook repo and capture the payloads the route emits."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[dict] = []

    async def dispatch_event(self, tx, event_type, payload, *, owner_id=None, namespace=None):
        self.calls.append(
            {
                "event_type": event_type,
                "payload": payload,
                "owner_id": owner_id,
                "namespace": namespace,
            }
        )
        return []


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    import mnemos.core.lifecycle as lc
    from mnemos.persistence import SqliteBackend

    backend = SqliteBackend(tmp_path / "doc_import_webhook.sqlite3", SimpleNamespace())
    await backend.open()
    monkeypatch.setattr(lc, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(lc, "_cache", None, raising=False)
    try:
        yield backend
    finally:
        await backend.close()


def _stub_parser(monkeypatch, chunks):
    importer = MagicMock()
    importer.parse_document.return_value = (
        "doc text",
        {"source_file": "doc.pdf"},
        chunks,
    )
    monkeypatch.setattr(document_import, "DOCLING_AVAILABLE", True)
    monkeypatch.setattr(document_import, "DoclingImporter", lambda: importer)
    return importer


async def _run_import(backend, monkeypatch, content: str):
    """Drive the route end to end and return (payload, captured webhooks)."""
    _stub_parser(
        monkeypatch,
        [
            {
                "chunk_num": 0,
                "title": "Chunk 0",
                "content": content,
                "metadata": {"source_file": "doc.pdf", "chunk_num": 0},
            }
        ],
    )
    capturing = _CapturingWebhooks(backend.webhooks)
    monkeypatch.setattr(type(backend), "webhooks", property(lambda _self: capturing))
    monkeypatch.setattr(backend, "supports_webhooks", True, raising=False)

    payload, status_code = await document_import.import_memories_from_document(
        file=_Upload("doc.pdf", b"%PDF-1.4\nx"),
        category="documents",
        subcategory=None,
        permission_mode=None,
        project_tag="mnemos",
        allow_archive_snapshot=False,
        user=_User(),
    )
    return payload, status_code, capturing


async def test_webhook_masks_secrets_and_uses_stored_namespace(sqlite_backend, monkeypatch):
    """A vaulted chunk's webhook carries masked content and namespace=vault."""
    payload, status_code, capturing = await _run_import(sqlite_backend, monkeypatch, SECRET_CHUNK)

    assert status_code == 200, payload
    assert payload["memories_created"] == 1
    assert len(capturing.calls) == 1

    event = capturing.calls[0]
    assert event["event_type"] == "memory.created"
    data = event["payload"]

    # The raw credentials must never reach the outbound payload.
    assert AWS_KEY not in data["content"]
    assert GITHUB_TOKEN not in data["content"]
    assert "[REDACTED" in data["content"] or "REDACTED" in data["content"]

    # The event must describe the row as stored, not the caller's namespace.
    assert data["namespace"] == VAULT_NAMESPACE
    assert data["namespace"] != "team"

    # Sanity: the row really was vaulted (this is what makes the payload lie).
    from mnemos.persistence.visibility import VisibilityFilter

    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.memories.get_memory(
            tx,
            payload["memory_ids"][0],
            visibility=VisibilityFilter.for_read(_Root(), namespace=VAULT_NAMESPACE),
        )
    assert row is not None
    assert row["namespace"] == VAULT_NAMESPACE
    stored_meta = row["metadata"]
    if isinstance(stored_meta, str):
        stored_meta = json.loads(stored_meta)
    assert stored_meta["secret_redact_fields"]["content"]


class _Root:
    user_id = "root"
    namespace = "default"
    role = "root"
    group_ids: list[str] = []
    authenticated = True
    api_key_id = None


async def test_clean_chunk_webhook_is_unchanged(sqlite_backend, monkeypatch):
    """A clean chunk keeps its content and pre-vault namespace (no regression)."""
    clean = "quarterly planning notes, no credentials here"
    payload, status_code, capturing = await _run_import(sqlite_backend, monkeypatch, clean)

    assert status_code == 200, payload
    assert len(capturing.calls) == 1
    data = capturing.calls[0]["payload"]
    assert data["content"] == clean
    assert data["namespace"] == "team"
    assert capturing.calls[0]["namespace"] == "team"
