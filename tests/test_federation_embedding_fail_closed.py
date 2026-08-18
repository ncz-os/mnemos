"""An unverifiable peer vector must be declined, never adopted.

Federation may copy a peer's embedding instead of re-computing it, but only
when the two nodes are known to share a vector space. The gate compared the
peer's ``embedding_model`` / ``embedding_dim`` against the local ones -- and
guarded each comparison on the LOCAL value being truthy:

    if local_embed_model and emb_model != local_embed_model:
    elif embed_dim_expected and emb_dim and emb_dim != embed_dim_expected:
    else:
        <adopt the peer's vector>

So a node that could not resolve its own embedder skipped both comparisons and
fell through to the accept branch, taking ANY peer's vector. That is live on
the fleet: several hosts set neither ``MNEMOS_EMBED_HTTP_MODEL`` nor
``providers.inference_embed_model``, leaving ``local_embed_model`` None.

Vectors are backend-specific artifacts -- engine and quantisation move them,
not just the model name -- so adopting an unverifiable one silently mixes two
spaces in a single collection. An unknown fingerprint is unverifiable, not
compatible: fail closed. The memory content lands regardless; only the vector
is declined, and a local re-embed fills it in this node's own space.

These tests pin the decision rule directly, mirroring the gate, so they need no
live peer.
"""

from __future__ import annotations

import pytest


def _adopts(*, emb_model, emb_dim, local_embed_model, embed_dim_expected) -> bool:
    """Mirror of the gate in domain/federation.py: adopt the peer vector?"""
    if not local_embed_model or not embed_dim_expected:
        return False
    if emb_model != local_embed_model:
        return False
    if not emb_dim or emb_dim != embed_dim_expected:
        return False
    return True


def test_matching_fingerprint_is_adopted():
    """Two nodes that agree still share vectors for free."""
    assert _adopts(
        emb_model="bge-m3", emb_dim=1024, local_embed_model="bge-m3", embed_dim_expected=1024
    )


@pytest.mark.parametrize(
    ("local_model", "local_dim", "why"),
    [
        (None, 1024, "local model unresolved"),
        ("bge-m3", None, "local dim unresolved"),
        (None, None, "neither resolved"),
        ("", 1024, "local model empty string"),
        ("bge-m3", 0, "local dim zero"),
    ],
)
def test_unresolved_local_embedder_declines_the_vector(local_model, local_dim, why):
    """The regression: an unconfigured receiver must not accept blindly."""
    assert not _adopts(
        emb_model="bge-m3",
        emb_dim=1024,
        local_embed_model=local_model,
        embed_dim_expected=local_dim,
    ), f"fail-open: adopted a peer vector when {why}"


@pytest.mark.parametrize(
    ("peer_model", "peer_dim"),
    [
        ("nomic-embed-text-v1.5", 1024),
        ("bge-large-en-v1.5", 1024),
        ("bge-m3", 768),
        ("bge-m3", None),
        (None, 1024),
    ],
    ids=["other-model", "same-dim-other-model", "same-model-other-dim", "no-dim", "no-model"],
)
def test_mismatched_or_missing_peer_fingerprint_declines(peer_model, peer_dim):
    """Includes the 1024-dim look-alike: same width, different vector space."""
    assert not _adopts(
        emb_model=peer_model,
        emb_dim=peer_dim,
        local_embed_model="bge-m3",
        embed_dim_expected=1024,
    )


def test_the_exact_fleet_configuration_that_failed_open():
    """ACHILLES/minos: no local embed model resolved, peer offers bge-m3/1024."""
    assert not _adopts(
        emb_model="bge-m3", emb_dim=1024, local_embed_model=None, embed_dim_expected=1024
    ), "a host with an unresolved embedder must re-embed, not adopt"
