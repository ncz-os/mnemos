"""Federation feed payload shape (FB423, 2026-11-06).

Pins the two helpers behind the native federation feed lines:

* ``_feed_payload`` — the documented per-memory dict a feed line serialises;
* ``_redact_field`` — the credential span-redaction used by the row path.

Two divergences are pinned as current behaviour and reported: the timestamps are
REQUIRED (a row missing both aliases raises ``KeyError``) and ``_feed_payload``
does not redact at all, because redaction is applied in ``_redacted_row`` for the
row serializer only — ``pure_python_serialize_memory_for_feed`` never calls it.

Credential fixtures are synthetic; nothing here reads a real secret.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mnemos.domain.federation.native_bridge import (  # noqa: E402
    _feed_payload,
    _redact_field,
)

# Shaped like the detector's GitHub-token pattern, but synthetic.
FAKE_CREDENTIAL = "ghp_" + "fakefakefakefakefakefakefakefakefake"


def _row(**overrides):
    row = {
        "id": "mem_fb423",
        "content": "plain body",
        "category": "projects",
        "tags": ["a"],
        "created_at": datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 11, 6, 12, 30, 0, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


# ── _feed_payload: shape ───────────────────────────────────────────────────


def test_a_full_row_carries_exactly_the_documented_keys():
    payload = _feed_payload(_row(embedding=[0.1, 0.2], refs=["mem_other"]))
    assert payload == {
        "id": "mem_fb423",
        "content": "plain body",
        "category": "projects",
        "tags": ["a"],
        "embedding": [0.1, 0.2],
        "refs": ["mem_other"],
        "created_at": "2026-11-06T12:00:00+00:00",
        "updated_at": "2026-11-06T12:30:00+00:00",
    }


def test_key_order_is_stable_and_embedding_sits_before_refs():
    assert list(_feed_payload(_row(embedding=[0.0]))) == [
        "id",
        "content",
        "category",
        "tags",
        "embedding",
        "refs",
        "created_at",
        "updated_at",
    ]
    assert list(_feed_payload(_row())) == [
        "id",
        "content",
        "category",
        "tags",
        "refs",
        "created_at",
        "updated_at",
    ]


def test_extra_row_keys_do_not_leak_into_the_payload():
    payload = _feed_payload(_row(namespace="default", owner_id="o", permission_mode=600, archived=True))
    for leaked in ("namespace", "owner_id", "permission_mode", "archived"):
        assert leaked not in payload


def test_embedding_is_omitted_when_missing_or_none():
    assert "embedding" not in _feed_payload(_row())
    assert "embedding" not in _feed_payload(_row(embedding=None))
    marker = [0.5]
    assert _feed_payload(_row(embedding=marker))["embedding"] is marker


def test_tags_default_to_an_empty_list_and_pass_through_when_present():
    assert _feed_payload(_row(tags=None))["tags"] == []
    assert _feed_payload(_row(tags=[]))["tags"] == []
    tags = ["x", "y"]
    assert _feed_payload(_row(tags=tags))["tags"] is tags


def test_refs_default_to_an_empty_list():
    assert _feed_payload(_row())["refs"] == []


def test_refs_fall_back_through_the_documented_aliases():
    assert _feed_payload(_row(source_memory_ids=["s1"]))["refs"] == ["s1"]
    assert _feed_payload(_row(memory_refs=["m1"]))["refs"] == ["m1"]
    # the canonical key wins over both aliases
    assert _feed_payload(_row(refs=["r"], source_memory_ids=["s"], memory_refs=["m"]))["refs"] == ["r"]
    # and the first alias wins over the later one
    assert _feed_payload(_row(source_memory_ids=["s"], memory_refs=["m"]))["refs"] == ["s"]


def test_timestamps_use_the_created_and_updated_aliases():
    row = {
        "id": "mem_alias",
        "content": "body",
        "category": "c",
        "created": datetime(2026, 1, 2, 3, 4, 5),
        "updated": date(2026, 1, 3),
    }
    payload = _feed_payload(row)
    assert payload["created_at"] == "2026-01-02T03:04:05"
    assert payload["updated_at"] == "2026-01-03"


def test_a_missing_required_field_raises_naming_it():
    for field in ("id", "content", "category"):
        row = _row()
        del row[field]
        with pytest.raises(KeyError) as exc:
            _feed_payload(row)
        assert exc.value.args == (field,), field
    with pytest.raises(KeyError):
        _feed_payload(_row(content=None))


def test_a_missing_timestamp_is_required_not_omitted():
    # PIN: the brief expects a missing optional field to be omitted; the
    # timestamps are required, so the whole payload raises instead.
    row = _row()
    del row["created_at"]
    with pytest.raises(KeyError) as exc:
        _feed_payload(row)
    assert exc.value.args == ("created_at",)
    row = _row()
    del row["updated_at"]
    with pytest.raises(KeyError) as exc:
        _feed_payload(row)
    assert exc.value.args == ("updated_at",)


def test_payload_is_json_serialisable_with_no_datetime_objects():
    payload = _feed_payload(_row(embedding=[0.1]))
    encoded = json.dumps(payload)
    assert "2026-11-06T12:00:00" in encoded
    for value in payload.values():
        assert not isinstance(value, (datetime, date)), type(value)
    assert json.loads(encoded) == payload


# ── redaction: the divergence ──────────────────────────────────────────────


def test_feed_payload_does_not_redact_content():
    # PIN: redaction lives in _redacted_row (used by serialize_memory_rows), not
    # here, and the feed-line serializer calls this helper on raw rows — so a
    # credential-shaped span travels verbatim on that path.
    payload = _feed_payload(_row(content=f"token {FAKE_CREDENTIAL} end"))
    assert FAKE_CREDENTIAL in payload["content"]


def test_redact_field_masks_the_same_span():
    # Contrast with the pin above: the row path would redact it.
    out = _redact_field(f"token {FAKE_CREDENTIAL} end")
    assert FAKE_CREDENTIAL not in out
    assert "[REDACTED]" in out


def test_redact_field_passes_clean_text_and_none_through():
    assert _redact_field(None) is None
    assert _redact_field("") == ""
    assert _redact_field("nothing sensitive here") == "nothing sensitive here"


def test_redact_field_stringifies_non_string_values():
    assert _redact_field(5) == "5"
    assert _redact_field(["a", "b"]) == "['a', 'b']"
