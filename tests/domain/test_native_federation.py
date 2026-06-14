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


def test_pure_python_memory_rows_serializer_matches_feed_wire_shape() -> None:
    encoded = native_bridge.pure_python_serialize_memory_rows(
        [
            {
                **_rows()[0],
                "subcategory": None,
                "metadata": {"source": "test"},
                "quality_rating": 75,
                "verbatim_content": "hello",
                "owner_id": "owner-1",
                "namespace": "default",
                "permission_mode": 644,
            }
        ]
    )
    payload = json.loads(encoded)

    assert isinstance(encoded, bytes)
    assert payload[0]["id"] == "mem_1"
    assert payload[0]["created"] == "2026-05-28T12:00:00+00:00"
    assert payload[0]["metadata"] == {"source": "test"}
    assert payload[0]["source"] == "openclaw"
    assert payload[0]["archived"] is False


def test_adapter_falls_back_when_native_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(
        serialize_memory_for_feed=lambda _rows: (_ for _ in ()).throw(RuntimeError("synthetic native failure")),
        serialize_memory_rows=lambda _rows: (_ for _ in ()).throw(RuntimeError("synthetic native failure")),
    )
    monkeypatch.setitem(sys.modules, "mnemos_native_search", fake)
    bridge = importlib.reload(native_bridge)

    assert bridge.serialize_memory_for_feed(_rows()) == bridge.pure_python_serialize_memory_for_feed(_rows())
    assert bridge.serialize_memory_rows(_rows()) == bridge.pure_python_serialize_memory_rows(_rows())


def test_native_serializer_matches_python_reference() -> None:
    native = pytest.importorskip("mnemos_native_search")
    expected = native_bridge.pure_python_serialize_memory_for_feed(_rows())
    actual = native.serialize_memory_for_feed(_rows())

    assert actual == expected
    assert [json.loads(item) for item in actual] == [json.loads(item) for item in expected]


def test_native_memory_rows_serializer_matches_python_reference() -> None:
    native = pytest.importorskip("mnemos_native_search")
    rows = [
        {
            **_rows()[0],
            "subcategory": None,
            "metadata": {"source": "test"},
            "quality_rating": 75,
            "verbatim_content": "hello",
            "owner_id": "owner-1",
            "namespace": "default",
            "permission_mode": 644,
        }
    ]
    expected = native_bridge.pure_python_serialize_memory_rows(rows)
    actual = native.serialize_memory_rows(rows)

    assert isinstance(actual, bytes)
    assert json.loads(actual) == json.loads(expected)
