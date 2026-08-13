"""F3 tests (adversarial review 2026-06-28).

F3: webhook payloads must not carry raw secret content. Every
``dispatch_event`` site that puts memory ``content`` on an outbound webhook
now span-redacts it (preferring the spans recorded at ingest, else recompute)
via ``_redacted_for_webhook`` / ``redact_field_with_stored`` — so a webhook
receiver (which a non-root REST read would have masked, and which may feed the
payload straight to an LLM) never sees the raw secret.

These exercise the shared helper the seven dispatch sites call; the call sites
themselves are thin (``"content": _redacted_for_webhook(content, metadata)``).
"""

from __future__ import annotations

from mnemos.core.persisted_text_classification import classify_persisted_text_fields
from mnemos.core.secret_detection import redact_field_with_stored


def test_redacted_for_webhook_masks_secret_via_stored_spans():
    """create/bulk dispatch: spans recorded at ingest mask the payload content."""
    from mnemos.api.routes.memories import _redacted_for_webhook

    secret = "INFRASTRUCTURE CREDENTIALS: root pw is DenylistSelfTest@NotARealSecret1"
    classified = classify_persisted_text_fields(
        content=secret,
        namespace="default",
        classified_at="ingest",
    )
    redacted = _redacted_for_webhook(secret, classified.metadata)
    assert "DenylistSelfTest@NotARealSecret1" not in redacted
    assert "[REDACTED]" in redacted


def test_redacted_for_webhook_recompute_fallback_catches_pat():
    """dag/dedup dispatch with no stored spans -> recompute still masks a PAT."""
    secret = "the github token is ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890"
    # No metadata (e.g. a row without recorded spans) -> recompute backstop.
    masked = redact_field_with_stored(secret, None, "content")
    assert "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890" not in masked
    assert "[REDACTED]" in masked


def test_redacted_for_webhook_clean_content_passthrough():
    """Clean content is sent verbatim — no recall/format cost on the happy path."""
    from mnemos.api.routes.memories import _redacted_for_webhook

    clean = "deployment finished; see https://status.example.com/incidents/42"
    classified = classify_persisted_text_fields(
        content=clean,
        namespace="default",
        classified_at="ingest",
    )
    assert _redacted_for_webhook(clean, classified.metadata) == clean


def test_redacted_for_webhook_handles_json_string_metadata():
    """Row metadata can arrive as a JSON string (db round-trip); spans still resolve."""
    import json

    from mnemos.api.routes.memories import _redacted_for_webhook

    content = "the magic word is flibbertigibbet and it is harmless"
    # flibbertigibbet is not a secret shape -> only stored spans can mask it.
    meta = json.dumps({"secret_redact_fields": {"content": [[15, 37]]}})
    redacted = _redacted_for_webhook(content, meta)
    assert "flibbertigibbet" not in redacted
    assert "[REDACTED]" in redacted
