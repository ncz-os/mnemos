"""Unit tests for v6.2 M-2.2.1 Merkle inclusion proof helpers + endpoint scaffold."""

from __future__ import annotations

import pytest


from mnemos.audit.crypto import (
    merkle_leaf,
    merkle_proof,
    merkle_root,
    verify_inclusion,
)


def test_proof_pow2_8_leaves():
    leaves = [bytes([i]) * 32 for i in range(8)]
    root = merkle_root(leaves)
    for i in range(8):
        proof = merkle_proof(leaves, i)
        assert len(proof) == 3
        assert verify_inclusion(leaves[i], proof, root) is True


def test_proof_pow2_4_leaves():
    leaves = [bytes([0xA0 + i]) * 32 for i in range(4)]
    root = merkle_root(leaves)
    for i in range(4):
        proof = merkle_proof(leaves, i)
        assert len(proof) == 2
        assert verify_inclusion(leaves[i], proof, root) is True


def test_proof_pads_to_pow2():
    # 5 leaves -> pad to 8
    leaves = [bytes([0xB0 + i]) * 32 for i in range(5)]
    root = merkle_root(leaves)
    for i in range(5):
        proof = merkle_proof(leaves, i)
        assert len(proof) == 3
        assert verify_inclusion(leaves[i], proof, root) is True


def test_proof_single_leaf():
    leaf = b"\x42" * 32
    proof = merkle_proof([leaf], 0)
    assert proof == []
    # single-leaf tree: leaf is the root, empty proof verifies
    assert verify_inclusion(leaf, proof, leaf) is True


def test_proof_empty_input():
    assert merkle_proof([], 0) == []


def test_proof_index_out_of_range():
    with pytest.raises(IndexError):
        merkle_proof([b"\x00" * 32], 5)


def test_verify_rejects_wrong_leaf():
    leaves = [bytes([i]) * 32 for i in range(4)]
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, 1)
    # Same proof, different leaf -> reject
    assert verify_inclusion(b"\xff" * 32, proof, root) is False


def test_verify_rejects_wrong_root():
    leaves = [bytes([i]) * 32 for i in range(4)]
    proof = merkle_proof(leaves, 0)
    assert verify_inclusion(leaves[0], proof, b"\x00" * 32) is False


def test_verify_rejects_bad_position():
    leaves = [bytes([i]) * 32 for i in range(4)]
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, 0)
    # Corrupt one position to invalid value
    tampered = [(proof[0][0], "X")] + proof[1:]
    assert verify_inclusion(leaves[0], tampered, root) is False


def test_proof_uses_merkle_leaf_helper():
    """When the leaves are entry_id||signature hashes (what the audit
    chain actually stores), the proof + verify chain stays consistent."""
    entries = [(bytes([i]) * 16, bytes([i + 1]) * 64) for i in range(6)]
    leaves = [merkle_leaf(eid, sig) for eid, sig in entries]
    root = merkle_root(leaves)
    for i in range(6):
        proof = merkle_proof(leaves, i)
        assert verify_inclusion(leaves[i], proof, root) is True
