"""Persisted text secret classification helpers.

Every write path that stores user/peer supplied text must classify *all* text
variants before they are persisted (canonical content, verbatim/original text,
and compressed variants).  Credential-record memories are moved to the vault
namespace; incidental credential spans are recorded in metadata for the
redact-on-read layer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from mnemos.core.provenance import PROVENANCE_KEY, stamp_provenance
from mnemos.core.secret_detection import (
    SECRET_CLEAN_MARKER_KEY,
    SecretClass,
    VAULT_NAMESPACE,
    classify,
    clean_marker_fields,
)

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


# M1 (code-health decisions 2026-10-31): every ``secret_*`` metadata key is
# SERVER-ONLY. ``secret_redact_fields`` / ``secret_redact_spans`` are the
# stored-span marker the read path trusts ahead of recomputing the
# classifier, so a caller that could write one could switch read-time
# masking off for a row (plant ``{"content": [[0, 0]]}`` — a span list that
# masks nothing). ``secret_vaulted`` / ``secret_original_namespace`` /
# ``secret_classified_at`` / ``secret_reasons`` are equally
# authorization-relevant. The prefix is the contract: any future
# ``secret_*`` key is stripped too, so a new marker cannot become
# caller-writable by omission.
_SERVER_ONLY_METADATA_PREFIX = "secret_"
_MAX_METADATA_WALK_DEPTH = 8


#: Typed status leaf shapes (FB114, audit M4): these keys are exempt from the
#: classifier when their value matches the field's strict type — mirrors the
#: "status ids are not secrets" finding (audit M2): a 64-hex head_sha, a
#: branch slug or a counted test tally metalog is not a credential. The
#: exemption is a 3-way line: the key is a typed status field, the value
#: matches that field's shape, AND the value classifies to something other
#: than VAULT. A ghp_ token planted as the "branch" value therefore fails the
#: safe direction: it classifies VAULT and vaults the record.
_TYPED_STATUS_LEAF_SHAPES: dict[str, re.Pattern] = {
    "job_id": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"),
    "branch": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"),
    "verdict": re.compile(r"^[A-Za-z]{1,24}$"),
    "phase": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,47}$"),
    "reviewer_model": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"),
}
_SHA_LEAF_VALUE_RE = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")


def _typed_status_leaf_exempt(key: str, value: str) -> bool:
    """Classify one metadata leaf and exempt it only when it is truly status data."""
    normalized = key.lower()
    shape = _TYPED_STATUS_LEAF_SHAPES.get(normalized)
    if shape is None:
        if normalized.endswith("_sha"):
            shape = _SHA_LEAF_VALUE_RE
        else:
            return False
    if not shape.match(value):
        return False
    # Shape matches: only a VAULT-class finding breaks the exemption.
    try:
        leaf_finding = classify(value)
    except Exception:
        # Fail closed: an uncertain leaf is not exempt.
        return False
    if leaf_finding.cls is SecretClass.VAULT:
        return False
    return True


def _classify_metadata_leaves(meta: dict[str, Any]) -> tuple[bool, list[list[Any]], list[str]]:
    """Run ``classify`` over every STRING leaf of the metadata channel.

    FB114 (audit M4): the metadata channel reached disk and the read path
    UNCLASSIFIED. Three-valued like the text fields:

    * a VAULT-tree finding vaults the WHOLE record (fail-closed, same as text);
    * a REDACT-tree finding records the leaf path for read-time masking
      (leaf-level, not span-level — the value is replaced with
      ``[REDACTED]`` on the default read path);
    * CLEAN leaves are untouched.

    Solid ``secret_*``/``_provenance`` server markers and non-string leaves
    are skipped. Returns ``(any_vault, redact_paths, reasons)``.
    """
    any_vault = False
    redact_paths: list[list[Any]] = []
    reasons: list[str] = []

    def _walk(node: Any, path: list[Any], depth: int) -> None:
        nonlocal any_vault
        if depth > _MAX_METADATA_WALK_DEPTH:
            # Fail closed: a subtree below the depth limit is never classified,
            # so a credential nested past the limit would otherwise persist and
            # read back verbatim. Vault the record instead of skipping it.
            if node:
                any_vault = True
                dotted = ".".join(str(p) for p in path)
                reasons.append(f"metadata:{dotted}:depth_exceeded")
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_str = str(key)
                if key_str.startswith("secret_") or key_str == PROVENANCE_KEY:
                    continue  # server-only marker channel
                if isinstance(value, str) and value:
                    if _typed_status_leaf_exempt(key_str, value):
                        continue
                    finding = classify(value)
                    if finding.cls is not SecretClass.CLEAN:
                        here = path + [key]
                        reasons.extend(f"metadata:{'.'.join(str(p) for p in here)}:{r}" for r in finding.reasons)
                        if finding.cls is SecretClass.VAULT:
                            any_vault = True
                        elif finding.spans:
                            redact_paths.append(here)
                elif isinstance(value, (dict, list)):
                    _walk(value, path + [key], depth + 1)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, (dict, list)):
                    _walk(value, path + [idx], depth + 1)
                elif isinstance(value, str) and value:
                    finding = classify(value)
                    if finding.cls is not SecretClass.CLEAN:
                        here = path + [idx]
                        reasons.extend(f"metadata:{'.'.join(str(p) for p in here)}:{r}" for r in finding.reasons)
                        if finding.cls is SecretClass.VAULT:
                            any_vault = True
                        elif finding.spans:
                            redact_paths.append(here)

    _walk(meta, [], 0)
    return any_vault, redact_paths, reasons



def strip_caller_secret_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop every caller-/peer-supplied server-only metadata key.

    Applied at every write path (create, bulk create, PATCH, import,
    federation ingest) BEFORE classification, so the classification result
    is the only source of secret metadata that can ever be persisted.

    FB114 (audit M3): ``_provenance`` is a server-only key too — a caller may
    not plant a forged provenance block; :func:`stamp_provenance` re-adds the
    server's own block after classification when the write carries an
    authenticated principal.
    """
    return {
        key: value
        for key, value in _metadata_dict(metadata).items()
        if not str(key).startswith(_SERVER_ONLY_METADATA_PREFIX) and str(key) != PROVENANCE_KEY
    }


def classify_persisted_text_fields(
    *,
    content: str | None = None,
    verbatim_content: str | None = None,
    compressed_content: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    namespace: str,
    classified_at: str = "ingest",
    memory_id: str | None = None,
    principal: str | None = None,
    api_key_id: str | None = None,
) -> PersistedTextClassification:
    """Classify all persisted text fields for one logical memory write.

    Returns the namespace/metadata to persist.  Any VAULT-class finding in any
    supplied field vaults the whole memory/record.  REDACT findings are tracked
    per field so retrieval paths can mask the canonical, verbatim, and
    compressed forms consistently.

    Caller-supplied ``secret_*`` metadata is dropped first (see
    :func:`strip_caller_secret_metadata`): this function is the only source
    of the server-stamped marker, so the metadata in the result is rebuilt
    from scratch rather than merged over whatever the caller sent.

    FB114 (audit M4): every STRING leaf of the caller metadata channel is
    classified too — the channel used to persist and read back UNCLASSIFIED.
    Typed status fields (job_id, *_sha, branch, verdict, phase,
    reviewer_model) whose value matches the field's strict shape are exempt
    (status ids are not secrets), except that a VAULT-shaped value breaks the
    exemption deliberately.

    FB114 (audit M3): when ``principal`` is supplied (the AUTHENTICATED
    identity resolved server-side, never a request-body field) the result is
    stamped with the server-only ``_provenance`` block; caller-supplied
    ``_provenance`` is stripped before classification (see
    :func:`strip_caller_secret_metadata`).

    The helper fails closed: classifier exceptions quarantine into the vault.
    """

    meta = strip_caller_secret_metadata(metadata)
    fields = {
        "content": content,
        "verbatim_content": verbatim_content,
        "compressed_content": compressed_content,
    }
    reasons: list[str] = []
    redact_fields: dict[str, list[tuple[int, int]]] = {}
    clean_fields: list[str] = []
    vaulted = False

    try:
        for field_name, text in fields.items():
            if text is None or not str(text):
                continue
            finding = classify(str(text))
            if finding.cls is SecretClass.CLEAN:
                # M6: remember the CLEAN verdict so the read path can skip
                # recomputing the classifier for this field. Only fields the
                # classifier actually saw are recorded.
                clean_fields.append(field_name)
                continue
            reasons.extend(f"{field_name}:{reason}" for reason in finding.reasons)
            if finding.spans:
                redact_fields[field_name] = list(finding.spans)
            if finding.cls is SecretClass.VAULT:
                vaulted = True

        # FB114 (audit M4): the metadata channel is classified like content.
        # Runs INSIDE the fail-closed try: a classifier failure vaults the
        # record like any other classification failure.
        _any_vault, _meta_redact_paths, _meta_reasons = _classify_metadata_leaves(meta)
        vaulted = vaulted or _any_vault
        reasons.extend(_meta_reasons)
        if _meta_redact_paths:
            meta["secret_redact_metadata"] = _meta_redact_paths
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
    if clean_fields:
        # M6 (code-health decisions 2026-10-31): the "clean under classifier
        # vN" marker. Stamped only here and only from an actual CLEAN verdict;
        # the read path skips its recompute while the version still matches.
        meta[SECRET_CLEAN_MARKER_KEY] = clean_marker_fields(clean_fields)

    if vaulted:
        logger.warning(
            "[secret-vault] auto-vaulted persisted text %s (reasons=%s)",
            memory_id or "<unknown>",
            meta.get("secret_reasons", []),
        )

    # FB114 (audit M3): server-stamped provenance AFTER classification so the
    # block reflects the final persisted metadata. No principal → no stamp
    # (peer/federation/import writes carry no caller identity; stated
    # plainly as out of scope).
    if principal is not None:
        meta = stamp_provenance(meta, principal=principal, key_id=api_key_id)

    return PersistedTextClassification(
        namespace=new_namespace,
        metadata=meta,
        vaulted=vaulted,
        redact_fields=redact_fields,
        reasons=meta.get("secret_reasons", []),
    )
