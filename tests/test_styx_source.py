"""STYX source selection and the per-host guard."""

from __future__ import annotations

import io
import json
import socket
import tarfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from mnemos.tools.styx.config import SOURCE_HTTP, StyxConfig
from mnemos.tools.styx.errors import StyxConfigError, StyxError
from mnemos.tools.styx.source import (
    _HTTP_EXPORT_PAGE_LIMIT,
    _STREAM_CHUNK,
    _is_loopback,
    archive_bundle,
    produce_bundle,
)


class _LogCapture:
    """Minimal context-free log record grabber for source-level WARNINGs."""

    def __init__(self, logger_name: str):
        import logging

        self._records: list = []
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self._records.append(record)
        self._logger = logging.getLogger(logger_name)
        self._old_level = self._logger.level

    def start(self):
        import logging

        self._logger.addHandler(self._handler)
        if self._logger.level == logging.NOTSET or self._logger.level > logging.WARNING:
            self._logger.setLevel(logging.WARNING)

    def stop(self):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._old_level)
        return self._records


def _capture_logs(logger_name: str) -> _LogCapture:
    cap = _LogCapture(logger_name)
    cap.start()
    return cap


RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3n0qmt"


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("http://127.0.0.1:5002/v1/export", True),
        ("http://localhost:5002/v1/export", True),
        ("http://[::1]:5002/v1/export", True),
        ("http://192.0.2.67:5002/v1/export", False),
        ("https://mnemos.example.com/v1/export", False),
    ],
)
def test_loopback_detection(endpoint, expected):
    assert _is_loopback(endpoint) is expected


@pytest.mark.asyncio
async def test_http_source_refuses_a_remote_endpoint(tmp_path):
    """STYX is per-host. Pointing it at a peer would quietly recreate the
    centralised puller the design rejected, so it is refused by default."""
    config = StyxConfig(
        gdrive_folder_id="f",
        age_recipient=RECIPIENT,
        source_mode=SOURCE_HTTP,
        http_endpoint="http://192.0.2.67:5002/v1/export",
        work_dir=tmp_path,
    )
    with pytest.raises(StyxConfigError, match="per-host by design"):
        await produce_bundle(config, tmp_path / "bundle")


@pytest.mark.asyncio
async def test_backend_source_refuses_a_missing_store(tmp_path):
    config = StyxConfig(
        gdrive_folder_id="f",
        age_recipient=RECIPIENT,
        sqlite_path=tmp_path / "absent.sqlite3",
        work_dir=tmp_path,
    )
    with pytest.raises(StyxConfigError, match="no MNEMOS store"):
        await produce_bundle(config, tmp_path / "bundle")


def test_archive_bundle_is_relative_and_ordered(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "semantic").mkdir(parents=True)
    (bundle / "mif-manifest.json").write_text("{}")
    (bundle / "semantic" / "a.md").write_text("alpha")

    archive = archive_bundle(bundle, tmp_path / "out.tar.gz")

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert names == sorted(names), "entries should be deterministically ordered"
    assert not any(Path(n).is_absolute() or n.startswith("..") for n in names)
    assert "mif-manifest.json" in names
    assert "semantic/a.md" in names


def _http_config(tmp_path: Path, endpoint: str) -> StyxConfig:
    return StyxConfig(
        gdrive_folder_id="f",
        age_recipient=RECIPIENT,
        source_mode=SOURCE_HTTP,
        http_endpoint=endpoint,
        http_token="root-token",
        work_dir=tmp_path,
    )


@contextmanager
def _serve_streaming_app(app):
    """Run a real HTTP/1.1 uvicorn server on an ephemeral loopback port."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("uvicorn test server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "uvicorn test server did not stop"


@pytest.mark.asyncio
async def test_http_source_reads_large_chunked_multipage_stream_completely(tmp_path):
    """Count records, not frames, across a realistic large chunked response."""
    expected_count = 12_345
    padding = "x" * 2048
    observed_query: dict[str, str] = {}
    observed_auth: list[str | None] = []
    app = FastAPI()

    @app.get("/v1/export")
    async def export(request: Request):
        observed_query.update(request.query_params)
        observed_auth.append(request.headers.get("authorization"))
        page_size = int(request.query_params["limit"])

        async def body():
            for start in range(0, expected_count, page_size):
                stop = min(start + page_size, expected_count)
                records = [{"id": f"mem_{number}", "payload": {"content": padding}} for number in range(start, stop)]
                yield json.dumps({"records": records}, separators=(",", ":")) + "\n"
                if start == 0:
                    yield b""
            yield json.dumps({"export_complete": True, "record_count": expected_count}) + "\n"

        return StreamingResponse(body(), media_type="application/x-ndjson")

    with _serve_streaming_app(app) as base_url:
        config = _http_config(
            tmp_path,
            f"{base_url}/v1/export?limit=7&offset=99&category=projects",
        )
        result = await produce_bundle(config, tmp_path / "bundle")

    payload = result.path / "export.mpf.json"
    assert result.record_count == expected_count
    assert result.kind == "mpf-envelope"
    assert payload.stat().st_size > 20 * 1024 * 1024
    assert payload.stat().st_size > 80 * _STREAM_CHUNK
    exported = json.loads(payload.read_text())
    assert exported["record_count"] == expected_count
    assert len(exported["records"]) == expected_count
    assert not (result.path / "export.ndjson").exists()
    assert observed_query == {
        "limit": str(_HTTP_EXPORT_PAGE_LIMIT),
        "offset": "0",
        "category": "projects",
        "stream": "true",
        "stream_records": "false",
        "include_sidecars": "true",
        "include_secrets": "true",
        "mpf_version": "0.2",
    }
    assert observed_auth == ["Bearer root-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'{"records":[{"id":"mem_1"}]}\n', "before its completion marker"),
        (
            b'{"records":[{"id":"mem_1"}]}\n{"export_complete":true,"record_count":2}\n',
            "completion count does not match",
        ),
        (
            b'{"records":[{"id":"mem_1"}]}\n{"export_complete":true,"record_count":1}\n{"records":[]}\n',
            "after its completion marker",
        ),
    ],
)
async def test_http_source_rejects_incomplete_or_inconsistent_stream(monkeypatch, tmp_path, body, message):
    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/x-ndjson"}

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(body))
    bundle = tmp_path / "bundle"
    with pytest.raises(StyxError, match=message):
        await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), bundle)
    assert not (bundle / "export.ndjson").exists()


# ── Duplicate-identity defense (2026-10-03 keyset-boundary regression) ─────
#
# The server-side fix makes the cursor lossless; these tests pin the
# client-side backstop: a byte-identical duplicate at a page boundary is
# tolerated (unique-identity count, WARNING naming the id), while a
# duplicate with a DIFFERENT payload still fails hard.


def _dup_record(number, content="x" * 100):
    return {
        "id": f"mem_{number}",
        "kind": "memory",
        "payload": {
            "created": "2026-10-01T00:00:00",
            "content": content,
        },
    }


@pytest.mark.asyncio
async def test_http_source_tolerates_byte_identical_duplicate_at_page_boundary(monkeypatch, tmp_path):
    """Last record of page 1 re-emitted byte-identical as first of page 2:
    the backup proceeds, counting UNIQUE identities, and logs a WARNING
    naming the duplicated id (the completion marker declares the snapshot's
    unique count, matching the materialized envelope)."""
    page1 = [_dup_record(0), _dup_record(1)]
    page2 = [_dup_record(1), _dup_record(2)]  # mem_1 duplicated, byte-identical
    body = (
        json.dumps({"records": page1}, separators=(",", ":"))
        + "\n"
        + json.dumps({"records": page2}, separators=(",", ":"))
        + "\n"
        + json.dumps({"export_complete": True, "record_count": 3})
        + "\n"
    )

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/x-ndjson"}

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(body.encode()))
    captured = _capture_logs("mnemos.tools.styx.source")
    try:
        result = await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")
    finally:
        records = captured.stop()
    assert result.record_count == 3, "unique identities, not wire frames"
    exported = json.loads((result.path / "export.mpf.json").read_text())
    assert [r["id"] for r in exported["records"]] == ["mem_0", "mem_1", "mem_2"]
    warnings = [r for r in records if r.levelname == "WARNING" and "mem_1" in r.getMessage()]
    assert warnings, f"expected a WARNING naming mem_1, got: {[r.getMessage() for r in records]}"


@pytest.mark.asyncio
async def test_http_source_rejects_duplicate_identity_with_divergent_payload(monkeypatch, tmp_path):
    """Same id, DIFFERENT payload on the wire: corruption, not a pagination
    seam — the backup must fail hard."""
    page1 = [_dup_record(0), _dup_record(1, content="original")]
    page2 = [_dup_record(1, content="TAMPERED"), _dup_record(2)]
    body = (
        json.dumps({"records": page1}, separators=(",", ":"))
        + "\n"
        + json.dumps({"records": page2}, separators=(",", ":"))
        + "\n"
        + json.dumps({"export_complete": True, "record_count": 3})
        + "\n"
    )

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/x-ndjson"}

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(body.encode()))
    with pytest.raises(StyxError, match="DIFFERENT payload"):
        await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")
    assert not (tmp_path / "bundle" / "export.ndjson").exists()
    assert not (tmp_path / "bundle" / "export.mpf.json").exists()


@pytest.mark.asyncio
async def test_http_source_rejects_completion_count_matching_neither_unique_nor_wire(monkeypatch, tmp_path):
    """A marker that equals neither the unique identity count nor the
    received wire count is a truncated or inconsistent stream: fail."""
    page1 = [_dup_record(0), _dup_record(1)]
    page2 = [_dup_record(1), _dup_record(2)]
    body = (
        json.dumps({"records": page1}, separators=(",", ":"))
        + "\n"
        + json.dumps({"records": page2}, separators=(",", ":"))
        + "\n"
        + json.dumps({"export_complete": True, "record_count": 5})  # neither 3 nor 4
        + "\n"
    )

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/x-ndjson"}

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(body.encode()))
    with pytest.raises(StyxError, match="completion count does not match unique"):
        await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")
    assert not (tmp_path / "bundle" / "export.mpf.json").exists()


@pytest.mark.asyncio
async def test_http_source_accepts_deployed_server_wire_count_with_seam_duplicates(monkeypatch, tmp_path):
    """Regression for the 2026-11-01..03 PYTHIA STYX failures
    (``materialized MPF record count does not match validated stream
    (28415 != 28418)``). The deployed server counts every frame it sends,
    seam re-emissions included (stream_export summed len(records)); a live
    read-only export on 2026-11-03 showed marker 28455 = wire 28455, unique
    28452. The validator and the materializer must count the SAME set (unique
    identities) and accept a wire-count marker when every repeat is
    byte-identical, so the backup succeeds with all unique records."""
    page1 = [_dup_record(0), _dup_record(1)]
    page2 = [_dup_record(1), _dup_record(2), _dup_record(3)]
    page3 = [_dup_record(2), _dup_record(3), _dup_record(4)]  # two-row seam overlap
    body = "".join(json.dumps({"records": page}, separators=(",", ":")) + "\n" for page in (page1, page2, page3)) + (
        json.dumps({"export_complete": True, "record_count": 8}) + "\n"
    )  # wire count

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/x-ndjson"}

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(body.encode()))
    result = await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")
    assert result.record_count == 5
    exported = json.loads((result.path / "export.mpf.json").read_text())
    assert exported["record_count"] == 5
    assert [r["id"] for r in exported["records"]] == [f"mem_{i}" for i in range(5)]
