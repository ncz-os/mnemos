"""MN-02 / CF-05 (SEC F2.4): doc-import webhooks must not ship raw, vaulted chunks.

The document-import route dispatches a ``memory.created`` webhook payload
from inside the per-chunk transaction. The chunk content's secret
classification and the vault promotion both happen INSIDE
``DocumentRepository.import_chunk`` -- after the route has already built
its metadata dict -- so a payload assembled from the route's own local
values leaks two things to an external HTTP subscriber:

1. the RAW chunk text (stored ingest spans never reached the payload), and
2. the pre-vault namespace, which contradicts the row actually persisted.

These tests pin the contract: the outbound payload carries the redacted
content and the namespace the repository says it persisted.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests._charon_fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio

# Vault-class credential: ``classify()`` returns SecretClass.VAULT with a
# span over the password literal, so the chunk is auto-vaulted at ingest.
SECRET_LITERAL = "DenylistSelfTest2NotReal99"
SECRET_TEXT = f"INFRASTRUCTURE CREDENTIALS: TYPHON root login password is {SECRET_LITERAL}"


def _stub_importer_with_secret_chunk(mock_importer_class) -> None:
    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        "doc text",
        {"source_file": "creds.pdf"},
        [
            {
                "chunk_num": 0,
                "title": "Chunk 0",
                "content": SECRET_TEXT,
                "metadata": {"source_file": "creds.pdf", "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_created_webhook_payload_redacts_vaulted_chunk(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_secret_chunk(mock_importer_class)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("creds.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    assert len(backend.webhooks.calls) == 1
    _event, call = backend.webhooks.calls[0]
    payload = call["payload"]
    assert payload["source"] == "document_import"
    assert SECRET_LITERAL not in payload["content"]
    assert payload["content"] == "INFRASTRUCTURE CREDENTIALS: TYPHON root login password is [REDACTED]"
    # The row was written to the vault namespace; the event must say so.
    assert payload["namespace"] == "vault"
    assert call["namespace"] == "vault"


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_clean_chunk_webhook_payload_is_unchanged(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """No secret spans -> payload content passes through byte-for-byte."""
    backend = install_fake_backend(monkeypatch)
    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        "doc text",
        {"source_file": "notes.pdf"},
        [
            {
                "chunk_num": 0,
                "title": "Chunk 0",
                "content": "Deployment runbook: restart the worker pool.",
                "metadata": {"source_file": "notes.pdf", "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("notes.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    _event, call = backend.webhooks.calls[0]
    assert call["payload"]["content"] == "Deployment runbook: restart the worker pool."
    assert call["payload"]["namespace"] == call["namespace"]
    assert call["payload"]["namespace"] != "vault"
