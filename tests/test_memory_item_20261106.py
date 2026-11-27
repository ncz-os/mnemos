"""FB444 — memory item validation for ``mnemos.domain.models.MemoryItem``.

Pins the model on ``master`` (``22a3f7d8``): four required fields, 32 documented
fields with documented defaults, ``extra="ignore"`` for forward compatibility,
lossless serialisation, a fresh ``tags`` list per instance and a ``StrictInt``
permission mode.

Pinned divergences, not endorsed: ``MemoryItem.tags`` is NOT passed through
``_normalize_memory_tags`` (blank, padded and case-variant tags are stored
as-is even though the request models refuse them), and ``content`` has no
non-empty check, so ``""`` is a valid memory.

Pure model construction: no backend, no I/O.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mnemos.domain.models import MemoryItem

REQUIRED = {"id": "mem_1", "content": "hello", "category": "facts", "created": "2026-11-06T12:00:00"}

DOCUMENTED_FIELDS = {
    "id",
    "content",
    "category",
    "subcategory",
    "tags",
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
    "embedding_status",
    "federation_sequence",
    "audit_latest_entry_id",
    "audit_latest_entry_hash",
    "score",
    "superseded_by",
    "vaulted",
}


def _item(**over):
    return MemoryItem(**{**REQUIRED, **over})


def test_minimal_item_validates():
    item = _item()
    assert item.id == "mem_1"
    assert item.content == "hello"
    assert item.category == "facts"


def test_schema_has_exactly_the_documented_fields():
    assert set(MemoryItem.model_fields) == DOCUMENTED_FIELDS


def test_defaults_for_a_minimal_item():
    item = _item()
    assert item.subcategory is None
    assert item.tags == []
    assert item.updated is None
    assert item.metadata is None
    assert item.quality_rating is None
    assert item.compressed_content is None
    assert item.verbatim_content is None
    assert item.source == "openclaw"
    assert item.owner_id is None
    assert item.group_id is None
    assert item.namespace is None
    assert item.permission_mode is None
    assert item.archived_at is None
    assert item.archived is False
    assert item.embedding is None
    assert item.embedding_status is None
    assert item.score is None
    assert item.superseded_by is None
    assert item.vaulted is False


def test_source_explicit_none_is_allowed():
    assert _item(source=None).source is None


def test_extra_configuration_is_ignore():
    assert MemoryItem.model_config.get("extra") == "ignore"


@pytest.mark.parametrize("missing", ["id", "content", "category", "created"])
def test_missing_required_field_is_named(missing):
    payload = {k: v for k, v in REQUIRED.items() if k != missing}
    with pytest.raises(ValidationError) as caught:
        MemoryItem(**payload)
    errors = caught.value.errors()
    assert any(err["loc"] == (missing,) for err in errors)
    assert missing in str(caught.value)


def test_unknown_string_field_is_ignored():
    item = _item(future_field="from a newer peer")
    assert not hasattr(item, "future_field")
    assert "future_field" not in item.model_dump()


def test_non_string_id_is_refused():
    with pytest.raises(ValidationError):
        _item(id=5)


def test_empty_content_is_a_valid_item():
    """Pinned divergence: there is no non-empty check on content."""
    assert _item(content="").content == ""


def test_tags_are_not_normalised():
    """Pinned divergence: item tags bypass ``_normalize_memory_tags``."""
    tags = [" hive ", "hive", "", "Hive"]
    assert _item(tags=tags).tags == tags


def test_tags_default_is_a_fresh_list_per_instance():
    first = _item()
    second = _item()
    first.tags.append("hive")
    assert second.tags == []


def test_metadata_is_preserved_verbatim():
    metadata = {"labels": ["a", "b"], "provenance": {"peer": "h1", "seq": 3}, "n": 1.5}
    assert _item(metadata=metadata).metadata == metadata


def test_permission_mode_accepts_an_int():
    assert _item(permission_mode=600).permission_mode == 600


@pytest.mark.parametrize("value", [True, "600", 600.0])
def test_permission_mode_is_strict(value):
    with pytest.raises(ValidationError):
        _item(permission_mode=value)


def test_score_accepts_int_and_float():
    assert _item(score=1).score == 1
    assert _item(score=0.25).score == 0.25


def test_archived_and_vaulted_are_booleans():
    assert _item(archived=True, vaulted=True).archived is True
    assert _item(archived=True, vaulted=True).vaulted is True


def test_model_dump_round_trips():
    item = _item(
        tags=["hive"],
        metadata={"k": "v"},
        quality_rating=4,
        archived=True,
        embedding_status="ready",
        embedding_dim=1024,
        score=0.5,
    )
    assert MemoryItem(**item.model_dump()) == item


def test_json_round_trip_is_lossless():
    item = _item(tags=["hive", "repos"], metadata={"k": [1, 2]}, score=0.25)
    assert MemoryItem.model_validate_json(item.model_dump_json()) == item
