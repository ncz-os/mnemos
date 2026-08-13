"""F2/F2b tests (adversarial review 2026-06-28).

F2: embeddings are computed on span-redacted content, not raw, so secret
text never enters the vector index.

F2b: retrieval redaction prefers the spans recorded at ingest (stored in
row metadata) over recomputing classify() at retrieval, so a later
classifier relaxation cannot re-expose a secret caught at ingest.
"""

from __future__ import annotations

from mnemos.core.persisted_text_classification import classify_persisted_text_fields
from mnemos.core.secret_detection import (
    _stored_spans,
    redact_content,
    redact_field_with_stored,
)
from mnemos.domain.models import row_to_memory


def test_content_redacted_for_embedding_uses_stored_spans():
    """F2: _content_redacted_for_embedding masks secret spans before embedding."""
    from mnemos.api.routes.memories import _content_redacted_for_embedding

    secret = "INFRASTRUCTURE CREDENTIALS: root pw is DenylistSelfTest@NotARealSecret1"
    classified = classify_persisted_text_fields(
        content=secret, namespace="default", classified_at="ingest",
    )
    redacted = _content_redacted_for_embedding(secret, classified)
    assert "DenylistSelfTest@NotARealSecret1" not in redacted
    assert "[REDACTED]" in redacted


def test_content_redacted_for_embedding_clean_passthrough():
    """F2: clean content (no spans) passes through unchanged for embedding."""
    from mnemos.api.routes.memories import _content_redacted_for_embedding

    clean = "API endpoint is https://api.example.com/v2/auth"
    classified = classify_persisted_text_fields(
        content=clean, namespace="default", classified_at="ingest",
    )
    assert _content_redacted_for_embedding(clean, classified) == clean


def test_redact_field_with_stored_prefers_stored_spans_over_recompute():
    """F2b: a span stored at ingest is masked even if recompute would miss it."""
    # "flibbertigibbet" is not a secret shape — recompute classify() finds nothing.
    content = "the magic word is flibbertigibbet and it is harmless"
    stored = {"secret_redact_fields": {"content": [(15, 37)]}}  # "word is flibbertigibbet"

    recompute = redact_content(content)  # no spans -> unchanged
    assert recompute == content

    via_stored = redact_field_with_stored(content, stored, "content")
    assert "flibbertigibbet" not in via_stored
    assert "[REDACTED]" in via_stored


def test_redact_field_with_stored_falls_back_to_recompute_without_metadata():
    """F2b: no stored spans -> fall back to recompute (redact-at-retrieval backstop)."""
    secret = "the github token is ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890"
    # No metadata -> recompute must still catch the high-confidence PAT prefix.
    masked = redact_field_with_stored(secret, None, "content")
    assert "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890" not in masked
    assert "[REDACTED]" in masked


def test_redact_field_with_stored_legacy_secret_redact_spans():
    """F2b: legacy content-only secret_redact_spans (pre per-field map) still honored."""
    content = "token is hunter2-hunter2-hunter2"
    # A span that recompute would not flag but legacy metadata records.
    legacy = {"secret_redact_spans": [(9, 30)]}
    via_stored = redact_field_with_stored(content, legacy, "content")
    assert "hunter2" not in via_stored
    assert "[REDACTED]" in via_stored


def test_stored_spans_per_field():
    """F2b: _stored_spans resolves per-field from secret_redact_fields."""
    md = {
        "secret_redact_fields": {
            "content": [(0, 5)],
            "verbatim_content": [(10, 20)],
            "compressed_content": [(0, 3)],
        }
    }
    assert _stored_spans(md, "content") == [(0, 5)]
    assert _stored_spans(md, "verbatim_content") == [(10, 20)]
    assert _stored_spans(md, "compressed_content") == [(0, 3)]
    assert _stored_spans(md, "missing") == []


def test_row_to_memory_uses_stored_spans_for_redaction():
    """F2b: row_to_memory redacts content via stored spans, not recompute."""
    content = "the safe word is flibbertigibbet today"
    row = {
        "id": "mem_test",
        "content": content,
        "category": "rules",
        "created": "2026-06-28T00:00:00Z",
        "metadata": {"secret_redact_fields": {"content": [(15, 29)]}},
    }
    item = row_to_memory(row, redact_secrets=True)
    assert "flibbertigibbet" not in item.content
    assert "[REDACTED]" in item.content
