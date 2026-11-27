"""OpenAI-compatible content serialisation (FB416, 2026-11-06).

Pins the three content helpers in ``mnemos/domain/openai_compat/content.py``:

* ``_serialize_content`` turns a list of ``ContentBlock`` models into plain
  provider dicts (``exclude_none``) and leaves strings/other values untouched;
* ``_plain_value`` converts request fragments recursively, dropping None from
  dicts but keeping None list items;
* ``_content_text`` flattens a string or a content-block list to text, joining
  parts with a newline and rendering images as ``[image_url: <url>]``.

No request is made and no provider client is involved.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mnemos.domain.openai_compat.content import (  # noqa: E402
    _content_text,
    _plain_value,
    _serialize_content,
)
from mnemos.domain.openai_compat.schemas import ContentBlock  # noqa: E402

TEXT_BLOCK = ContentBlock(type="text", text="hello")
IMAGE_BLOCK = ContentBlock(type="image_url", image_url={"url": "https://img.example/fb416.png"})


# ── _serialize_content ─────────────────────────────────────────────────────


def test_string_content_round_trips_unchanged():
    assert _serialize_content("plain text") == "plain text"
    text = "plain text"
    assert _serialize_content(text) is text
    assert _serialize_content("") == ""


def test_non_list_values_pass_through_unchanged():
    assert _serialize_content(None) is None
    payload = {"type": "text", "text": "already plain"}
    assert _serialize_content(payload) is payload


def test_content_blocks_serialise_to_plain_dicts():
    assert _serialize_content([TEXT_BLOCK, IMAGE_BLOCK]) == [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "https://img.example/fb416.png"}},
    ]


def test_non_model_elements_are_passed_through_in_place():
    plain = {"type": "text", "text": "dict block"}
    out = _serialize_content([TEXT_BLOCK, plain, "bare string"])
    assert out[1] is plain
    assert out[2] == "bare string"


def test_serialised_list_is_a_new_list():
    blocks = [TEXT_BLOCK]
    out = _serialize_content(blocks)
    assert out is not blocks
    out.append("mutated")
    assert blocks == [TEXT_BLOCK]


def test_exclude_none_drops_only_none_fields():
    # A text block that also carries an image_url keeps both (the model allows
    # the combination; only the None fields are dropped).
    both = ContentBlock(type="text", text="caption", image_url={"url": "https://img.example/x.png"})
    assert _serialize_content([both]) == [
        {"type": "text", "text": "caption", "image_url": {"url": "https://img.example/x.png"}}
    ]
    assert _serialize_content([TEXT_BLOCK]) == [{"type": "text", "text": "hello"}]


def test_empty_content_list_stays_empty():
    assert _serialize_content([]) == []


# ── _plain_value ───────────────────────────────────────────────────────────


def test_plain_value_dumps_a_model_without_none_fields():
    assert _plain_value(TEXT_BLOCK) == {"type": "text", "text": "hello"}
    assert _plain_value(IMAGE_BLOCK) == {"type": "image_url", "image_url": {"url": "https://img.example/fb416.png"}}


def test_plain_value_drops_none_from_dicts_recursively():
    value = {"a": None, "b": 1, "c": {"d": None, "e": "keep"}, "f": [None, {"g": None, "h": 2}]}
    assert _plain_value(value) == {"b": 1, "c": {"e": "keep"}, "f": [None, {"h": 2}]}


def test_plain_value_keeps_none_list_items():
    # The list branch maps every item; only the dict branch filters None.
    assert _plain_value([None, 1, None]) == [None, 1, None]
    assert _plain_value([TEXT_BLOCK, None]) == [{"type": "text", "text": "hello"}, None]


def test_plain_value_passes_scalars_through():
    for value in ("s", 1, 1.5, True, b"bytes"):
        assert _plain_value(value) is value, repr(value)
    assert _plain_value({}) == {}
    assert _plain_value([]) == []


# ── _content_text ──────────────────────────────────────────────────────────


def test_none_and_empty_content_are_the_empty_string():
    assert _content_text(None) == ""
    assert _content_text("") == ""
    assert _content_text([]) == ""


def test_string_content_is_returned_unchanged():
    assert _content_text("already text") == "already text"


def test_text_blocks_join_in_order_with_a_newline():
    blocks = [ContentBlock(type="text", text="first"), ContentBlock(type="text", text="second")]
    assert _content_text(blocks) == "first\nsecond"
    assert _content_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}, {"type": "text", "text": "c"}]) == (
        "a\nb\nc"
    )


def test_image_blocks_render_as_a_marked_url():
    assert _content_text([IMAGE_BLOCK]) == "[image_url: https://img.example/fb416.png]"
    assert _content_text([TEXT_BLOCK, IMAGE_BLOCK, ContentBlock(type="text", text="after")]) == (
        "hello\n[image_url: https://img.example/fb416.png]\nafter"
    )


def test_empty_text_parts_are_dropped_from_the_join():
    blocks = [{"type": "text", "text": ""}, {"type": "text", "text": "real"}, {"type": "text", "text": ""}]
    assert _content_text(blocks) == "real"
    assert _content_text([{"type": "text", "text": ""}]) == ""


def test_parts_that_are_not_dicts_are_skipped():
    # A bare string inside the list contributes nothing (the helper only reads
    # dict/block parts), so it neither appears nor produces a blank line.
    assert _content_text(["bare", {"type": "text", "text": "kept"}]) == "kept"


def test_unknown_block_types_are_skipped():
    assert _content_text([{"type": "audio", "text": "ignored"}, {"type": "text", "text": "kept"}]) == "kept"


def test_an_image_without_a_usable_url_is_skipped():
    assert _content_text([{"type": "image_url", "image_url": "https://img.example/str.png"}]) == ""
    assert _content_text([{"type": "image_url", "image_url": {}}]) == ""


def test_non_string_text_is_stringified():
    assert _content_text([{"type": "text", "text": 5}]) == "5"
    assert _content_text([{"type": "text", "text": True}]) == "True"


def test_non_list_non_string_values_are_stringified():
    assert _content_text(42) == "42"
    assert _content_text({"a": 1}) == "{'a': 1}"


def test_serialised_blocks_are_accepted_back_verbatim():
    # There is no deserialiser: the one-way trip is serialise -> flatten, and
    # serialising the already-plain dicts again is a no-op.
    serialised = _serialize_content([TEXT_BLOCK, IMAGE_BLOCK])
    assert _content_text(serialised) == "hello\n[image_url: https://img.example/fb416.png]"
    assert _serialize_content(serialised) == serialised
