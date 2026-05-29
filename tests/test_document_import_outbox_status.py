"""Regression coverage for document_import HTTP status semantics.

These tests exercise the backend-neutral PersistenceBackend path. The route
owns response shaping and chunk-key derivation; DocumentRepository owns the
actual backend-specific write. Tests that need to inject repository outcomes
replace the module-level repository with a small recording stub rather than
mocking asyncpg calls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mnemos.api.routes import document_import
from mnemos.domain.document_repo import DocumentRepository, ImportedDocumentChunk
from tests._fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio


class _RecordingDocumentRepo:
    def __init__(self, handler=None) -> None:
        self.calls: list[dict] = []
        self._delegate = DocumentRepository()
        self._handler = handler

    async def import_chunk(self, backend, tx, **kwargs):
        self.calls.append(dict(kwargs))
        if self._handler is not None:
            return await self._handler(backend, tx, kwargs)
        return await self._delegate.import_chunk(backend, tx, **kwargs)


class _FailingExitTx:
    def __init__(self, message: str = "simulated commit-ack timeout") -> None:
        self._message = message

    async def __aenter__(self):
        return SimpleNamespace(_fake=True, conn=SimpleNamespace())

    async def __aexit__(self, *exc_info):
        raise asyncio.TimeoutError(self._message)


def _stub_importer_with_chunks(mock_importer_class, n: int, *, contents=None):
    contents = contents or [f"chunk {i} content" for i in range(n)]
    mock_importer = MagicMock()
    mock_importer.parse_document.return_value = (
        "doc text",
        {"source_file": "doc.pdf"},
        [
            {
                "chunk_num": i,
                "title": f"Chunk {i}",
                "content": contents[i],
                "metadata": {"source_file": "doc.pdf", "chunk_num": i},
            }
            for i in range(n)
        ],
    )
    mock_importer_class.return_value = mock_importer


def _install_recording_repo(monkeypatch, handler=None) -> _RecordingDocumentRepo:
    repo = _RecordingDocumentRepo(handler=handler)
    monkeypatch.setattr(document_import, "_document_repo", repo)
    return repo


def _fail_transaction_exit(monkeypatch, backend, *, message: str = "simulated commit-ack timeout") -> None:
    def _transactional():
        return _FailingExitTx(message)

    monkeypatch.setattr(backend, "transactional", _transactional)


def _chunk_keys(repo: _RecordingDocumentRepo) -> list[str]:
    return [call["chunk_key"] for call in repo.calls]


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_all_chunks_committed_returns_200(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Happy path: every chunk's transaction commits -> 200."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=2)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["memories_created"] == 2
    assert body["errors"] == []
    assert backend.commits == 2
    assert backend.rollbacks == 0


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_partial_failure_returns_207_multi_status(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Some chunks commit, some roll back -> 207 Multi-Status."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=3)

    call_count = {"n": 0}

    async def _flaky_dispatch(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("webhook table unavailable")
        return []

    monkeypatch.setattr(backend.webhooks, "dispatch_event", _flaky_dispatch)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 207, resp.text
    body = resp.json()
    assert body["memories_created"] == 2
    assert len(body["errors"]) == 1
    assert body["errors"][0]["chunk"] == 2
    assert backend.commits == 2
    assert backend.rollbacks == 1


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_total_failure_returns_502(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Zero chunks commit on content/outbox failure -> 502 Bad Gateway."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=2)

    async def _always_fail(*args, **kwargs):
        raise RuntimeError("webhook table unavailable")

    monkeypatch.setattr(backend.webhooks, "dispatch_event", _always_fail)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 502
    body = resp.json()
    assert body["memories_created"] == 0
    assert len(body["errors"]) == 2
    assert backend.commits == 0
    assert backend.rollbacks == 2


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_partial_failure_returns_207(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """File 1 succeeds, file 2 fails -> top-level 207."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)

    call_count = {"n": 0}

    async def _flaky_dispatch(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        raise RuntimeError("simulated for file 2")

    monkeypatch.setattr(backend.webhooks, "dispatch_event", _flaky_dispatch)

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
            ("files", ("doc2.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 207, resp.text
    results = resp.json()
    assert len(results) == 2
    assert results[0]["status_code"] == 200
    assert results[1]["status_code"] == 502
    for entry in results:
        assert "raw_headers" not in entry


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_total_failure_returns_502(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Every file fails its only chunk -> top-level 502."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)

    async def _always_fail(*args, **kwargs):
        raise RuntimeError("webhook table unavailable")

    monkeypatch.setattr(backend.webhooks, "dispatch_event", _always_fail)

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
            ("files", ("doc2.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 502
    results = resp.json()
    assert len(results) == 2
    for entry in results:
        assert entry["memories_created"] == 0
        assert entry["status_code"] == 502


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
async def test_batch_import_all_empty_files_returns_207_not_502(
    client,
    auth_headers,
):
    """Client-error files are not retryable gateway failures."""
    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("empty1.pdf", b"")),
            ("files", ("empty2.pdf", b"")),
        ],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 207, resp.text
    results = resp.json()
    assert len(results) == 2
    for entry in results:
        assert entry["status_code"] == 400
        assert entry["memories_created"] == 0


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_all_success_returns_200(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Every file fully imports -> batch returns 200."""
    install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
            ("files", ("doc2.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 2
    for entry in results:
        assert entry["memories_created"] == 1
        assert entry["status_code"] == 200


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_mid_batch_pool_loss_returns_top_level_503(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Backend disappears after file 1 -> file 2 gets per-file 503 and top-level 503."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)

    import mnemos.core.lifecycle as lc

    call_count = {"n": 0}

    async def _dispatch_then_drop_backend(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            monkeypatch.setattr(lc, "_persistence_backend", None)
        return []

    monkeypatch.setattr(backend.webhooks, "dispatch_event", _dispatch_then_drop_backend)

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("doc1.pdf", b"%PDF-1.4\nx")),
            ("files", ("doc2.pdf", b"%PDF-1.4\nx")),
        ],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 503, resp.text
    results = resp.json()
    assert len(results) == 2
    assert [r["status_code"] for r in results] == [200, 503]


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_single_import_mid_file_infra_loss_preserves_committed_chunks(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Chunk 1 commits, chunk 2 hits infra loss -> 503 preserves chunk 1."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=2)

    call_count = {"n": 0}

    async def _dispatch_then_infra_fail(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise asyncio.TimeoutError("simulated pool-acquire timeout")
        return []

    monkeypatch.setattr(backend.webhooks, "dispatch_event", _dispatch_then_infra_fail)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 503, resp.text
    payload = resp.json()
    assert payload["memories_created"] == 1
    assert len(payload["memory_ids"]) == 1
    assert any("infrastructure" in (e.get("error") or "").lower() for e in payload.get("errors", []))


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_single_import_transaction_exit_infra_loss_surfaces_unconfirmed_id(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Commit-ack loss surfaces the in-flight canonical id as unconfirmed."""
    backend = install_fake_backend(monkeypatch)
    _fail_transaction_exit(monkeypatch, backend)
    _stub_importer_with_chunks(mock_importer_class, n=1)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 503, resp.text
    payload = resp.json()
    assert payload["memories_created"] == 0
    assert payload["memory_ids"] == []
    assert len(payload.get("unconfirmed_memory_ids") or []) == 1


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_batch_import_mid_file_infra_loss_preserves_per_file_committed_chunks(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Batch preserves per-file committed IDs when a later chunk has infra loss."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=2)

    call_count = {"n": 0}

    async def _dispatch_then_infra_fail(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise asyncio.TimeoutError("simulated pool-acquire timeout")
        return []

    monkeypatch.setattr(backend.webhooks, "dispatch_event", _dispatch_then_infra_fail)

    resp = await client.post(
        "/v1/documents/batch-import",
        files=[("files", ("doc1.pdf", b"%PDF-1.4\nx"))],
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 503, resp.text
    results = resp.json()
    assert len(results) == 1
    entry = results[0]
    assert entry["status_code"] == 503
    assert entry["memories_created"] == 1
    assert len(entry["memory_ids"]) == 1


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_uses_canonical_id_from_returning_clause(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """The route surfaces the canonical id returned by DocumentRepository."""
    install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)
    canonical_id = "mem_canonical_from_existing_row"

    async def _return_canonical(_backend, _tx, kwargs):
        assert kwargs["memory_id"] != canonical_id
        return ImportedDocumentChunk(memory_id=canonical_id)

    repo = _install_recording_repo(monkeypatch, _return_canonical)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["memories_created"] == 1
    assert payload["memory_ids"] == [canonical_id]
    assert len(repo.calls) == 1


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_chunk_key_is_stable_and_present_in_insert(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Same file + chunk + params produce the same sha256 chunk key."""
    install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)
    repo = _install_recording_repo(monkeypatch)

    resp1 = await client.post(
        "/v1/documents/import",
        files={"file": ("stable.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    chunk_key_1 = _chunk_keys(repo)[-1]
    assert isinstance(chunk_key_1, str)
    assert len(chunk_key_1) == 64
    assert all(c in "0123456789abcdef" for c in chunk_key_1)

    resp2 = await client.post(
        "/v1/documents/import",
        files={"file": ("stable.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    chunk_key_2 = _chunk_keys(repo)[-1]

    assert chunk_key_1 == chunk_key_2


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_chunk_key_includes_content_digest(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Same filename + chunk_num but different content produces a different key."""
    install_fake_backend(monkeypatch)
    repo = _install_recording_repo(monkeypatch)

    _stub_importer_with_chunks(
        mock_importer_class,
        n=1,
        contents=["original draft content"],
    )
    resp1 = await client.post(
        "/v1/documents/import",
        files={"file": ("draft.pdf", b"%PDF-1.4\nv1")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    chunk_key_v1 = _chunk_keys(repo)[-1]

    _stub_importer_with_chunks(
        mock_importer_class,
        n=1,
        contents=["revised draft content with new wording"],
    )
    resp2 = await client.post(
        "/v1/documents/import",
        files={"file": ("draft.pdf", b"%PDF-1.4\nv2")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    chunk_key_v2 = _chunk_keys(repo)[-1]

    assert chunk_key_v1 != chunk_key_v2


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_unconfirmed_memory_ids_uses_canonical_id_after_conflict(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Commit-ack loss after canonical resolution reports the canonical id."""
    backend = install_fake_backend(monkeypatch)
    _fail_transaction_exit(monkeypatch, backend)
    _stub_importer_with_chunks(mock_importer_class, n=1)
    canonical_id = "mem_canonical_existing_row"

    async def _return_canonical(_backend, _tx, _kwargs):
        return ImportedDocumentChunk(memory_id=canonical_id)

    _install_recording_repo(monkeypatch, _return_canonical)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 503, resp.text
    payload = resp.json()
    assert payload["memories_created"] == 0
    assert payload["memory_ids"] == []
    assert payload.get("unconfirmed_memory_ids") == [canonical_id]


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_chunk_key_includes_permission_mode(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Same file/content under different permission modes gets different keys."""
    install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)
    repo = _install_recording_repo(monkeypatch)

    resp1 = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos", "permission_mode": "644"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    chunk_key_644 = _chunk_keys(repo)[-1]

    resp2 = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos", "permission_mode": "600"},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    chunk_key_600 = _chunk_keys(repo)[-1]

    assert chunk_key_644 != chunk_key_600


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_chunk_key_includes_category(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Different category for the same content produces a different key."""
    install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)
    repo = _install_recording_repo(monkeypatch)

    resp1 = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    chunk_key_documents = _chunk_keys(repo)[-1]

    resp2 = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "research", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    chunk_key_research = _chunk_keys(repo)[-1]

    assert chunk_key_documents != chunk_key_research


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_import_resolves_legacy_v70_chunk_key_to_canonical_id(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """Legacy-key resolution returns an existing canonical id and skips created event."""
    backend = install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)
    legacy_canonical_id = "mem_legacy_row_already_existed"

    async def _return_legacy_canonical(_backend, _tx, kwargs):
        assert kwargs["legacy_chunk_key"] != kwargs["chunk_key"]
        return ImportedDocumentChunk(
            memory_id=legacy_canonical_id,
            emit_created_event=False,
        )

    _install_recording_repo(monkeypatch, _return_legacy_canonical)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["memories_created"] == 1
    assert payload["memory_ids"] == [legacy_canonical_id]
    assert backend.webhooks.calls == []


@patch("mnemos.api.routes.document_import.DOCLING_AVAILABLE", True)
@patch("mnemos.api.routes.document_import.DoclingImporter")
async def test_legacy_update_falls_through_to_on_conflict_on_unique_violation(
    mock_importer_class,
    client,
    auth_headers,
    monkeypatch,
):
    """When legacy resolution falls through, the repository's canonical id is surfaced."""
    install_fake_backend(monkeypatch)
    _stub_importer_with_chunks(mock_importer_class, n=1)
    insert_canonical_id = "mem_canonical_from_on_conflict_path"

    async def _return_on_conflict_canonical(_backend, _tx, kwargs):
        assert kwargs["legacy_chunk_key"] != kwargs["chunk_key"]
        return ImportedDocumentChunk(memory_id=insert_canonical_id)

    repo = _install_recording_repo(monkeypatch, _return_on_conflict_canonical)

    resp = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.pdf", b"%PDF-1.4\nx")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["memories_created"] == 1
    assert payload["memory_ids"] == [insert_canonical_id]
    assert len(repo.calls) == 1
