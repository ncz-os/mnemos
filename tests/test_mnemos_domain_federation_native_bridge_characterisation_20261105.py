"""Characterisation tests for ``mnemos/domain/federation/native_bridge.py``.

These tests PIN today's behaviour of the module's public functions and of the
private helpers they delegate to. They are not a design proposal: if a value
changes intentionally, update the pin together with the change.

Everything here is deterministic and offline: the optional native extension
(``mnemos_native_search``) is replaced with fakes via ``monkeypatch.setattr``
on the module's ``_NATIVE_FEDERATION`` global, so neither a present nor an
absent compiled extension changes the outcome. No network, no clock, no sleep.

One defect is pinned with ``xfail(strict=True)``: ``serialize_memory_for_feed``
hands the caller's raw rows to the native extension without the vault filtering
and span redaction that ``serialize_memory_rows`` applies. See
``test_feed_native_path_drops_vault_rows_like_the_rows_path_does`` for the exact
reason. The corresponding rows-path contract is pinned as a passing test.
"""

from __future__ import annotations

import ast
import json
import types
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.domain.federation import native_bridge


CREATED = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
UPDATED = datetime(2026, 5, 28, 12, 1, tzinfo=timezone.utc)
SECRET = "the root password is Hunter2Secure!"

# The exact field order ``_memory_item_payload`` emits (plus the
# ``federation_sequence`` key ``_wire_payload`` appends on top).
MEMORY_ITEM_KEYS = [
    "id",
    "content",
    "category",
    "subcategory",
    "created",
    "updated",
    "metadata",
    "quality_rating",
    "compressed_content",
    "verbatim_content",
    "source",
    "owner_id",
    "group_id",
    "namespace",
    "permission_mode",
    "source_model",
    "source_provider",
    "source_session",
    "source_agent",
    "archived_at",
    "archived",
    "embedding",
    "embedding_model",
    "embedding_dim",
    "audit_latest_entry_id",
    "audit_latest_entry_hash",
]


def _feed_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "mem_1",
        "content": "hello",
        "category": "projects",
        "tags": ["rust", "federation"],
        "embedding": [0.1, 0.2, 0.3],
        "refs": ["mem_0"],
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    row.update(overrides)
    return row


def _memory_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "mem_1",
        "content": "hello",
        "category": "projects",
        "created": CREATED,
        "updated": UPDATED,
        "namespace": "default",
    }
    row.update(overrides)
    return row


def _fake_native(*, feed=None, rows=None) -> types.SimpleNamespace:
    """A stand-in for ``mnemos_native_search`` with recording callables."""

    def _feed(payload):
        if callable(feed):
            return feed(payload)
        return ["native-feed"]

    def _rows(payload):
        if callable(rows):
            return rows(payload)
        return b"native-rows"

    return types.SimpleNamespace(
        serialize_memory_for_feed=_feed,
        serialize_memory_rows=_rows,
    )


# --------------------------------------------------------------------------
# module-level state
# --------------------------------------------------------------------------


def test_native_availability_flag_is_a_bool() -> None:
    assert isinstance(native_bridge.NATIVE_AVAILABLE, bool)


def test_native_availability_flag_is_a_static_snapshot(monkeypatch) -> None:
    # Replacing the global does not recompute the flag: it was fixed at import.
    before = native_bridge.NATIVE_AVAILABLE
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native())
    assert native_bridge.NATIVE_AVAILABLE is before


def test_module_imports_the_extension_under_a_private_alias() -> None:
    source = Path(native_bridge.__file__).read_text(encoding="utf-8")
    imported = {
        alias.name: alias.asname
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported.get("mnemos_native_search") == "_NATIVE_FEDERATION"


# --------------------------------------------------------------------------
# _iso_value
# --------------------------------------------------------------------------


def test_iso_value_returns_none_for_none() -> None:
    assert native_bridge._iso_value(None) is None


def test_iso_value_calls_a_callable_isoformat() -> None:
    assert native_bridge._iso_value(CREATED) == "2026-05-28T12:00:00+00:00"


def test_iso_value_returns_plain_strings_unchanged() -> None:
    assert native_bridge._iso_value("already-a-string") == "already-a-string"


def test_iso_value_returns_non_isoformattable_values_unchanged() -> None:
    marker = ["not", "a", "date"]
    assert native_bridge._iso_value(marker) is marker
    assert native_bridge._iso_value(0) == 0
    assert native_bridge._iso_value(False) is False


def test_iso_value_propagates_an_exception_raised_by_isoformat() -> None:
    class Bad:
        def isoformat(self) -> str:
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        native_bridge._iso_value(Bad())


# --------------------------------------------------------------------------
# _first_present / _first_optional
# --------------------------------------------------------------------------


def test_first_present_returns_the_value_of_the_first_existing_key() -> None:
    assert native_bridge._first_present({"a": 1, "b": 2}, "b", "a") == 2


def test_first_present_skips_keys_whose_value_is_none() -> None:
    assert native_bridge._first_present({"a": None, "b": 0}, "a", "b") == 0


def test_first_present_raises_key_error_named_after_the_first_key() -> None:
    with pytest.raises(KeyError) as excinfo:
        native_bridge._first_present({"only": 1}, "missing", "also_missing")
    assert excinfo.value.args == ("missing",)


def test_first_present_with_no_keys_raises_index_error() -> None:
    with pytest.raises(IndexError):
        native_bridge._first_present({"a": 1})


def test_first_optional_defaults_to_none() -> None:
    assert native_bridge._first_optional({"a": None}, "a") is None


def test_first_optional_returns_the_supplied_default() -> None:
    assert native_bridge._first_optional({}, "a", default="fallback") == "fallback"


def test_first_optional_treats_zero_and_empty_values_as_present() -> None:
    assert native_bridge._first_optional({"a": None, "b": 0}, "a", "b", default="x") == 0
    assert native_bridge._first_optional({"a": ""}, "a", default="x") == ""


def test_first_optional_with_no_keys_returns_the_default() -> None:
    assert native_bridge._first_optional({}, default="d") == "d"


# --------------------------------------------------------------------------
# _feed_payload -- the compact feed row shape
# --------------------------------------------------------------------------


def test_feed_payload_emits_the_compact_field_order() -> None:
    payload = native_bridge._feed_payload(_feed_row())
    assert list(payload) == ["id", "content", "category", "tags", "embedding", "refs", "created_at", "updated_at"]
    assert payload["refs"] == ["mem_0"]
    assert payload["created_at"] == "2026-05-28T12:00:00+00:00"
    assert payload["updated_at"] == "2026-05-28T12:01:00+00:00"


def test_feed_payload_omits_embedding_when_absent_or_none() -> None:
    assert "embedding" not in native_bridge._feed_payload(_feed_row(embedding=None))
    row = _feed_row()
    del row["embedding"]
    assert "embedding" not in native_bridge._feed_payload(row)


def test_feed_payload_defaults_tags_to_an_empty_list() -> None:
    assert native_bridge._feed_payload(_feed_row(tags=None))["tags"] == []
    row = _feed_row()
    del row["tags"]
    assert native_bridge._feed_payload(row)["tags"] == []


def test_feed_payload_passes_explicit_tags_through_untouched() -> None:
    tags = ["only-one"]
    assert native_bridge._feed_payload(_feed_row(tags=tags))["tags"] is tags


def test_feed_payload_prefers_refs_over_the_legacy_aliases() -> None:
    payload = native_bridge._feed_payload(_feed_row(refs=["a"], source_memory_ids=["b"], memory_refs=["c"]))
    assert payload["refs"] == ["a"]


def test_feed_payload_falls_back_to_source_memory_ids_then_memory_refs() -> None:
    alias = _feed_row(source_memory_ids=["s"], memory_refs=["ignored"])
    del alias["refs"]
    assert native_bridge._feed_payload(alias)["refs"] == ["s"]

    legacy = _feed_row(memory_refs=["m"])
    del legacy["refs"]
    assert native_bridge._feed_payload(legacy)["refs"] == ["m"]


def test_feed_payload_defaults_refs_to_an_empty_list() -> None:
    row = _feed_row()
    del row["refs"]
    assert native_bridge._feed_payload(row)["refs"] == []


def test_feed_payload_accepts_short_created_updated_aliases() -> None:
    row = {"id": "m", "content": "c", "category": "k", "created": "c-iso", "updated": "u-iso"}
    payload = native_bridge._feed_payload(row)
    assert payload["created_at"] == "c-iso"
    assert payload["updated_at"] == "u-iso"


@pytest.mark.parametrize("missing", ["id", "content", "category"])
def test_feed_payload_requires_id_content_and_category(missing: str) -> None:
    row = _feed_row()
    del row[missing]
    with pytest.raises(KeyError) as excinfo:
        native_bridge._feed_payload(row)
    assert excinfo.value.args == (missing,)


def test_feed_payload_requires_created_at_key_present() -> None:
    # A key that is absent raises; a key present but None is accepted.
    row = _feed_row()
    del row["created_at"]
    with pytest.raises(KeyError) as excinfo:
        native_bridge._feed_payload(row)
    assert excinfo.value.args == ("created_at",)


@pytest.mark.parametrize("null_key", ["created_at", "updated_at"])
def test_feed_payload_rejects_null_timestamps(null_key: str) -> None:
    # _first_present skips None, so a present-but-null timestamp is the same
    # KeyError as an absent one. Only the non-null alias would rescue the row.
    row = {"id": "m", "content": "c", "category": "k", "created_at": "c", "updated_at": "u"}
    row[null_key] = None
    with pytest.raises(KeyError) as excinfo:
        native_bridge._feed_payload(row)
    assert excinfo.value.args == (null_key,)


def test_feed_payload_requires_updated_at_key_present() -> None:
    # Same contract as created_at: absent (or null) raises with the first key name.
    row = _feed_row()
    del row["updated_at"]
    with pytest.raises(KeyError) as excinfo:
        native_bridge._feed_payload(row)
    assert excinfo.value.args == ("updated_at",)


def test_feed_payload_ignores_unrelated_columns() -> None:
    payload = native_bridge._feed_payload(_feed_row(extra="ignored", namespace="default"))
    assert "extra" not in payload
    assert "namespace" not in payload


# --------------------------------------------------------------------------
# _redact_field / _is_vault_row / _redacted_row
# --------------------------------------------------------------------------


def test_redact_field_returns_none_for_none() -> None:
    assert native_bridge._redact_field(None) is None


def test_redact_field_masks_a_credential_span() -> None:
    assert native_bridge._redact_field(SECRET) == "the root password is [REDACTED]"


def test_redact_field_leaves_clean_content_untouched() -> None:
    assert native_bridge._redact_field("plain project notes") == "plain project notes"


def test_redact_field_stringifies_non_string_values() -> None:
    assert native_bridge._redact_field(42) == "42"


def test_redact_field_turns_empty_string_into_empty_string() -> None:
    assert native_bridge._redact_field("") == ""


@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        (VAULT_NAMESPACE, True),
        ("vault", True),
        ("VAULT", False),
        ("default", False),
        ("", False),
        (None, False),
    ],
)
def test_is_vault_row_matches_the_vault_namespace_exactly(namespace: Any, expected: bool) -> None:
    assert native_bridge._is_vault_row({"namespace": namespace}) is expected


def test_is_vault_row_treats_a_missing_namespace_as_not_vault() -> None:
    assert native_bridge._is_vault_row({}) is False


def test_redacted_row_returns_a_shallow_copy_and_leaves_the_input_alone() -> None:
    embedding = [0.1, 0.2]
    row = _memory_row(content="clean", embedding=embedding)
    out = native_bridge._redacted_row(row)
    assert out == row
    assert out is not row
    assert out["embedding"] is embedding


def test_redacted_row_redacts_every_content_bearing_field() -> None:
    row = _memory_row(content=SECRET, compressed_content=SECRET, verbatim_content=SECRET)
    out = native_bridge._redacted_row(row)
    assert out["content"] == "the root password is [REDACTED]"
    assert out["compressed_content"] == "the root password is [REDACTED]"
    assert out["verbatim_content"] == "the root password is [REDACTED]"


def test_redacted_row_redacts_fields_whose_value_is_none_to_none() -> None:
    out = native_bridge._redacted_row({"content": None, "compressed_content": None})
    assert out == {"content": None, "compressed_content": None}


def test_redacted_row_skips_columns_that_are_absent() -> None:
    assert native_bridge._redacted_row({"id": "a"}) == {"id": "a"}


@pytest.mark.parametrize("tombstone_type", ["consolidation", "withdrawal"])
def test_redacted_row_skips_redaction_for_tombstone_rows(tombstone_type: str) -> None:
    row = {"type": tombstone_type, "id": "a", "content": SECRET}
    out = native_bridge._redacted_row(row)
    assert out["content"] == SECRET
    assert out is not row


def test_redacted_row_still_redacts_unknown_type_values() -> None:
    row = {"type": "memory", "content": SECRET}
    assert native_bridge._redacted_row(row)["content"] == "the root password is [REDACTED]"


def test_redacted_non_vault_rows_drops_vault_rows() -> None:
    rows = [
        _memory_row(id="keep", namespace="default"),
        _memory_row(id="drop", namespace=VAULT_NAMESPACE),
        _memory_row(id="keep-null", namespace=None),
    ]
    assert [row["id"] for row in native_bridge._redacted_non_vault_rows(rows)] == ["keep", "keep-null"]


def test_redacted_non_vault_rows_of_an_empty_sequence_is_empty() -> None:
    assert native_bridge._redacted_non_vault_rows([]) == []


# --------------------------------------------------------------------------
# _memory_item_payload -- the full memory wire row
# --------------------------------------------------------------------------


def test_memory_item_payload_emits_every_key_in_order() -> None:
    payload = native_bridge._memory_item_payload(_memory_row())
    assert list(payload) == MEMORY_ITEM_KEYS


def test_memory_item_payload_fills_defaults_for_a_minimal_row() -> None:
    payload = native_bridge._memory_item_payload({"id": "m", "content": "c", "category": "k", "created": "t"})
    assert payload["created"] == "t"
    assert payload["source"] == "openclaw"
    assert payload["metadata"] is None
    assert payload["archived"] is False
    assert payload["archived_at"] is None
    for key in ("subcategory", "updated", "quality_rating", "owner_id", "embedding"):
        assert payload[key] is None


def test_memory_item_payload_requires_id_content_and_category() -> None:
    with pytest.raises(KeyError) as excinfo:
        native_bridge._memory_item_payload({"created": "t"})
    assert excinfo.value.args == ("id",)


def test_memory_item_payload_prefers_created_over_created_at() -> None:
    payload = native_bridge._memory_item_payload(_memory_row(created="short", created_at="long"))
    assert payload["created"] == "short"


def test_memory_item_payload_falls_back_to_created_at() -> None:
    row = _memory_row(created_at="long")
    del row["created"]
    assert native_bridge._memory_item_payload(row)["created"] == "long"


def test_memory_item_payload_coerces_an_empty_created_to_an_empty_string() -> None:
    assert native_bridge._memory_item_payload(_memory_row(created=""))["created"] == ""


def test_memory_item_payload_raises_for_a_null_created() -> None:
    # A present-but-null `created` is skipped by _first_present; the
    # `or ""` coercion only ever sees a falsy *string*.
    with pytest.raises(KeyError) as excinfo:
        native_bridge._memory_item_payload(_memory_row(created=None))
    assert excinfo.value.args == ("created",)


def test_memory_item_payload_requires_a_created_key() -> None:
    row = _memory_row()
    del row["created"]
    with pytest.raises(KeyError) as excinfo:
        native_bridge._memory_item_payload(row)
    assert excinfo.value.args == ("created",)


def test_memory_item_payload_prefers_updated_over_updated_at() -> None:
    payload = native_bridge._memory_item_payload(_memory_row(updated="short", updated_at="long"))
    assert payload["updated"] == "short"


def test_memory_item_payload_falls_back_to_updated_at() -> None:
    row = _memory_row(updated_at="long")
    del row["updated"]
    assert native_bridge._memory_item_payload(row)["updated"] == "long"


def test_memory_item_payload_leaves_a_missing_updated_as_none() -> None:
    row = _memory_row()
    del row["updated"]
    assert native_bridge._memory_item_payload(row)["updated"] is None


def test_memory_item_payload_marks_an_archived_row() -> None:
    payload = native_bridge._memory_item_payload(_memory_row(archived_at="2026-06-01T00:00:00"))
    assert payload["archived"] is True
    assert payload["archived_at"] == "2026-06-01T00:00:00"


def test_memory_item_payload_treats_an_empty_archived_at_as_archived() -> None:
    # archived-ness is decided by "is not None", not by truthiness.
    payload = native_bridge._memory_item_payload(_memory_row(archived_at=""))
    assert payload["archived"] is True


def test_memory_item_payload_isoformats_datetime_columns() -> None:
    payload = native_bridge._memory_item_payload(_memory_row(archived_at=CREATED))
    assert payload["created"] == "2026-05-28T12:00:00+00:00"
    assert payload["updated"] == "2026-05-28T12:01:00+00:00"
    assert payload["archived_at"] == "2026-05-28T12:00:00+00:00"


def test_memory_item_payload_decodes_json_string_metadata() -> None:
    payload = native_bridge._memory_item_payload(_memory_row(metadata='{"source": "test"}'))
    assert payload["metadata"] == {"source": "test"}


def test_memory_item_payload_maps_empty_string_metadata_to_none() -> None:
    assert native_bridge._memory_item_payload(_memory_row(metadata=""))["metadata"] is None


def test_memory_item_payload_raises_json_decode_error_for_malformed_metadata() -> None:
    with pytest.raises(json.JSONDecodeError) as excinfo:
        native_bridge._memory_item_payload(_memory_row(metadata="{not json"))
    assert excinfo.type is json.JSONDecodeError
    assert isinstance(excinfo.value, ValueError)


def test_memory_item_payload_copies_mapping_metadata() -> None:
    metadata = {"source": "test"}
    payload = native_bridge._memory_item_payload(_memory_row(metadata=metadata))
    assert payload["metadata"] == {"source": "test"}
    assert payload["metadata"] is not metadata


def test_memory_item_payload_copies_any_object_dict_accepts() -> None:
    # Not a Mapping: `dict(metadata)` only needs keys()/__getitem__ (or pairs).
    class Mappingish:
        def keys(self):
            return ["a"]

        def __getitem__(self, key):
            return 1

    assert not isinstance(Mappingish(), Mapping)
    assert native_bridge._memory_item_payload(_memory_row(metadata=Mappingish()))["metadata"] == {"a": 1}


def test_memory_item_payload_propagates_a_type_error_for_unconvertible_metadata() -> None:
    class Neither:
        pass

    with pytest.raises(TypeError):
        native_bridge._memory_item_payload(_memory_row(metadata=Neither()))


def test_memory_item_payload_passes_through_optional_columns() -> None:
    payload = native_bridge._memory_item_payload(
        _memory_row(
            subcategory="sub",
            quality_rating=75,
            owner_id="owner-1",
            group_id="group-1",
            permission_mode=644,
            source_model="m",
            source_provider="p",
            source_session="s",
            source_agent="a",
            embedding=[0.5],
            embedding_model="emb",
            embedding_dim=1,
            audit_latest_entry_id=7,
            audit_latest_entry_hash="hash",
        )
    )
    assert payload["subcategory"] == "sub"
    assert payload["quality_rating"] == 75
    assert payload["owner_id"] == "owner-1"
    assert payload["group_id"] == "group-1"
    assert payload["permission_mode"] == 644
    assert payload["source_model"] == "m"
    assert payload["source_provider"] == "p"
    assert payload["source_session"] == "s"
    assert payload["source_agent"] == "a"
    assert payload["embedding"] == [0.5]
    assert payload["embedding_model"] == "emb"
    assert payload["embedding_dim"] == 1
    assert payload["audit_latest_entry_id"] == 7
    assert payload["audit_latest_entry_hash"] == "hash"


# --------------------------------------------------------------------------
# _wire_payload -- tombstone shapes versus a plain memory row
# --------------------------------------------------------------------------


def test_wire_payload_emits_the_consolidation_shape() -> None:
    payload = native_bridge._wire_payload(
        {
            "type": "consolidation",
            "id": "mem_1",
            "consolidated_into": "mem_canonical",
            "consolidated_at": CREATED,
            "federation_sequence": 9,
            "content": "ignored for tombstones",
        }
    )
    assert payload == {
        "type": "consolidation",
        "id": "mem_1",
        "consolidated_into": "mem_canonical",
        "consolidated_at": "2026-05-28T12:00:00+00:00",
        "federation_sequence": 9,
    }


def test_wire_payload_coerces_a_null_consolidated_at_to_an_empty_string() -> None:
    payload = native_bridge._wire_payload(
        {"type": "consolidation", "id": "m", "consolidated_into": "c", "consolidated_at": None}
    )
    assert payload["consolidated_at"] == ""
    assert payload["federation_sequence"] is None


def test_wire_payload_requires_consolidated_into() -> None:
    with pytest.raises(KeyError) as excinfo:
        native_bridge._wire_payload({"type": "consolidation", "id": "m"})
    assert excinfo.value.args == ("consolidated_into",)


def test_wire_payload_requires_consolidated_at() -> None:
    with pytest.raises(KeyError) as excinfo:
        native_bridge._wire_payload({"type": "consolidation", "id": "m", "consolidated_into": "c"})
    assert excinfo.value.args == ("consolidated_at",)


def test_wire_payload_emits_the_withdrawal_shape() -> None:
    payload = native_bridge._wire_payload(
        {"type": "withdrawal", "id": "mem_1", "namespace": "default", "updated": CREATED, "federation_sequence": 3}
    )
    assert payload == {
        "type": "withdrawal",
        "id": "mem_1",
        "namespace": "default",
        "withdrawn_at": "2026-05-28T12:00:00+00:00",
        "reason": "ineligible",
        "federation_sequence": 3,
    }


def test_wire_payload_withdrawal_prefers_updated_over_created() -> None:
    payload = native_bridge._wire_payload(
        {"type": "withdrawal", "id": "m", "updated": "u-iso", "created": "c-iso"}
    )
    assert payload["withdrawn_at"] == "u-iso"


def test_wire_payload_withdrawal_falls_back_to_created() -> None:
    payload = native_bridge._wire_payload({"type": "withdrawal", "id": "m", "created": "c-iso"})
    assert payload["withdrawn_at"] == "c-iso"


def test_wire_payload_withdrawal_without_timestamps_gets_an_empty_string() -> None:
    payload = native_bridge._wire_payload({"type": "withdrawal", "id": "m"})
    assert payload["withdrawn_at"] == ""


@pytest.mark.parametrize("namespace", ["", None])
def test_wire_payload_withdrawal_blank_namespace_becomes_none(namespace: Any) -> None:
    payload = native_bridge._wire_payload({"type": "withdrawal", "id": "m", "namespace": namespace})
    assert payload["namespace"] is None


def test_wire_payload_withdrawal_keeps_a_named_namespace() -> None:
    payload = native_bridge._wire_payload({"type": "withdrawal", "id": "m", "namespace": "projects"})
    assert payload["namespace"] == "projects"


def test_wire_payload_requires_the_id_of_a_withdrawal() -> None:
    with pytest.raises(KeyError) as excinfo:
        native_bridge._wire_payload({"type": "withdrawal"})
    assert excinfo.value.args == ("id",)


def test_wire_payload_plain_row_is_a_memory_item_plus_sequence() -> None:
    payload = native_bridge._wire_payload(_memory_row(federation_sequence=11))
    assert list(payload) == [*MEMORY_ITEM_KEYS, "federation_sequence"]
    assert payload["federation_sequence"] == 11


def test_wire_payload_plain_row_without_sequence_has_none() -> None:
    assert native_bridge._wire_payload(_memory_row())["federation_sequence"] is None


def test_wire_payload_only_recognises_the_exact_tombstone_type_strings() -> None:
    payload = native_bridge._wire_payload(_memory_row(type="Consolidation"))
    assert "consolidated_into" not in payload
    assert payload["source"] == "openclaw"


# --------------------------------------------------------------------------
# pure_python_serialize_memory_for_feed
# --------------------------------------------------------------------------


def test_feed_serializer_emits_compact_json_lines() -> None:
    encoded = native_bridge.pure_python_serialize_memory_for_feed([_feed_row(tags=[], refs=[])])
    assert encoded == [
        '{"id":"mem_1","content":"hello","category":"projects","tags":[],"embedding":[0.1,0.2,0.3],'
        '"refs":[],"created_at":"2026-05-28T12:00:00+00:00","updated_at":"2026-05-28T12:01:00+00:00"}'
    ]


def test_feed_serializer_preserves_row_order_and_count() -> None:
    rows = [_feed_row(id="a"), _feed_row(id="b"), _feed_row(id="c")]
    encoded = native_bridge.pure_python_serialize_memory_for_feed(rows)
    assert [json.loads(item)["id"] for item in encoded] == ["a", "b", "c"]


def test_feed_serializer_of_an_empty_sequence_is_empty() -> None:
    assert native_bridge.pure_python_serialize_memory_for_feed([]) == []


def test_feed_serializer_keeps_non_ascii_unescaped() -> None:
    encoded = native_bridge.pure_python_serialize_memory_for_feed([_feed_row(id="ü", content="héllo 🦀")])
    assert "héllo 🦀" in encoded[0]
    assert "\\u" not in encoded[0]


def test_feed_serializer_does_not_redact_or_drop_vault_rows() -> None:
    # The compact feed shape is emitted verbatim: redaction and vault
    # exclusion belong to pure_python_serialize_memory_rows, not this path.
    encoded = native_bridge.pure_python_serialize_memory_for_feed([_feed_row(content=SECRET, namespace="vault")])
    assert SECRET in encoded[0]


def test_feed_serializer_propagates_a_missing_key() -> None:
    with pytest.raises(KeyError):
        native_bridge.pure_python_serialize_memory_for_feed([{"id": "m"}])


# --------------------------------------------------------------------------
# serialize_memory_for_feed -- native dispatch with pure-Python fallback
# --------------------------------------------------------------------------


def test_serialize_memory_for_feed_uses_the_native_result_when_available(monkeypatch) -> None:
    seen: list[Any] = []

    def feed(rows):
        seen.append(list(rows))
        return ["native-1", "native-2"]

    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(feed=feed))
    rows = [_feed_row()]
    assert native_bridge.serialize_memory_for_feed(rows) == ["native-1", "native-2"]
    assert len(seen) == 1
    assert seen[0][0]["id"] == "mem_1"
    assert seen[0][0]["content"] == "hello"


def test_serialize_memory_for_feed_coerces_the_native_result_to_a_list(monkeypatch) -> None:
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(feed=lambda _rows: ("a", "b")))
    assert native_bridge.serialize_memory_for_feed([_feed_row()]) == ["a", "b"]


def test_serialize_memory_for_feed_falls_back_when_native_reraises(monkeypatch) -> None:
    def boom(_rows):
        raise RuntimeError("synthetic native failure")

    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(feed=boom))
    rows = [_feed_row()]
    assert native_bridge.serialize_memory_for_feed(rows) == native_bridge.pure_python_serialize_memory_for_feed(rows)


def test_serialize_memory_for_feed_falls_back_when_no_native_extension(monkeypatch) -> None:
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", None)
    rows = [_feed_row()]
    assert native_bridge.serialize_memory_for_feed(rows) == native_bridge.pure_python_serialize_memory_for_feed(rows)


def test_serialize_memory_for_feed_of_an_empty_sequence_calls_native(monkeypatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr(
        native_bridge, "_NATIVE_FEDERATION", _fake_native(feed=lambda rows: called.append(rows) or ["native-empty"])
    )
    assert native_bridge.serialize_memory_for_feed([]) == ["native-empty"]
    assert called == [[]]


# --------------------------------------------------------------------------
# pure_python_serialize_memory_rows
# --------------------------------------------------------------------------


def test_memory_rows_serializer_returns_utf8_bytes() -> None:
    encoded = native_bridge.pure_python_serialize_memory_rows([_memory_row()])
    assert type(encoded) is bytes
    assert json.loads(encoded.decode("utf-8"))[0]["id"] == "mem_1"


def test_memory_rows_serializer_emits_the_wire_shape_with_sequence() -> None:
    payload = json.loads(native_bridge.pure_python_serialize_memory_rows([_memory_row(federation_sequence=5)]))
    assert list(payload[0]) == [*MEMORY_ITEM_KEYS, "federation_sequence"]
    assert payload[0]["federation_sequence"] == 5
    assert payload[0]["source"] == "openclaw"


def test_memory_rows_serializer_of_an_empty_sequence_is_an_empty_array() -> None:
    assert native_bridge.pure_python_serialize_memory_rows([]) == b"[]"


def test_memory_rows_serializer_drops_vault_rows() -> None:
    rows = [
        _memory_row(id="keep", namespace="default"),
        _memory_row(id="drop", namespace=VAULT_NAMESPACE),
    ]
    payload = json.loads(native_bridge.pure_python_serialize_memory_rows(rows))
    assert [row["id"] for row in payload] == ["keep"]


def test_memory_rows_serializer_redacts_content_bearing_fields() -> None:
    payload = json.loads(
        native_bridge.pure_python_serialize_memory_rows(
            [_memory_row(content=SECRET, compressed_content=SECRET, verbatim_content=SECRET)]
        )
    )
    assert payload[0]["content"] == "the root password is [REDACTED]"
    assert payload[0]["compressed_content"] == "the root password is [REDACTED]"
    assert payload[0]["verbatim_content"] == "the root password is [REDACTED]"


def test_memory_rows_serializer_emits_tombstones_without_live_content() -> None:
    rows = [
        {"type": "withdrawal", "id": "gone", "namespace": "default", "updated": "u"},
        {"type": "consolidation", "id": "old", "consolidated_into": "new", "consolidated_at": "c"},
    ]
    payload = json.loads(native_bridge.pure_python_serialize_memory_rows(rows))
    assert payload == [
        {
            "type": "withdrawal",
            "id": "gone",
            "namespace": "default",
            "withdrawn_at": "u",
            "reason": "ineligible",
            "federation_sequence": None,
        },
        {
            "type": "consolidation",
            "id": "old",
            "consolidated_into": "new",
            "consolidated_at": "c",
            "federation_sequence": None,
        },
    ]


def test_memory_rows_serializer_drops_a_vault_namespace_tombstone() -> None:
    # Tombstones skip redaction, but _is_vault_row still filters them out.
    row = {"type": "withdrawal", "id": "gone", "namespace": VAULT_NAMESPACE, "updated": "u"}
    assert native_bridge._wire_payload(row)["namespace"] == VAULT_NAMESPACE
    assert native_bridge.pure_python_serialize_memory_rows([row]) == b"[]"


def test_memory_rows_serializer_keeps_non_ascii_unescaped() -> None:
    encoded = native_bridge.pure_python_serialize_memory_rows([_memory_row(content="héllo 🦀")])
    assert b"h\xc3\xa9llo" in encoded
    assert b"\\u00e9" not in encoded
    assert json.loads(encoded.decode("utf-8"))[0]["content"] == "héllo 🦀"


def test_memory_rows_serializer_propagates_a_missing_created_key() -> None:
    with pytest.raises(KeyError):
        native_bridge.pure_python_serialize_memory_rows([{"id": "m", "content": "c", "category": "k"}])


# --------------------------------------------------------------------------
# serialize_memory_rows -- native dispatch gated on federation_sequence
# --------------------------------------------------------------------------


def test_serialize_memory_rows_uses_the_native_result_when_available(monkeypatch) -> None:
    seen: list[Any] = []

    def rows(payload):
        seen.append([dict(row) for row in payload])
        return b"native-bytes"

    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=rows))
    assert native_bridge.serialize_memory_rows([_memory_row()]) == b"native-bytes"
    assert seen and seen[0][0]["id"] == "mem_1"


def test_serialize_memory_rows_bypasses_native_when_a_sequence_is_present(monkeypatch) -> None:
    seen: list[Any] = []

    def rows(payload):
        seen.append(payload)
        return b"native-bytes"

    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=rows))
    row = _memory_row(federation_sequence=4)
    assert native_bridge.serialize_memory_rows([row]) == native_bridge.pure_python_serialize_memory_rows([row])
    assert seen == []


def test_serialize_memory_rows_falls_back_when_native_reraises(monkeypatch) -> None:
    def boom(_rows):
        raise RuntimeError("synthetic native failure")

    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=boom))
    rows = [_memory_row()]
    assert native_bridge.serialize_memory_rows(rows) == native_bridge.pure_python_serialize_memory_rows(rows)


def test_serialize_memory_rows_falls_back_when_no_native_extension(monkeypatch) -> None:
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", None)
    rows = [_memory_row()]
    assert native_bridge.serialize_memory_rows(rows) == native_bridge.pure_python_serialize_memory_rows(rows)


def test_serialize_memory_rows_of_an_empty_sequence_calls_native(monkeypatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=lambda rows: seen.append(rows)))
    assert native_bridge.serialize_memory_rows([]) == b"[]"
    assert seen == [[]]


def test_serialize_memory_rows_falls_back_when_native_returns_none(monkeypatch) -> None:
    # bytes(None) raises TypeError, which the bare except swallows.
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=lambda _rows: None))
    assert native_bridge.serialize_memory_rows([]) == b"[]"


def test_serialize_memory_rows_uses_the_native_result_for_an_empty_sequence(monkeypatch) -> None:
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=lambda _rows: b"[]"))
    assert native_bridge.serialize_memory_rows([]) == b"[]"


def test_serialize_memory_rows_coerces_the_native_result_to_bytes(monkeypatch) -> None:
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=lambda _rows: bytearray(b"abc")))
    assert native_bridge.serialize_memory_rows([_memory_row()]) == b"abc"


def test_serialize_memory_rows_redaction_happens_before_the_sequence_gate(monkeypatch) -> None:
    # A vault row carrying a sequence is dropped first, so it must not
    # force the pure-Python path for the remaining rows.
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=lambda _rows: b"native-bytes"))
    vault_row = _memory_row(id="vault_mem", namespace=VAULT_NAMESPACE, federation_sequence=7)
    assert native_bridge.serialize_memory_rows([_memory_row(), vault_row]) == b"native-bytes"


def test_memory_rows_native_path_receives_only_redacted_non_vault_rows(monkeypatch) -> None:
    seen: list[Any] = []

    def rows(payload):
        seen.append([dict(row) for row in payload])
        return b"native-bytes"

    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=rows))
    native_bridge.serialize_memory_rows(
        [
            _memory_row(id="plain", content=SECRET),
            _memory_row(id="vault_mem", namespace=VAULT_NAMESPACE, content=SECRET),
        ]
    )
    assert [row["id"] for row in seen[0]] == ["plain"]
    assert seen[0][0]["content"] == "the root password is [REDACTED]"
    assert SECRET not in seen[0][0]["content"]


def test_feed_native_path_receives_the_callers_rows_untouched(monkeypatch) -> None:
    # The compact feed shape is a raw passthrough: no vault filtering and no
    # span redaction, on the native path just as on the pure-Python path.
    seen: list[Any] = []

    def feed(payload):
        seen.append([dict(row) for row in payload])
        return ["native-line"]

    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(feed=feed))
    rows = [
        _memory_row(id="plain", content=SECRET),
        _memory_row(id="vault_mem", namespace=VAULT_NAMESPACE, content=SECRET),
    ]
    native_bridge.serialize_memory_for_feed(rows)
    assert [row["id"] for row in seen[0]] == ["plain", "vault_mem"]
    assert seen[0][0]["content"] == SECRET
    assert seen[0][1]["content"] == SECRET


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defect (native feed path only): serialize_memory_for_feed forwards the caller's "
        "raw `rows` to the native extension at line 168 -- no vault filtering and no span "
        "redaction -- whereas serialize_memory_rows filters and redacts at line 184 before "
        "calling out at line 187. Whenever a native extension is installed, every row "
        "(including vault-namespace rows) reaches native code unredacted, even a single row. "
        "Latent today: "
        "serialize_memory_for_feed has no non-test caller in the tree, and the one route that "
        "serializes memories (mnemos/api/routes/federation.py) uses serialize_feed_response, "
        "which goes through the redacting rows path."
    ),
)
def test_feed_native_path_drops_vault_rows_like_the_rows_path_does(monkeypatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        native_bridge,
        "_NATIVE_FEDERATION",
        _fake_native(feed=lambda payload: seen.append(list(payload)) or ["native-line"]),
    )
    rows = [
        _memory_row(id="plain", content=SECRET),
        _memory_row(id="vault_mem", namespace=VAULT_NAMESPACE, content=SECRET),
    ]
    native_bridge.serialize_memory_for_feed(rows)
    assert [row["id"] for row in seen[0]] == ["plain"]
    assert SECRET not in json.dumps(seen[0])


# --------------------------------------------------------------------------
# serialize_feed_response -- the envelope around serialize_memory_rows
# --------------------------------------------------------------------------


def test_feed_response_wraps_rows_next_cursor_and_has_more() -> None:
    encoded = native_bridge.serialize_feed_response([_memory_row(id="m1")], next_cursor="cur", has_more=True)
    payload = json.loads(encoded)
    assert list(payload) == ["memories", "next_cursor", "has_more"]
    assert [row["id"] for row in payload["memories"]] == ["m1"]
    assert payload["next_cursor"] == "cur"
    assert payload["has_more"] is True


def test_feed_response_with_no_cursor_serialises_null() -> None:
    encoded = native_bridge.serialize_feed_response([], next_cursor=None, has_more=False)
    assert encoded == b'{"memories":[],"next_cursor":null,"has_more":false}'


def test_feed_response_with_an_empty_cursor_keeps_the_empty_string() -> None:
    assert b'"next_cursor":""' in native_bridge.serialize_feed_response([], next_cursor="", has_more=False)


def test_feed_response_keeps_a_non_ascii_cursor_unescaped() -> None:
    encoded = native_bridge.serialize_feed_response([], next_cursor="ü", has_more=False)
    assert "ü".encode("utf-8") in encoded
    assert json.loads(encoded)["next_cursor"] == "ü"


@pytest.mark.parametrize(
    ("flag", "literal"),
    [(True, b"true"), (False, b"false"), (1, b"true"), (0, b"false"), ("yes", b"true"), ([], b"false"), (None, b"false")],
)
def test_feed_response_has_more_is_rendered_from_truthiness(flag: Any, literal: bytes) -> None:
    encoded = native_bridge.serialize_feed_response([], next_cursor=None, has_more=flag)
    assert encoded.endswith(b'"has_more":' + literal + b"}")


def test_feed_response_is_utf8_bytes() -> None:
    assert isinstance(native_bridge.serialize_feed_response([], next_cursor=None, has_more=False), bytes)


def test_feed_response_redacts_and_drops_vault_rows() -> None:
    rows = [
        _memory_row(id="plain", content=SECRET),
        _memory_row(id="vault_mem", namespace=VAULT_NAMESPACE, content=SECRET),
    ]
    encoded = native_bridge.serialize_feed_response(rows, next_cursor=None, has_more=False)
    assert b"vault_mem" not in encoded
    assert SECRET.encode() not in encoded
    assert b"[REDACTED]" in encoded


def test_feed_response_pure_python_envelope_matches_the_literal(monkeypatch) -> None:
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", None)
    encoded = native_bridge.serialize_feed_response(
        [_memory_row(id="m1", content="hello")], next_cursor=None, has_more=False
    )
    assert encoded.startswith(b'{"memories":[{"id":"m1","content":"hello","category":"projects",')
    assert encoded.endswith(b',"next_cursor":null,"has_more":false}')


def test_feed_response_uses_the_native_serializer_when_available(monkeypatch) -> None:
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", _fake_native(rows=lambda _rows: b'[{"native":1}]'))
    encoded = native_bridge.serialize_feed_response([_memory_row()], next_cursor="c", has_more=True)
    assert encoded == b'{"memories":[{"native":1}],"next_cursor":"c","has_more":true}'


def test_feed_response_propagates_a_missing_created_key(monkeypatch) -> None:
    monkeypatch.setattr(native_bridge, "_NATIVE_FEDERATION", None)
    with pytest.raises(KeyError):
        native_bridge.serialize_feed_response(
            [{"id": "m", "content": "c", "category": "k"}], next_cursor=None, has_more=False
        )
