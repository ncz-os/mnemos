"""Read-path policy: redaction, framing and visibility gates.

These helpers decide what a caller may see on the way OUT of the API —
whether credential spans are masked, whether retrieved content is framed as
untrusted data, and which rows a request is allowed to reach. They lived in
``mnemos/api/routes/memories.py`` and every other route module reached into
that file for them, which made one route module the shared library for six
others. This module is that shared library instead: routes import from here,
and routes do not import each other's private names (enforced by the
import-linter contract in ``pyproject.toml``).

Nothing here has been changed in behaviour; the functions moved verbatim.
"""

import logging

from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.core.security import is_root
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

logger = logging.getLogger(__name__)


def _validate_permission_mode(value: int | None, *, default: int | None = None) -> int | None:
    """Validate Unix-style octal permission digits stored as an integer."""
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(status_code=422, detail="permission_mode must be an integer")
    if value < 0 or value > 777 or any(digit not in "01234567" for digit in str(value)):
        raise HTTPException(status_code=422, detail="permission_mode must be octal-style 0-777")
    return value


def _should_redact_secrets(user: UserContext, *, include_secrets: bool = False, namespace: str | None = None) -> bool:
    """Whether to mask credential spans for this read (redact-at-retrieval).

    Privileged (full content, no masking) ONLY when the caller is root
    AND explicitly opted in (include_secrets=True) or targeted the vault
    namespace. Everything else — every non-root caller, and root on the
    default path — gets credential spans masked ``[REDACTED]`` before the
    content leaves the server. This is the redact-at-retrieval gate that
    backstops a vaulted-miss and masks incidental spans (release-blocking
    2026-06-13).
    """
    if not is_root(user):
        return True
    privileged = bool(include_secrets) or namespace == VAULT_NAMESPACE
    return not privileged


def _should_redact_secrets_for_row(user: UserContext, row) -> bool:
    """Row-aware redact gate for the GET-by-id explicit-fetch escape hatch.

    Root fetching the exact id of a VAULT-namespace row gets full content
    (the deliberate credential-fetch path); root fetching a NON-vault row by
    id is still redacted so an ordinary root read can't leak an incidental
    credential span (ngc-review 2026-06-13). Non-root always redacts.
    """
    if not is_root(user):
        return True
    row_ns = row.get("namespace") if hasattr(row, "get") else None
    return row_ns != VAULT_NAMESPACE


def _content_redacted_for_embedding(content: str, classified) -> str:
    """Return ``content`` with secret spans masked, for embedding (F2, 2026-06-28).

    Adversarial review F2: embeddings must not encode raw secret text. The
    ingest classifier records spans per field (authoritative at ingest);
    reuse them here rather than recomputing, so the embedding is masked
    consistently with what was classified. Content with no spans is
    returned unchanged (no recall cost for clean memories).
    """
    from mnemos.core.secret_detection import redact

    spans = (getattr(classified, "redact_fields", None) or {}).get("content") or []
    if not spans:
        return content
    return redact(content, spans)


def _redacted_for_webhook(content: str, metadata) -> str:
    """Span-redact ``content`` for outbound webhook payloads (F3, 2026-06-28).

    Webhook payloads leave the server to an external HTTP endpoint; raw
    secret content must not leak there (a non-root REST read would mask
    it, and the receiver may feed the payload to an LLM). Use the stored
    ingest spans when available, else recompute, matching the
    redact-at-retrieval contract.
    """
    from mnemos.core.secret_detection import redact_field_with_stored

    return redact_field_with_stored(content, metadata, "content")


def _should_frame_data(user, *, operational: bool = False) -> bool:
    """Whether to apply untrusted-data framing + injection quarantine.

    Prompt-injection defense (release-gate 2026-06-13). Retrieved memories
    are framed as untrusted reference DATA and AI-targeting injection
    meta-instructions are quarantined on EVERY read path by default, so a
    malicious stored memory cannot steer a consuming agent. The ONLY way to
    get verbatim, unframed content is the explicit operational opt-in --
    and, like ``include_secrets``, that opt-in is root-only. A non-root
    caller is ALWAYS framed regardless of the flag.
    """
    if not is_root(user):
        return True
    return not bool(operational)


def _read_visibility_for(user: UserContext, *, namespace: str) -> VisibilityFilter:
    """Read-path visibility for an already-resolved namespace.

    Root callers bypass; non-root callers are pinned. Use when the
    handler has already pinned the namespace explicitly (e.g. on
    create/update) so the same-namespace round-trip doesn't reject.
    """
    if is_root(user):
        return VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=namespace,
        )
    return VisibilityFilter.for_read(user, namespace=namespace)


def _vault_inclusive_read_visibility_for(user: UserContext, *, namespace: str | None = None) -> VisibilityFilter:
    """Root/vault-inclusive visibility for post-write response re-fetches.

    Auto-vaulting moves a just-written row from the caller namespace to
    ``vault``. The normal default read filter intentionally subtracts the
    vault, so a same-transaction response re-fetch would return None and crash
    or roll back. Post-write re-fetches are not enumeration; they target the
    exact id that was just authorized and written, so use root-bypass without
    the default vault subtraction to return the row reliably on every backend.
    """
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=namespace,
        exclude_namespaces=(),
    )


def _mutation_visibility_for(user: UserContext, *, namespace: str | None) -> VisibilityFilter:
    """Mutation-path visibility for an already-resolved namespace.

    Root callers bypass; non-root callers are owner+namespace pinned.
    """
    if is_root(user):
        return VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=namespace,
        )
    return VisibilityFilter.for_mutation(user, namespace=namespace)
