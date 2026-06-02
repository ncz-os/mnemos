"""Unit tests for the relay E2EE layer. Pure (no network, no GCS)."""

from __future__ import annotations

import base64
import os

import pytest

from spark_relay import relay_crypto


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


def test_seal_open_roundtrip(key: bytes) -> None:
    payload = {"job_id": "abc", "prompt": "hi", "context": [{"id": 1, "content": "x"}]}
    blob = relay_crypto.seal(payload, key)
    assert blob[:4] == relay_crypto.MAGIC
    assert relay_crypto.open_blob(blob, key) == payload


def test_wrong_key_rejected(key: bytes) -> None:
    blob = relay_crypto.seal({"a": 1}, key)
    with pytest.raises(relay_crypto.RelayCryptoError, match="authentication failed"):
        relay_crypto.open_blob(blob, os.urandom(32))


def test_tampered_ciphertext_rejected(key: bytes) -> None:
    blob = bytearray(relay_crypto.seal({"a": 1}, key))
    blob[-1] ^= 0xFF  # flip a tag bit
    with pytest.raises(relay_crypto.RelayCryptoError, match="authentication failed"):
        relay_crypto.open_blob(bytes(blob), key)


def test_tampered_header_rejected(key: bytes) -> None:
    blob = bytearray(relay_crypto.seal({"a": 1}, key))
    blob[4] = 0x09  # bump version inside the AAD-protected header
    with pytest.raises(relay_crypto.RelayCryptoError):
        relay_crypto.open_blob(bytes(blob), key)


def test_bad_magic_rejected(key: bytes) -> None:
    with pytest.raises(relay_crypto.RelayCryptoError, match="bad magic"):
        relay_crypto.open_blob(b"XXXX\x01" + b"\x00" * 32, key)


def test_short_blob_rejected(key: bytes) -> None:
    with pytest.raises(relay_crypto.RelayCryptoError, match="too short"):
        relay_crypto.open_blob(b"SHR1", key)


def test_load_key_from_env(monkeypatch: pytest.MonkeyPatch, key: bytes) -> None:
    monkeypatch.setenv("SPARK_HIVE_RELAY_E2EE_KEY", base64.b64encode(key).decode())
    assert relay_crypto.load_key() == key


def test_load_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPARK_HIVE_RELAY_E2EE_KEY", raising=False)
    with pytest.raises(relay_crypto.RelayCryptoError, match="no E2EE key"):
        relay_crypto.load_key()


def test_load_key_wrong_length(key: bytes) -> None:
    with pytest.raises(relay_crypto.RelayCryptoError, match="32 bytes"):
        relay_crypto.load_key(base64.b64encode(b"short").decode())


def test_seal_rejects_bad_nonce(key: bytes) -> None:
    with pytest.raises(relay_crypto.RelayCryptoError, match="nonce"):
        relay_crypto.seal({"a": 1}, key, nonce=b"short")


def test_distinct_nonces_per_seal(key: bytes) -> None:
    a = relay_crypto.seal({"a": 1}, key)
    b = relay_crypto.seal({"a": 1}, key)
    assert a != b  # random nonce => different ciphertext for identical plaintext


def test_aad_roundtrip(key: bytes) -> None:
    aad = relay_crypto.aad_for("pending", "u1")
    blob = relay_crypto.seal({"x": 1}, key, aad=aad)
    assert relay_crypto.open_blob(blob, key, aad=aad) == {"x": 1}


def test_aad_cross_prefix_rejected(key: bytes) -> None:
    """A pending blob must not authenticate as a result blob (anti-replay)."""
    blob = relay_crypto.seal({"x": 1}, key, aad=relay_crypto.aad_for("pending", "u1"))
    with pytest.raises(relay_crypto.RelayCryptoError, match="authentication failed"):
        relay_crypto.open_blob(blob, key, aad=relay_crypto.aad_for("result", "u1"))


def test_aad_cross_uuid_rejected(key: bytes) -> None:
    blob = relay_crypto.seal({"x": 1}, key, aad=relay_crypto.aad_for("pending", "u1"))
    with pytest.raises(relay_crypto.RelayCryptoError, match="authentication failed"):
        relay_crypto.open_blob(blob, key, aad=relay_crypto.aad_for("pending", "u2"))


def test_missing_aad_rejected(key: bytes) -> None:
    blob = relay_crypto.seal({"x": 1}, key, aad=relay_crypto.aad_for("pending", "u1"))
    with pytest.raises(relay_crypto.RelayCryptoError):
        relay_crypto.open_blob(blob, key)
