"""Reviewer findings on the metadata classifier depth cutoff and the provenance payload."""

from mnemos.core import persisted_text_classification as ptc
from mnemos.core import provenance

_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _nest(depth: int, leaf):
    node = leaf
    for _ in range(depth):
        node = {"k": node}
    return node


def test_token_within_the_depth_limit_vaults():
    any_vault, _, _ = ptc._classify_metadata_leaves(_nest(ptc._MAX_METADATA_WALK_DEPTH, _TOKEN))
    assert any_vault


def test_token_below_the_depth_limit_vaults_instead_of_being_skipped():
    any_vault, _, reasons = ptc._classify_metadata_leaves(_nest(ptc._MAX_METADATA_WALK_DEPTH + 3, _TOKEN))
    assert any_vault
    assert any("depth_exceeded" in r for r in reasons)


def test_shallow_clean_metadata_stays_clean():
    any_vault, redact, reasons = ptc._classify_metadata_leaves({"a": {"b": "plain"}})
    assert not any_vault and not redact and not reasons


def test_signature_payload_has_no_separator_collision():
    a = provenance._signature_payload(principal="alice\x1fadmin", key_id="k", at="t")
    b = provenance._signature_payload(principal="alice", key_id="admin\x1fk", at="t")
    assert a != b
    assert provenance._signature_payload(principal="p", key_id=None, at="t") != provenance._signature_payload(
        principal="p", key_id="", at="t2"
    )


def test_stamp_and_verify_round_trip_with_the_unambiguous_payload():
    meta = provenance.stamp_provenance({}, principal="alice", key_id="k1", sign_key="secret-key")
    assert provenance.verify_provenance.__name__
    import os

    os.environ["MNEMOS_PROVENANCE_HMAC_KEY"] = "secret-key"
    try:
        assert provenance.verify_provenance(meta) is True
        meta["_provenance"]["principal"] = "mallory"
        assert provenance.verify_provenance(meta) is False
    finally:
        os.environ.pop("MNEMOS_PROVENANCE_HMAC_KEY", None)
