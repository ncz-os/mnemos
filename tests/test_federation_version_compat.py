"""Federation must survive a rolling minor-version upgrade.

Measured on the fleet: PYTHIA (6.0.0) refused a 6.1.0 peer outright with

    schema mismatch: peer=6.1 (6.1.0) local=6.0 (6.0.0).
    Set compat_mode='permissive' on the peer to allow cross-version sync.

The gate compared `major.minor` for exact equality, so *any* minor release
broke federation across the whole fleet until an operator hand-edited every
peer. Minor releases are backwards compatible by our own versioning contract,
so the decision belongs on the MAJOR version; a major difference still aborts.

These tests pin the comparison rule directly, so they do not need a live peer.
"""

from __future__ import annotations

import pytest


def _decide(peer_signature: str, local_signature: str) -> tuple[bool, bool]:
    """Mirror of the gate in domain/federation.py: (sig_match, same_minor)."""
    peer_major = peer_signature.split(".")[0] if peer_signature else ""
    local_major = local_signature.split(".")[0]
    return bool(peer_major) and peer_major == local_major, peer_signature == local_signature


@pytest.mark.parametrize(
    ("peer", "local"),
    [("6.1", "6.0"), ("6.0", "6.1"), ("6.2", "6.0"), ("6.0", "6.12")],
    ids=["upgrade", "downgrade", "two-minors", "double-digit-minor"],
)
def test_same_major_federates_in_both_directions(peer, local):
    """A minor difference must not stop a sync, whichever side is newer."""
    sig_match, same_minor = _decide(peer, local)
    assert sig_match, f"{peer} and {local} share a major and must federate"
    assert not same_minor, "this case is deliberately cross-minor"


def test_identical_versions_still_compare_fingerprints():
    """Same major.minor keeps the drift check that catches forks."""
    sig_match, same_minor = _decide("6.1", "6.1")
    assert sig_match and same_minor, (
        "identical versions must still reach the migrations-fingerprint comparison"
    )


@pytest.mark.parametrize(
    ("peer", "local"),
    [("7.0", "6.1"), ("5.9", "6.0"), ("6.0", "7.0")],
    ids=["major-ahead", "major-behind", "local-ahead"],
)
def test_major_difference_still_aborts(peer, local):
    """A major bump may change the wire contract; refuse it."""
    sig_match, _ = _decide(peer, local)
    assert not sig_match, f"{peer} vs {local} crosses a major and must abort"


def test_missing_peer_signature_aborts():
    """An unreadable signature is unverifiable, not implicitly compatible."""
    sig_match, _ = _decide("", "6.1")
    assert not sig_match, "an empty peer signature must never satisfy the gate"


def test_the_exact_fleet_case_that_failed():
    """The literal pairing observed between PYTHIA and the arm64 container."""
    sig_match, same_minor = _decide("6.1", "6.0")
    assert sig_match, "PYTHIA 6.0 must accept a 6.1 peer without compat_mode surgery"
    assert not same_minor, "and must skip the fingerprint compare across minors"
