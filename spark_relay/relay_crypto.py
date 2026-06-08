"""End-to-end encryption for the Spark<->PYTHIA cloud-object relay.

The relay bucket (GCS) is a transport only; it must never see plaintext job
payloads or MNEMOS context. Both endpoints share one symmetric AES-256-GCM key
(``SPARK_HIVE_RELAY_E2EE_KEY``, base64, 32 bytes). The cloud provider sees only
ciphertext.

Wire format of a sealed blob::

    magic(4) || version(1) || nonce(12) || ciphertext+tag(...)

``magic`` = ``b"SHR1"`` lets us reject foreign / corrupt objects early; the GCM
tag (appended to the ciphertext by ``AESGCM.encrypt``) authenticates both the
ciphertext and the magic+version header (passed as associated data).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"SHR1"
VERSION = 1
_NONCE_LEN = 12
_HEADER = MAGIC + bytes([VERSION])
_KEY_ENV = "SPARK_HIVE_RELAY_E2EE_KEY"


class RelayCryptoError(Exception):
    """Raised when a blob cannot be sealed or opened."""


def load_key(b64_key: str | None = None) -> bytes:
    """Load the 32-byte AES key from arg or ``SPARK_HIVE_RELAY_E2EE_KEY``."""
    raw = b64_key if b64_key is not None else os.environ.get(_KEY_ENV)
    if not raw:
        raise RelayCryptoError(f"no E2EE key: pass one or set ${_KEY_ENV} (base64 of 32 bytes)")
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise RelayCryptoError(f"E2EE key is not valid base64: {exc}") from exc
    if len(key) != 32:
        raise RelayCryptoError(f"E2EE key must decode to 32 bytes (AES-256), got {len(key)}")
    return key


def aad_for(kind: str, uuid: str) -> bytes:
    """Associated-data context binding a blob to its bucket prefix + job id.

    Passed to :func:`seal`/:func:`open_blob` so a ciphertext authenticated for
    e.g. ``pending/<uuid>`` cannot be replayed as ``results/<uuid>`` or moved to
    a different job — the GCM tag covers ``kind`` and ``uuid``, so any mismatch
    fails authentication. ``kind`` is one of ``pending``/``result``/``failed``.
    """
    return f"{kind}:{uuid}".encode("utf-8")


def _aad(context: bytes | None) -> bytes:
    # The framing header is always authenticated; per-object context (if any) is
    # appended so it is bound into the same tag.
    return _HEADER + (context or b"")


def seal(
    payload: dict[str, Any],
    key: bytes,
    *,
    aad: bytes | None = None,
    nonce: bytes | None = None,
) -> bytes:
    """Serialize ``payload`` to JSON and encrypt it. Returns the sealed blob.

    ``aad`` binds the ciphertext to a context (see :func:`aad_for`); the same
    value must be supplied to :func:`open_blob`. ``nonce`` is injectable for
    tests only; production always uses a fresh random 96-bit nonce (GCM requires
    nonce uniqueness per key).
    """
    if len(key) != 32:
        raise RelayCryptoError("key must be 32 bytes")
    if nonce is None:
        nonce = os.urandom(_NONCE_LEN)
    elif len(nonce) != _NONCE_LEN:
        raise RelayCryptoError(f"nonce must be {_NONCE_LEN} bytes")
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(aad))
    return _HEADER + nonce + ciphertext


def open_blob(blob: bytes, key: bytes, *, aad: bytes | None = None) -> dict[str, Any]:
    """Decrypt and JSON-decode a sealed blob. Inverse of :func:`seal`.

    ``aad`` must match the value passed to :func:`seal`, else authentication
    fails (defends against cross-prefix / cross-job replay).
    """
    if len(blob) < len(_HEADER) + _NONCE_LEN:
        raise RelayCryptoError("blob too short")
    if blob[: len(MAGIC)] != MAGIC:
        raise RelayCryptoError("bad magic — not a Spark relay blob")
    version = blob[len(MAGIC)]
    if version != VERSION:
        raise RelayCryptoError(f"unsupported relay version {version}")
    nonce = blob[len(_HEADER) : len(_HEADER) + _NONCE_LEN]
    ciphertext = blob[len(_HEADER) + _NONCE_LEN :]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(aad))
    except InvalidTag as exc:
        raise RelayCryptoError("authentication failed — wrong key or tampered blob") from exc
    return json.loads(plaintext)
