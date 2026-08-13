"""GRAEAE consultation audit-chain hashing.

This lives in core because the WRITERS are per-backend persistence modules
(postgres/oracle/sqlite) while the VERIFIER lives in the graeae overlay
distribution. They previously computed different things:

    writer   sha256(prev_chain + prompt_hash + response_hash)      3 fields
    verifier hmac(prev, seq, consultation_id, task_type, provider,
                  quality_score, prompt_hash, response_hash)       8 fields

so verification failed on every row ever written, and `fetch_audit_chain` did
not even return four of the fields the verifier needed. One definition, used by
both sides, is the only way that stays fixed.

TWO ALGORITHMS ARE SUPPORTED ON PURPOSE. An audit chain is tamper-evident: its
value comes from nobody being able to rewrite it, including us. Re-signing the
existing rows under the new algorithm would make old entries verify again at the
cost of destroying exactly the property the log exists to provide, and would void
any attestation already made against it. So rows carry the algorithm that signed
them, historical rows keep their original signatures, and only new rows use v2.
"""

from __future__ import annotations

import hashlib
import hmac

#: Rows written before the chain algorithm was versioned. A bare SHA-256 over
#: three fields, with no key: forgeable by anyone who can read the table, which
#: is why v2 exists.
ALGO_SHA256_V1 = "sha256-v1"

#: Keyed HMAC over the full ordered metadata tuple, so the provider, task type
#: and quality score attributed to a consultation are bound into the chain and
#: cannot be edited after the fact.
ALGO_HMAC_V2 = "hmac-v2"

#: What new writes use.
CURRENT_ALGO = ALGO_HMAC_V2

_FIELD_SEP = "\x1f"
_GENESIS_LABEL = b"mnemos-graeae-audit-genesis"


class AuditChainKeyMissing(RuntimeError):
    """Raised when a v2 operation is attempted without a signing key."""


def _hmac_key(key: str | bytes | None) -> bytes:
    if not key:
        raise AuditChainKeyMissing(
            "audit chain signing key (MNEMOS_GRAEAE_AUDIT_HMAC_KEY) is not "
            "configured; refusing to operate an unsigned audit chain"
        )
    return key.encode() if isinstance(key, str) else key


def genesis_hash(key: str | bytes | None) -> str:
    """Keyed chain root, so it cannot be reproduced without the key."""
    return hmac.new(_hmac_key(key), _GENESIS_LABEL, hashlib.sha256).hexdigest()


def compute_v1(*, prev_chain_hash: str, prompt_hash: str, response_hash: str) -> str:
    """The legacy unkeyed hash. Provided ONLY so historical rows still verify."""
    return hashlib.sha256(
        ((prev_chain_hash or "") + (prompt_hash or "") + (response_hash or "")).encode()
    ).hexdigest()


def compute_v2(
    *,
    key: str | bytes | None,
    prev_chain_hash: str,
    prompt_hash: str,
    response_hash: str,
    sequence_num: object = None,
    consultation_id: object = None,
    task_type: object = None,
    provider: object = None,
    quality_score: object = None,
) -> str:
    """HMAC each link over the full, ordered metadata tuple."""
    payload = _FIELD_SEP.join(
        [
            prev_chain_hash or "",
            "" if sequence_num is None else str(sequence_num),
            "" if consultation_id is None else str(consultation_id),
            "" if task_type is None else str(task_type),
            "" if provider is None else str(provider),
            "" if quality_score is None else f"{float(quality_score):.6f}",
            prompt_hash or "",
            response_hash or "",
        ]
    )
    return hmac.new(_hmac_key(key), payload.encode(), hashlib.sha256).hexdigest()


def compute_for_algo(algo: str | None, **kwargs: object) -> str:
    """Dispatch to the algorithm a row was actually signed with.

    A NULL/absent algo means the row predates versioning, so it is v1 -- that is
    what makes the migration a pure metadata backfill rather than a re-signing.
    """
    if (algo or ALGO_SHA256_V1) == ALGO_SHA256_V1:
        return compute_v1(
            prev_chain_hash=kwargs.get("prev_chain_hash"),  # type: ignore[arg-type]
            prompt_hash=kwargs.get("prompt_hash"),  # type: ignore[arg-type]
            response_hash=kwargs.get("response_hash"),  # type: ignore[arg-type]
        )
    return compute_v2(**kwargs)  # type: ignore[arg-type]
