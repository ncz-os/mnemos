"""Unit tests for v6.2 M-2.2.1 audit crypto primitives."""

from __future__ import annotations

import base64
import dataclasses
import os
import uuid
from datetime import datetime, timezone

import pytest

from mnemos.audit.crypto import (
    AuditEntry,
    canonical_entry_bytes,
    canonical_payload_hash,
    constant_time_eq,
    derive_writer_keypair,
    entry_hash,
    load_root_keypair,
    merkle_leaf,
    merkle_root,
    sign_entry,
    verify_entry,
)


def _new_entry(pub: bytes) -> AuditEntry:
    return AuditEntry(
        entry_id=uuid.uuid4().bytes,
        memory_id=uuid.uuid4().bytes,
        prev_entry_id=None,
        prev_entry_hash=None,
        op="create",
        payload_hash=b"\x00" * 32,
        writer_id="user-test",
        writer_pubkey=pub,
        signed_at=datetime.now(tz=timezone.utc).isoformat(),
    )


class TestPayloadHash:
    def test_deterministic(self) -> None:
        h1 = canonical_payload_hash(
            memory_id="m",
            content="hi",
            category="facts",
            subcategory=None,
            metadata={"a": 1},
            embedding=None,
        )
        h2 = canonical_payload_hash(
            memory_id="m",
            content="hi",
            category="facts",
            subcategory=None,
            metadata={"a": 1},
            embedding=None,
        )
        assert h1 == h2
        assert len(h1) == 32

    def test_metadata_key_order_invariant(self) -> None:
        h1 = canonical_payload_hash(
            memory_id="m",
            content="hi",
            category="facts",
            subcategory=None,
            metadata={"a": 1, "b": 2},
            embedding=None,
        )
        h2 = canonical_payload_hash(
            memory_id="m",
            content="hi",
            category="facts",
            subcategory=None,
            metadata={"b": 2, "a": 1},
            embedding=None,
        )
        assert h1 == h2

    def test_embedding_hash_in_payload(self) -> None:
        h_no_embed = canonical_payload_hash(
            memory_id="m",
            content="hi",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
        )
        h_with_embed = canonical_payload_hash(
            memory_id="m",
            content="hi",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=b"\x01" * 16,
        )
        assert h_no_embed != h_with_embed

    def test_changes_when_content_changes(self) -> None:
        h1 = canonical_payload_hash(
            memory_id="m",
            content="hi",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
        )
        h2 = canonical_payload_hash(
            memory_id="m",
            content="hi.",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
        )
        assert h1 != h2


class TestWriterKeyDerivation:
    def test_deterministic(self) -> None:
        ss = b"x" * 32
        _, pub1 = derive_writer_keypair(ss, "user-1")
        _, pub2 = derive_writer_keypair(ss, "user-1")
        assert pub1 == pub2

    def test_different_writers(self) -> None:
        ss = b"x" * 32
        _, pub1 = derive_writer_keypair(ss, "user-1")
        _, pub2 = derive_writer_keypair(ss, "user-2")
        assert pub1 != pub2

    def test_different_secrets(self) -> None:
        _, pub1 = derive_writer_keypair(b"a" * 32, "user-1")
        _, pub2 = derive_writer_keypair(b"b" * 32, "user-1")
        assert pub1 != pub2

    def test_empty_secret_rejected(self) -> None:
        with pytest.raises(ValueError):
            derive_writer_keypair(b"", "user-1")

    def test_empty_writer_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            derive_writer_keypair(b"x" * 32, "")


class TestSignVerify:
    def test_roundtrip(self) -> None:
        priv, pub = derive_writer_keypair(b"x" * 32, "u")
        ent = _new_entry(pub)
        sig = sign_entry(ent, priv)
        assert len(sig) == 64
        assert verify_entry(ent, sig) is True

    def test_tamper_op_rejected(self) -> None:
        priv, pub = derive_writer_keypair(b"x" * 32, "u")
        ent = _new_entry(pub)
        sig = sign_entry(ent, priv)
        tampered = dataclasses.replace(ent, op="delete")
        assert verify_entry(tampered, sig) is False

    def test_wrong_pubkey_rejected(self) -> None:
        priv, pub = derive_writer_keypair(b"x" * 32, "u")
        _, other_pub = derive_writer_keypair(b"x" * 32, "other")
        ent = _new_entry(pub)
        sig = sign_entry(ent, priv)
        assert verify_entry(ent, sig, writer_pubkey=other_pub) is False

    def test_short_sig_rejected(self) -> None:
        _, pub = derive_writer_keypair(b"x" * 32, "u")
        ent = _new_entry(pub)
        assert verify_entry(ent, b"\x00" * 10) is False


class TestRootKey:
    def test_load_roundtrip(self, monkeypatch) -> None:
        seed = os.urandom(32)
        monkeypatch.setenv(
            "MNEMOS_AUDIT_ROOT_PRIVKEY",
            base64.b64encode(seed).decode(),
        )
        priv, pub = load_root_keypair()
        assert len(pub) == 32
        sig = priv.sign(b"hello")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        Ed25519PublicKey.from_public_bytes(pub).verify(sig, b"hello")

    def test_unset_rejected(self, monkeypatch) -> None:
        monkeypatch.delenv("MNEMOS_AUDIT_ROOT_PRIVKEY", raising=False)
        with pytest.raises(ValueError, match="unset"):
            load_root_keypair()

    def test_wrong_length_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "MNEMOS_AUDIT_ROOT_PRIVKEY",
            base64.b64encode(b"x" * 16).decode(),
        )
        with pytest.raises(ValueError, match="32 bytes"):
            load_root_keypair()

    def test_bad_base64_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("MNEMOS_AUDIT_ROOT_PRIVKEY", "!!!not-b64!!!")
        with pytest.raises(ValueError):
            load_root_keypair()


class TestMerkle:
    def test_empty_returns_zero_root(self) -> None:
        assert merkle_root([]) == b"\x00" * 32

    def test_single_leaf(self) -> None:
        leaf = b"\x42" * 32
        # single-element tree: leaf is the root after zero-pad-to-1-then-no-combine
        assert merkle_root([leaf]) == leaf

    def test_power_of_two(self) -> None:
        leaves = [bytes([i]) * 32 for i in range(4)]
        root = merkle_root(leaves)
        assert len(root) == 32

    def test_pads_to_power_of_two(self) -> None:
        # 3 leaves → tree pads to 4. Root must differ from a 4-leaf
        # tree where the 4th leaf is a real value.
        a, b, c, d = [bytes([i]) * 32 for i in range(4)]
        root_3 = merkle_root([a, b, c])
        root_4_with_zero = merkle_root([a, b, c, b"\x00" * 32])
        assert root_3 == root_4_with_zero
        # And vs real 4th leaf:
        root_4_real = merkle_root([a, b, c, d])
        assert root_3 != root_4_real

    def test_leaf_helper(self) -> None:
        eid = b"\x01" * 16
        sig = b"\x02" * 64
        leaf = merkle_leaf(eid, sig)
        assert len(leaf) == 32


class TestEntryHashChain:
    def test_chains_two_entries(self) -> None:
        priv, pub = derive_writer_keypair(b"x" * 32, "u")
        e1 = _new_entry(pub)
        sig1 = sign_entry(e1, priv)
        h1 = entry_hash(e1, sig1)

        e2 = dataclasses.replace(
            _new_entry(pub),
            memory_id=e1.memory_id,
            prev_entry_id=e1.entry_id,
            prev_entry_hash=h1,
            op="update",
        )
        sig2 = sign_entry(e2, priv)
        # e2 signature is over canonical bytes that include prev_entry_hash;
        # any tamper of h1 in storage breaks e2's verify.
        assert verify_entry(e2, sig2) is True
        tampered = dataclasses.replace(e2, prev_entry_hash=b"\xff" * 32)
        assert verify_entry(tampered, sig2) is False


def test_canonical_entry_bytes_stable() -> None:
    priv, pub = derive_writer_keypair(b"x" * 32, "u")
    e = _new_entry(pub)
    b1 = canonical_entry_bytes(e)
    b2 = canonical_entry_bytes(e)
    assert b1 == b2
    # Sorted-keys property — replace by same key but different insertion
    # order in metadata-equivalent fields. The dataclass has no dicts in
    # the signed payload, so this is mostly a no-op safety net.
    assert b'"op":"create"' in b1


def test_constant_time_eq() -> None:
    assert constant_time_eq(b"abc", b"abc")
    assert not constant_time_eq(b"abc", b"abd")
    assert not constant_time_eq(b"abc", b"abcd")
