"""Concurrency and route coverage without the optional Docling dependency."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mnemos.api.routes import document_import as di
from tests._charon_fake_backend import install_fake_backend


@pytest.fixture(autouse=True)
def reset_semaphore(monkeypatch):
    monkeypatch.setattr(di, "_docling_conversion_semaphore", asyncio.Semaphore(1))


class FakeImporter:
    def __init__(self, hold=None):
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.lock = threading.Lock()
        self.hold = hold

    def parse_document(self, content, filename):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.hold is not None:
                self.hold.wait(timeout=5)
            return "text", {"source_file": filename}, []
        finally:
            with self.lock:
                self.active -= 1


@pytest.mark.asyncio
async def test_route_conversion_runs_without_docling(client, auth_headers, monkeypatch):
    install_fake_backend(monkeypatch)
    monkeypatch.setattr(di, "DOCLING_AVAILABLE", True)
    monkeypatch.setattr(di, "_load_docling", lambda: None)
    monkeypatch.setattr(di, "DocumentStream", lambda **kwargs: kwargs)

    class FakeDoc:
        pages = []
        document = None

        def __init__(self):
            self.document = self

        def export_to_markdown(self):
            return "text"

    class FakeConverter:
        def convert(self, stream):
            return FakeDoc()

    monkeypatch.setattr(di, "DocumentConverter", FakeConverter)
    response = await client.post(
        "/v1/documents/import",
        files={"file": ("doc.md", b"hello")},
        data={"category": "documents", "project_tag": "mnemos"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["memory_ids"]


@pytest.mark.asyncio
async def test_admission_caps_conversions_without_occupying_waiter_threads(monkeypatch):
    monkeypatch.setattr(di, "_docling_conversion_semaphore", asyncio.Semaphore(2))
    hold = threading.Event()
    fake = FakeImporter(hold)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=3)
    loop.set_default_executor(executor)
    tasks = [asyncio.create_task(di._convert_document_off_loop(fake, b"x", "d.md")) for _ in range(5)]
    try:
        for _ in range(200):
            if fake.active == 2:
                break
            await asyncio.sleep(0.01)
        assert fake.active == 2
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), 1) == "available"
    finally:
        hold.set()
        await asyncio.gather(*tasks)
        executor.shutdown(wait=True)
    assert fake.calls == 5
    assert fake.max_active == 2


@pytest.mark.asyncio
async def test_slot_released_when_parse_raises():
    class Boom:
        def parse_document(self, content, filename):
            raise RuntimeError("parse failed")

    with pytest.raises(RuntimeError, match="parse failed"):
        await di._convert_document_off_loop(Boom(), b"x", "d.md")
    assert await di._convert_document_off_loop(FakeImporter(), b"x", "d.md")


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_consume_slot():
    hold = threading.Event()
    fake = FakeImporter(hold)
    active = asyncio.create_task(di._convert_document_off_loop(fake, b"x", "active.md"))
    waiter = None
    try:
        for _ in range(200):
            if fake.active:
                break
            await asyncio.sleep(0.01)
        assert fake.active == 1
        waiter = asyncio.create_task(di._convert_document_off_loop(fake, b"x", "waiting.md"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert fake.calls == 1
    finally:
        hold.set()
        await active
    await asyncio.wait_for(di._convert_document_off_loop(fake, b"x", "next.md"), 1)
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_cancelled_active_parse_retains_slot():
    hold = threading.Event()
    fake = FakeImporter(hold)
    active = asyncio.create_task(di._convert_document_off_loop(fake, b"x", "active.md"))
    next_task = None
    try:
        for _ in range(200):
            if fake.active:
                break
            await asyncio.sleep(0.01)
        assert fake.active == 1
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        next_task = asyncio.create_task(di._convert_document_off_loop(fake, b"x", "next.md"))
        await asyncio.sleep(0.05)
        assert fake.calls == 1
    finally:
        hold.set()
        if next_task:
            await asyncio.wait_for(next_task, 2)
    assert fake.max_active == 1


@pytest.mark.asyncio
async def test_conversions_create_separate_converters(monkeypatch):
    monkeypatch.setattr(di, "DOCLING_AVAILABLE", True)
    monkeypatch.setattr(di, "_load_docling", lambda: None)
    monkeypatch.setattr(di, "DocumentStream", lambda **kwargs: kwargs)
    built = []

    class Converter:
        def __init__(self):
            built.append(self)

        def convert(self, stream):
            return type("Doc", (), {"document": type("Content", (), {"export_to_markdown": lambda self: "text"})()})()

    monkeypatch.setattr(di, "DocumentConverter", Converter)
    await asyncio.gather(*(di._convert_document_off_loop(di.DoclingImporter(), b"x", "d.md") for _ in range(2)))
    assert len(built) == 2 and built[0] is not built[1]
