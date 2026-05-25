"""Regression coverage for permission_mode plumbing in document_import."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests._fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio


def _stub_importer_with_one_chunk(mock_importer_class, *, filename: str, content: str):
    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        "doc text",
        {"source_file": filename},
        [
            {
                "chunk_num": 0,
                "title": "Chunk",
                "content": content,
                "metadata": {"source_file": filename, "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer


def _last_insert_kwargs(backend) -> dict:
    insert_calls = [
        payload
        for method, payload in backend.memories.calls
        if method == "insert_memory"
    ]
    assert insert_calls
    return insert_calls[-1]


async def test_import_document_rejects_invalid_permission_mode(client, auth_headers):
    """422 fires before DOCLING/backend checks so bad input is visible immediately."""
    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("test.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos", "permission_mode": "999"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "permission_mode" in resp.text


async def test_batch_import_rejects_invalid_permission_mode(client, auth_headers):
    resp = await client.post(
        "/v1/documents/batch-import",
        files=[("files", ("doc1.pdf", b"%PDF-1.4\nx"))],
        data={"category": "documents", "project_tag": "mnemos", "permission_mode": "888"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "permission_mode" in resp.text


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_document_persists_explicit_permission_mode(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Explicit permission_mode=644 reaches the backend memory repository."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_one_chunk(
        mock_importer_class,
        filename="fed.pdf",
        content="federation-visible chunk",
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("fed.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos", "permission_mode": "644"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    assert _last_insert_kwargs(backend)["permission_mode"] == 644


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_document_defaults_to_600(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_one_chunk(
        mock_importer_class,
        filename="default.pdf",
        content="default-perm chunk",
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("default.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    assert _last_insert_kwargs(backend)["permission_mode"] == 600
