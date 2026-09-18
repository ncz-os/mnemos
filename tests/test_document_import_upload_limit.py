"""Regression coverage for bounded document import upload reads."""

import pytest
from fastapi import HTTPException

from mnemos.api.routes import document_import

pytestmark = pytest.mark.asyncio


class _ChunkedUpload:
    filename = "upload.pdf"

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, size=-1):
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if size >= 0 and len(chunk) > size:
            self._chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


async def test_read_upload_file_capped_allows_exact_limit(monkeypatch):
    monkeypatch.setenv("MNEMOS_DOCUMENT_IMPORT_MAX_BYTES", "4")

    content = await document_import._read_upload_file_capped(_ChunkedUpload([b"ab", b"cd"]))

    assert content == b"abcd"


async def test_read_upload_file_capped_rejects_oversized_file(monkeypatch):
    monkeypatch.setenv("MNEMOS_DOCUMENT_IMPORT_MAX_BYTES", "4")

    with pytest.raises(HTTPException) as exc:
        await document_import._read_upload_file_capped(_ChunkedUpload([b"ab", b"cd", b"e"]))

    assert exc.value.status_code == 413
    assert "MNEMOS_DOCUMENT_IMPORT_MAX_BYTES" in str(exc.value.detail)


async def test_read_upload_file_capped_uses_server_body_limit(monkeypatch):
    monkeypatch.delenv("MNEMOS_DOCUMENT_IMPORT_MAX_BYTES", raising=False)

    class _Server:
        max_body_bytes = 3

    class _Settings:
        server = _Server()

    monkeypatch.setattr(document_import, "get_settings", lambda: _Settings())

    with pytest.raises(HTTPException) as exc:
        await document_import._read_upload_file_capped(_ChunkedUpload([b"ab", b"cd"]))

    assert exc.value.status_code == 413
