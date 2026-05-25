"""Active-project scoping for document import."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tests._fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio


def _one_chunk_importer(mock_importer_class, *, text: str = "active project docs"):
    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        text,
        {"source_file": "scope.md"},
        [
            {
                "chunk_num": 0,
                "title": "Scope",
                "content": text,
                "metadata": {"source_file": "scope.md", "chunk_num": 0},
            }
        ],
    )
    mock_importer_class.return_value = mock_importer


def _insert_memory_calls(backend):
    return [call for call in backend.memories.calls if call[0] == "insert_memory"]


async def test_import_without_project_tag_returns_422(client, auth_headers):
    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("scope.md", b"# Scope")},
        data={"category": "documents"},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert "project_tag" in resp.text


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_with_project_tag_persists_metadata(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    backend = install_fake_backend(monkeypatch)
    _one_chunk_importer(mock_importer_class)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("scope.md", b"# Scope")},
        data={"category": "documents", "project_tag": "ic-engine"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    insert_calls = _insert_memory_calls(backend)
    assert len(insert_calls) == 1
    metadata = json.loads(insert_calls[-1][1]["metadata_json"])
    assert metadata["project_tag"] == "ic-engine"
    assert metadata["import_source"] == "doc-import"


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_archive_snapshot_rejected_by_default(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    backend = install_fake_backend(monkeypatch)
    _one_chunk_importer(
        mock_importer_class,
        text="[PYTHIA] /mnt/datapool/backups/old-project/README.md",
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("archive.md", b"# archived")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert "historical archive snapshot" in resp.text
    assert _insert_memory_calls(backend) == []


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_archive_snapshot_override_records_audit_metadata(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    backend = install_fake_backend(monkeypatch)
    _one_chunk_importer(
        mock_importer_class,
        text="[ARTEMIS] /Users/jperlow/.claude/plugins/cache/old/doc.md",
    )

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("archive.md", b"# archived")},
        data={
            "category": "documents",
            "project_tag": "mnemos",
            "allow_archive_snapshot": "true",
        },
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    insert_calls = _insert_memory_calls(backend)
    assert len(insert_calls) == 1
    metadata = json.loads(insert_calls[-1][1]["metadata_json"])
    assert metadata["archive_override_at"]
    assert metadata["archive_override_reason"] == "claude_plugin_cache"
