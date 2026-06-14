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


def test_native_feed_response_redacts_secret_fields_and_excludes_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_secret = "Gumbo@Kona1b"
    rows = [
        {
            **_rows()[0],
            "subcategory": None,
            "metadata": {"source": "test"},
            "quality_rating": 75,
            "content": f"operator note with root password {raw_secret}",
            "compressed_content": f"compressed root password {raw_secret}",
            "verbatim_content": f"verbatim root password {raw_secret}",
            "owner_id": "owner-1",
            "namespace": "default",
            "permission_mode": 644,
        },
        {
            **_rows()[1],
            "id": "vault_mem",
            "subcategory": None,
            "metadata": {"source": "vault-test"},
            "quality_rating": 75,
            "content": f"vault row root password {raw_secret}",
            "compressed_content": f"vault compressed root password {raw_secret}",
            "verbatim_content": f"vault verbatim root password {raw_secret}",
            "owner_id": "owner-1",
            "namespace": "vault",
            "permission_mode": 644,
        },
    ]

    seen_by_native: list[list[dict[str, object]]] = []

    def fake_native_serializer(native_rows):
        seen_by_native.append([dict(row) for row in native_rows])
        return native_bridge.pure_python_serialize_memory_rows(native_rows)

    monkeypatch.setattr(
        native_bridge,
        "_NATIVE_FEDERATION",
        types.SimpleNamespace(serialize_memory_rows=fake_native_serializer),
    )

    encoded = native_bridge.serialize_feed_response(rows, next_cursor=None, has_more=False)
    payload = json.loads(encoded)
    encoded_text = encoded.decode("utf-8")

    raw_secret_present = raw_secret in encoded_text
    redaction_present = "[REDACTED]" in encoded_text
    seen_by_native_text = json.dumps(seen_by_native, ensure_ascii=False, default=str)

    assert payload["memories"][0]["id"] == "mem_1"
    assert [memory["id"] for memory in payload["memories"]] == ["mem_1"]
    assert redaction_present is True
    assert raw_secret_present is False
    assert raw_secret not in payload["memories"][0]["content"]
    assert raw_secret not in payload["memories"][0]["compressed_content"]
    assert raw_secret not in payload["memories"][0]["verbatim_content"]
    assert "vault_mem" not in encoded_text
    assert seen_by_native and seen_by_native[0][0]["content"] == payload["memories"][0]["content"]
    assert "vault_mem" not in seen_by_native_text
    assert raw_secret not in seen_by_native_text
