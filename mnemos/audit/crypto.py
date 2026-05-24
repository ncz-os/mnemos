"""Cryptographic primitives for the v6.2 audit chain.

This module is pure: no DB access, no logging side effects. It defines
the canonical-bytes representation, key derivation, signing, and the
Merkle leaf-hash + tree-root used by the sealer.

Design reference: docs/v6.2-nexus-pattern-adoption.md § 1.

JCS-lite canonicalization
-------------------------
RFC 8785 requires UTF-16 code-unit key sorting. We approximate with
``json.dumps(..., sort_keys=True, separators=(',', ':'),
ensure_ascii=False)``. For ASCII-only object keys (our case for
memory rows: id, content, category, subcategory, metadata, embedding_hash)
the two are bytewise identical. Non-ASCII key surrogate-pair edge
cases are out of scope for v6.2 ship; we'll swap in a real RFC-8785
implementation (e.g. ``rfc8785`` PyPI) if any non-ASCII key ever
enters the canonical set. nexus PR #9 (UTF-16 fix) noted in the
design doc.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Iterable, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature


# ---------- JCS-lite ----------


def _jcs(payload: Mapping[str, object]) -> bytes:
    """Sorted-keys compact JSON; ASCII-key-safe approximation of RFC 8785."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# ---------- Payload hash (used in payload_hash column) ----------


def canonical_payload_hash(
    *,
    memory_id: str,
    content: str,
    category: str,
    subcategory: str | None,
    metadata: Mapping[str, object] | None,
    embedding: bytes | None,
) -> bytes:
    """SHA-256 over JCS({id, content, category, subcategory, metadata,
    embedding_hash}). Returns 32 bytes.

    Embedding goes in as SHA-256 of its raw float32 bytes (not the
    bytes themselves) so reshipping the same memory under copy_embeddings
    doesn't perturb the audit hash if the model is unchanged.
    """
    embedding_hash = hashlib.sha256(embedding).hexdigest() if embedding else ""
    payload = {
        "id": memory_id,
        "content": content,
        "category": category,
        "subcategory": subcategory or "",
        "metadata": dict(metadata or {}),
        "embedding_hash": embedding_hash,
    }
    return hashlib.sha256(_jcs(payload)).digest()


# ---------- Entry canonical bytes (used in signature) ----------


@dataclass(frozen=True)
class AuditEntry:
    """One row of memory_audit_chain pre-signature."""

    entry_id: bytes  # 16 bytes (UUIDv7 bytes)
    memory_id: bytes  # 16 bytes
    prev_entry_id: bytes | None  # 16 bytes or None
    prev_entry_hash: bytes | None  # 32 bytes or None
    op: str
    payload_hash: bytes  # 32 bytes
    writer_id: str
    writer_pubkey: bytes  # 32 bytes
    signed_at: str  # ISO 8601 UTC; included in signed payload

    def _signing_payload(self) -> Mapping[str, object]:
        """Columns that are part of the signed bytes — every column
        except ``signature``, ``global_root``, ``global_seq``."""
        return {
            "entry_id": self.entry_id.hex(),
            "memory_id": self.memory_id.hex(),
            "prev_entry_id": (self.prev_entry_id.hex() if self.prev_entry_id else ""),
            "prev_entry_hash": (self.prev_entry_hash.hex() if self.prev_entry_hash else ""),
            "op": self.op,
            "payload_hash": self.payload_hash.hex(),
            "writer_id": self.writer_id,
            "writer_pubkey": self.writer_pubkey.hex(),
            "signed_at": self.signed_at,
        }


def canonical_entry_bytes(entry: AuditEntry) -> bytes:
    """Canonical bytes signed by the writer key."""
    return _jcs(entry._signing_payload())


def entry_hash(entry: AuditEntry, signature: bytes) -> bytes:
    """SHA-256 over canonical bytes || signature.

    This is what next entry's ``prev_entry_hash`` column stores —
    chains the linked list cryptographically. Returns 32 bytes.
    """
    h = hashlib.sha256()
    h.update(canonical_entry_bytes(entry))
    h.update(signature)
    return h.digest()


# ---------- Key derivation ----------

_HKDF_INFO_PREFIX = b"mnemos.audit.writer.v1:"


def derive_writer_keypair(
    session_secret: bytes,
    writer_id: str,
) -> tuple[Ed25519PrivateKey, bytes]:
    """HKDF-SHA256 derive a writer's Ed25519 private key.

    ``session_secret`` is normally ``settings.server.session_secret``
    bytes. ``writer_id`` is the caller user_id (or e.g. ``"fed:pythia"``
    for federation replicates). Returns (private_key, public_key_bytes_raw).

    Same writer_id under the same session secret derives the same key.
    Rotating the session secret rotates all writer keys.
    """
    if not session_secret:
        raise ValueError("session_secret is empty; cannot derive writer key")
    if not writer_id:
        raise ValueError("writer_id is empty; cannot derive writer key")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"mnemos.audit.writer.v1",
        info=_HKDF_INFO_PREFIX + writer_id.encode("utf-8"),
    )
    seed = hkdf.derive(session_secret)
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public_bytes = private.public_key().public_bytes_raw()
    return private, public_bytes


# ---------- Root key (per-instance) ----------


def load_root_keypair(
    env_var: str = "MNEMOS_AUDIT_ROOT_PRIVKEY",
) -> tuple[Ed25519PrivateKey, bytes]:
    """Load Ed25519 root key from env (base64-encoded 32-byte seed).

    Returns (private_key, public_key_bytes_raw).
    Raises ``ValueError`` when ``MNEMOS_AUDIT_CHAIN=on`` (default in
    v6.2) but the env var is unset or malformed — same fail-loud
    pattern as session-secret hardening (v6.1 P3 #38).
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        raise ValueError(
            f"{env_var} is unset; required when MNEMOS_AUDIT_CHAIN is on. "
            'Generate with: python -c "import os, base64; '
            'print(base64.b64encode(os.urandom(32)).decode())"'
        )
    try:
        seed = base64.b64decode(raw)
    except Exception as exc:
        raise ValueError(f"{env_var} is not valid base64: {exc}") from exc
    if len(seed) != 32:
        raise ValueError(f"{env_var} must decode to exactly 32 bytes (got {len(seed)})")
    private = Ed25519PrivateKey.from_private_bytes(seed)
    return private, private.public_key().public_bytes_raw()


# ---------- Signing / verifying ----------


def sign_entry(entry: AuditEntry, private_key: Ed25519PrivateKey) -> bytes:
    """Sign canonical entry bytes. Returns 64-byte Ed25519 signature."""
    return private_key.sign(canonical_entry_bytes(entry))


def verify_entry(
    entry: AuditEntry,
    signature: bytes,
    writer_pubkey: bytes | None = None,
) -> bool:
    """Verify Ed25519 signature against canonical entry bytes.

    Defaults to using ``entry.writer_pubkey``; override for testing
    e.g. that a fabricated entry with mismatched pubkey rejects.
    Returns ``True`` iff valid (constant time via ``cryptography``).
    """
    pk = writer_pubkey if writer_pubkey is not None else entry.writer_pubkey
    if len(pk) != 32:
        return False
    if len(signature) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pk).verify(signature, canonical_entry_bytes(entry))
        return True
    except InvalidSignature:
        return False


# ---------- Merkle helpers ----------


def merkle_leaf(entry_id: bytes, signature: bytes) -> bytes:
    """Leaf hash = SHA-256(entry_id || signature). 32 bytes."""
    h = hashlib.sha256()
    h.update(entry_id)
    h.update(signature)
    return h.digest()


def merkle_root(leaves: Iterable[bytes]) -> bytes:
    """SHA-256 Merkle tree root with zero-padding to power-of-two width.

    Returns 32 zero bytes for an empty leaves iterable (sentinel for
    "sealed window with no entries"; sealer skips empty windows).
    """
    level = [bytes(leaf) for leaf in leaves]
    if not level:
        return b"\x00" * 32
    # Pad to next power of two with the zero leaf.
    n = 1
    while n < len(level):
        n <<= 1
    if len(level) < n:
        zero = b"\x00" * 32
        level.extend([zero] * (n - len(level)))
    while len(level) > 1:
        next_level = []
        for i in range(0, len(level), 2):
            h = hashlib.sha256()
            h.update(level[i])
            h.update(level[i + 1])
            next_level.append(h.digest())
        level = next_level
    return level[0]


# ---------- Convenience: constant-time bytes-equal for tests ----------


def constant_time_eq(a: bytes, b: bytes) -> bool:
    """Wrapper around ``hmac.compare_digest`` — useful in callers
    that want to compare hashes / signatures without leaking timing.
    """
    return hmac.compare_digest(a, b)
