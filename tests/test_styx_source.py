"""STYX source selection and the per-host guard."""

from __future__ import annotations

import io
import json
import os
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

    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_args, **_kwargs: Response(body))
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

    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_args, **_kwargs: Response(body.encode()))
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
    dup_warnings = [r for r in records if r.levelname == "WARNING" and "byte-identical duplicate" in r.getMessage()]
    assert dup_warnings and "1 byte-identical duplicate" in dup_warnings[0].getMessage(), (
        f"expected the WARNING to report exactly 1 repeat sighting, got: "
        f"{[r.getMessage() for r in dup_warnings]}"
    )


@pytest.mark.asyncio
async def test_http_source_counts_every_repeat_sighting_not_just_first(monkeypatch, tmp_path):
    """The same id re-emitted byte-identical two more times is two repeat
    sightings (wire 5, unique 3): the WARNING total counts every repeat,
    while the id sample stays distinct."""
    page1 = [_dup_record(0), _dup_record(1)]
    page2 = [_dup_record(1), _dup_record(1), _dup_record(2)]  # mem_1 twice more, byte-identical
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

    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_args, **_kwargs: Response(body.encode()))
    captured = _capture_logs("mnemos.tools.styx.source")
    try:
        result = await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")
    finally:
        records = captured.stop()
    assert result.record_count == 3, "unique identities, not wire frames"
    dup_warnings = [r for r in records if r.levelname == "WARNING" and "byte-identical duplicate" in r.getMessage()]
    assert dup_warnings and "2 byte-identical duplicate" in dup_warnings[0].getMessage(), (
        f"expected the WARNING to report 2 repeat sightings, got: "
        f"{[r.getMessage() for r in dup_warnings]}"
    )


@pytest.mark.asyncio
async def test_http_source_flushes_spool_journal_in_batches(monkeypatch, tmp_path):
    """The commit_batch=1000 boundary is exercised with exactly the expected
    flush count: 1500 unique records produce ONE explicit batch commit inside
    the stream (at record 1000) plus ONE final commit when the spool context
    closes — no more, no less. A spool that never flushed mid-stream would
    emit only the final commit (len==1); one that flushed per-record would
    emit far more. This proves the journal is bounded by the batch size, not
    by corpus size."""
    import sqlite3 as _sqlite3

    real_connect = _sqlite3.connect
    commits: list[str] = []

    class _CountingConnection(_sqlite3.Connection):
        def commit(self):  # type: ignore[override]
            commits.append("commit")
            return super().commit()

    def _counting_connect(*args, **kwargs):
        kwargs.setdefault("factory", _CountingConnection)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(_sqlite3, "connect", _counting_connect)
    records = [_dup_record(i, content=f"payload-{i:04d}") for i in range(1500)]
    body = (
        json.dumps({"records": records}, separators=(",", ":"))
        + "\n"
        + json.dumps({"export_complete": True, "record_count": 1500})
        + "\n"
    )

    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/x-ndjson"}

    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_args, **_kwargs: Response(body.encode()))
    result = await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")
    assert result.record_count == 1500
    # commit_batch=1000: flush once at record 1000, then once more when the
    # context manager closes the spool. Plus the initial table-creation
    # commit inside _open(). Exactly three commits — no per-record flushes,
    # no missing mid-stream flush.
    assert len(commits) == 3, f"expected exactly 3 spool commits (1 table + 1 batch + 1 final), got {len(commits)}"
    spool_files = list(tmp_path.rglob(".mnemos-styx-validate-*"))
    assert not spool_files, f"spool directory leaked: {spool_files}"


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

    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_args, **_kwargs: Response(body.encode()))
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

    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_args, **_kwargs: Response(body.encode()))
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

    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_args, **_kwargs: Response(body.encode()))
    result = await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")
    assert result.record_count == 5
    exported = json.loads((result.path / "export.mpf.json").read_text())
    assert exported["record_count"] == 5
    assert [r["id"] for r in exported["records"]] == [f"mem_{i}" for i in range(5)]


# ── T-010: streaming validation fails closed without publishing artifacts ──


def _serve_body(body: bytes, monkeypatch):
    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/x-ndjson"}

    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_args, **_kwargs: Response(body))


@pytest.mark.asyncio
async def test_late_divergent_duplicate_fails_without_publishing_artifact(monkeypatch, tmp_path):
    """A divergence deep in the stream (page 1500 of 2000) must fail the
    backup and leave no wire capture, no materialized envelope, and no
    bundle metadata behind — including no leftover validator spool."""
    # Page i carries mem_{2i} and mem_{2i+1} plus a re-emission of the
    # previous page's last row mem_{2i-1} (seam overlap). Content is a
    # function of the page index, so the re-emitted boundary row at page
    # 1500 diverges from its first sighting on page 1499.
    def page(i):
        ids = [0, 1] if i == 0 else [2 * i - 1, 2 * i, 2 * i + 1]
        return [_dup_record(n, content="v0" if i < 1500 else "v1") for n in ids]

    pages = (page(i) for i in range(2000))
    body = "".join(
        json.dumps({"records": p}, separators=(",", ":")) + "\n" for p in pages
    ) + json.dumps({"export_complete": True, "record_count": 4000}) + "\n"
    _serve_body(body.encode(), monkeypatch)

    with pytest.raises(StyxError, match="DIFFERENT payload"):
        await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")

    assert not (tmp_path / "bundle" / "export.ndjson").exists()
    assert not (tmp_path / "bundle" / "export.mpf.json").exists()
    assert not (tmp_path / "bundle" / "styx-source.json").exists()
    leftovers = [entry for entry in os.listdir(tmp_path) if entry.startswith(".mnemos-styx-validate-")]
    assert not leftovers, f"validator spool leaked: {leftovers}"


@pytest.mark.asyncio
async def test_missing_completion_marker_fails_without_publishing_artifact(monkeypatch, tmp_path):
    """A truncated stream with a correct wire count still fails: the
    snapshot never committed, so no artifact may be published."""
    page1 = [_dup_record(0), _dup_record(1)]
    body = (
        json.dumps({"records": page1}, separators=(",", ":"))
        + "\n"
        + json.dumps({"export_complete": True, "record_count": 2})
        + "\n"
    )
    lines = body.splitlines(keepends=True)
    assert lines[-1].startswith('{"export_complete"')
    body = "".join(lines[:-1])  # the marker frame is never received
    _serve_body(body.encode(), monkeypatch)

    with pytest.raises(StyxError, match="before its completion marker"):
        await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")

    assert not (tmp_path / "bundle" / "export.mpf.json").exists()
    assert not (tmp_path / "bundle" / "styx-source.json").exists()


@pytest.mark.asyncio
async def test_identical_page_seam_duplicate_exports_once_with_disk_spool(monkeypatch, tmp_path):
    """Seam-guarded server: duplicate boundary rows are byte-identical, the
    marker declares the unique count, and the materialized envelope contains
    each record exactly once even though validation spooled state on disk."""
    page1 = [_dup_record(i) for i in range(10)]
    page2 = [_dup_record(9)] + [_dup_record(i) for i in range(10, 20)]  # seam overlap
    page3 = [_dup_record(19)] + [_dup_record(i) for i in range(20, 30)]
    body = "".join(
        json.dumps({"records": p}, separators=(",", ":")) + "\n" for p in (page1, page2, page3)
    ) + json.dumps({"export_complete": True, "record_count": 30}) + "\n"
    _serve_body(body.encode(), monkeypatch)
    captured = _capture_logs("mnemos.tools.styx.source")
    try:
        result = await produce_bundle(_http_config(tmp_path, "http://127.0.0.1:5002/v1/export"), tmp_path / "bundle")
    finally:
        records = captured.stop()

    assert result.record_count == 30
    exported = json.loads((result.path / "export.mpf.json").read_text())
    assert exported["record_count"] == 30
    assert [r["id"] for r in exported["records"]] == [f"mem_{i}" for i in range(30)]
    warnings = [r for r in records if r.levelname == "WARNING" and "mem_9" in r.getMessage()]
    assert warnings, f"expected seam WARNING naming mem_9, got: {[r.getMessage() for r in records]}"
