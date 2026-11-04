"""Server-stamped provenance for memory records (FB114, audit M3 2026-11-02).

The audit found that ~60 agents' status records carry only CALLER-asserted
identity columns (``source_agent`` / ``source_provider`` / ``source_model`` /
``source_session``) — nothing on the row binds the record to the principal
that actually authenticated the write, so a hostile agent can write an
APPROVE verdict for another agent's job and the record reads as if the other
agent wrote it.

The minimal proposal is implemented here:

* every authenticated write stamps a ``_provenance`` block into the row's
  persisted metadata with the RESOLVED principal (``user.user_id``), the
  API-key id the context was resolved from, the UTC timestamp, and — when a
  signing key is configured — an HMAC-SHA256 signature over that tuple;
* any caller-supplied ``_provenance`` is dropped BEFORE stamping (the block
  is server-only, like ``secret_*`` metadata), so a forgery cannot be
  planted;
* :func:`verify_provenance` recomputes the signature and answers whether the
  block is intact (``True``), tampered (``False``) or absent (``None``).
  FB118: an unsigned block is ``False`` from verify too — unattested means
  unverified — and a signed block without the key verifies ``False``.

Unsigned mode is a documented limitation: when ``MNEMOS_PROVENANCE_HMAC_KEY``
is not configured the block is still server-stamped (the principal binding
holds — the server is the only writer of the block) but carries
``signed=false`` and ``sig=null``; tamper-evidence then degrades to
treat-as-missing downstream. Operators who need the cryptographic binding set
the key (the pattern mirrors ``MNEMOS_GRAEAE_AUDIT_HMAC_KEY``).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from mnemos.core.config import runtime_env_value

#: The metadata key this module owns. Server-only: every write path strips a
#: caller copy before classification re-stamps it.
PROVENANCE_KEY = "_provenance"

#: Block schema version.
PROVENANCE_VERSION = 1

_PROVENANCE_ENV_KEY = "MNEMOS_PROVENANCE_HMAC_KEY"



def provenance_signing_key() -> str | None:
    """Return the configured signing key, or ``None`` for unsigned mode."""
    key = runtime_env_value(_PROVENANCE_ENV_KEY, "").strip()
    return key or None


def _signature_payload(*, principal: str, key_id: str | None, at: str) -> bytes:
    # Length-prefix every field so no (principal, key_id, at) tuple can collide
    # with another through a separator character inside a field.
    out = b""
    for part in (principal, key_id or "", at):
        raw = part.encode("utf-8")
        out += len(raw).to_bytes(4, "big") + raw
    return out


def stamp_provenance(
    metadata: dict,
    *,
    principal: str,
    key_id: str | None = None,
    sign_key: str | None = None,
) -> dict:
    """Return ``metadata`` with the server-stamped ``_provenance`` block added.

    The caller's ``principal`` must be the AUTHENTICATED identity resolved
    server-side — never a request-body field. When ``sign_key`` is falsy the
    block is stamped unsigned (``signed=false``), the documented degraded mode.
    """
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sign_key = sign_key if sign_key else provenance_signing_key()
    if sign_key:
        sig = hmac.new(
            sign_key.encode("utf-8"),
            _signature_payload(principal=principal, key_id=key_id, at=at),
            hashlib.sha256,
        ).hexdigest()
        block = {
            "v": PROVENANCE_VERSION,
            "principal": principal,
            "key_id": key_id,
            "at": at,
            "signed": True,
            "sig": sig,
        }
    else:
        block = {
            "v": PROVENANCE_VERSION,
            "principal": principal,
            "key_id": key_id,
            "at": at,
            "signed": False,
            "sig": None,
        }
    meta = dict(metadata or {})
    meta[PROVENANCE_KEY] = block
    return meta


def verify_provenance(metadata: dict | None) -> bool | None:
    """Answer three-valued: block intact (``True``), tampered (``False``), absent (``None``).

    FB118 (2026-11-02, rework of FB114 after review — fail closed): an
    UNSIGNED block never verifies, with or without a configured key. It
    carries no cryptographic attestation, and without a key no signature
    could ever be recomputed; before FB118 the unsigned branch answered
    ``sig is None``, so keyless deployments reported ``True`` for unattested
    records — passing the review finding. ``signed`` still marks the block as
    unstamped-signature so the doctor can refuse unsigned records explicitly.
    """
    if not isinstance(metadata, dict):
        return None
    block = metadata.get(PROVENANCE_KEY)
    if not isinstance(block, dict):
        return None
    principal = block.get("principal")
    if not isinstance(principal, str) or not principal:
        return False
    # Well-formedness gate (review finding, unsigned mode included): the
    # stamp text and schema version must be present, or the block was not
    # written by this code path.
    at = block.get("at")
    if not isinstance(at, str) or not at or block.get("v") != PROVENANCE_VERSION:
        return False
    if not block.get("signed"):
        # FB118: fail closed — unsigned means unverified, never True.
        return False
    key = provenance_signing_key()
    if not key:
        # A signed block cannot be verified without the key: fail closed.
        return False
    expected = hmac.new(
        key.encode("utf-8"),
        _signature_payload(principal=principal, key_id=block.get("key_id"), at=block.get("at") or ""),
        hashlib.sha256,
    ).hexdigest()
    presented = block.get("sig")
    if not isinstance(presented, str):
        return False
    return hmac.compare_digest(expected, presented)
