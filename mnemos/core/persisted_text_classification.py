"""Persisted text secret classification helpers.

Every write path that stores user/peer supplied text must classify *all* text
variants before they are persisted (canonical content, verbatim/original text,
and compressed variants).  Credential-record memories are moved to the vault
namespace; incidental credential spans are recorded in metadata for the
redact-on-read layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from mnemos.core.secret_detection import SecretClass, VAULT_NAMESPACE, classify

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistedTextClassification:
    namespace: str
    metadata: dict[str, Any]
    vaulted: bool = False
    redact_fields: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _metadata_dict(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return dict(metadata)
    try:
        return dict(metadata)
    except Exception:
        return {}


def classify_persisted_text_fields(
    *,
    content: str | None = None,
    verbatim_content: str | None = None,
    compressed_content: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    namespace: str,
    classified_at: str = "ingest",
    memory_id: str | None = None,
) -> PersistedTextClassification:
    """Classify all persisted text fields for one logical memory write.

    Returns the namespace/metadata to persist.  Any VAULT-class finding in any
    supplied field vaults the whole memory/record.  REDACT findings are tracked
    per field so retrieval paths can mask the canonical, verbatim, and
    compressed forms consistently.

    The helper fails closed: classifier exceptions quarantine into the vault.
    """

    meta = _metadata_dict(metadata)
    fields = {
        "content": content,
        "verbatim_content": verbatim_content,
        "compressed_content": compressed_content,
    }
    reasons: list[str] = []
    redact_fields: dict[str, list[tuple[int, int]]] = {}
    vaulted = False

    try:
        for field_name, text in fields.items():
            if text is None or not str(text):
                continue
            finding = classify(str(text))
            if finding.cls is SecretClass.CLEAN:
                continue
            reasons.extend(f"{field_name}:{reason}" for reason in finding.reasons)
            if finding.spans:
                redact_fields[field_name] = list(finding.spans)
            if finding.cls is SecretClass.VAULT:
                vaulted = True
    except Exception:
        logger.exception(
            "[secret-vault] persisted-text classification FAILED for %s — quarantining into vault",
            memory_id or "<unknown>",
        )
        reasons.append("classification_failed_fail_closed")
        meta["secret_classification_error"] = True
        vaulted = True

    new_namespace = namespace
    if vaulted and namespace != VAULT_NAMESPACE:
        meta["secret_vaulted"] = True
        meta["secret_original_namespace"] = namespace
        new_namespace = VAULT_NAMESPACE
    elif vaulted:
        meta.setdefault("secret_vaulted", True)

    if reasons:
        # Deduplicate while preserving order for stable fixtures/logs.
        meta["secret_reasons"] = list(dict.fromkeys(reasons))
        meta["secret_classified_at"] = classified_at
    if redact_fields:
        meta["secret_redact_fields"] = redact_fields
        # Back-compat for older readers/tests that looked only at canonical spans.
        if "content" in redact_fields:
            meta["secret_redact_spans"] = redact_fields["content"]

    if vaulted:
        logger.warning(
            "[secret-vault] auto-vaulted persisted text %s (reasons=%s)",
            memory_id or "<unknown>",
            meta.get("secret_reasons", []),
        )

    return PersistedTextClassification(
        namespace=new_namespace,
        metadata=meta,
        vaulted=vaulted,
        redact_fields=redact_fields,
        reasons=meta.get("secret_reasons", []),
    )
