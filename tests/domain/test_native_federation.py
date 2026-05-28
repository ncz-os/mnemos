from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import datetime, timezone

import pytest

from mnemos.domain.federation import native_bridge


def _rows():
    return [
        {
            "id": "mem_1",
            "content": "hello",
            "category": "projects",
            "tags": ["rust", "federation"],
            "embedding": [0.1, 0.2, 0.3],
            "refs": ["mem_0"],
            "created_at": datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 5, 28, 12, 1, tzinfo=timezone.utc),
        },
        {
            "id": "mem_2",
            "content": "fallback aliases",
            "category": "standards",
            "created": "2026-05-28T12:02:00+00:00",
            "updated": "2026-05-28T12:03:00+00:00",
            "source_memory_ids": ["mem_1"],
        },
    ]


def test_pure_python_serializer_field_order_and_defaults() -> None:
    encoded = native_bridge.pure_python_serialize_memory_for_feed([_rows()[1]])[0]
    assert encoded == (
        '{"id":"mem_2","content":"fallback aliases","category":"standards",'
        '"tags":[],"refs":["mem_1"],"created_at":"2026-05-28T12:02:00+00:00",'
        '"updated_at":"2026-05-28T12:03:00+00:00"}'
    )


def test_adapter_falls_back_when_native_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(
        serialize_memory_for_feed=lambda _rows: (_ for _ in ()).throw(RuntimeError("synthetic native failure"))
    )
    monkeypatch.setitem(sys.modules, "mnemos_native_search", fake)
    bridge = importlib.reload(native_bridge)

    assert bridge.serialize_memory_for_feed(_rows()) == bridge.pure_python_serialize_memory_for_feed(_rows())


def test_native_serializer_matches_python_reference() -> None:
    native = pytest.importorskip("mnemos_native_search")
    expected = native_bridge.pure_python_serialize_memory_for_feed(_rows())
    actual = native.serialize_memory_for_feed(_rows())

    assert actual == expected
    assert [json.loads(item) for item in actual] == [json.loads(item) for item in expected]
