"""FB118 red-first tests: mnemos rework of FB114 after the structured review.

Each review finding was verified against the source and live probes on the
FB114 branch (tip ``71fe47fe``) BEFORE this test file was written:

* Finding 1 (provenance key read at import time) DOES NOT hold — the key is
  read at call time inside ``stamp_provenance``/``verify_provenance`` via
  ``provenance_signing_key()`` (probes: late env → signed; key rotation
  between stamp and verify flips the verdict). The probe is pinned as a
  test, not changed in production.
* Finding 2 (unsigned provenance blocks verify True when no signing key is
  configured — must fail CLOSED) HOLDS — ``verify_provenance`` answers
  ``block.get("sig") is None`` (=True) for unsigned blocks before any key
  check. The fail-closed tests below are red against FB114 as shipped.
* Finding 3 (unbounded ``json.loads`` in the metadata redaction path)
  HOLDS — ``redact_metadata_with_stored`` and ``row_to_memory`` parse
  caller/store-controlled strings without a size limit. Bounded-parse
  tests below are red until the helper lands.

Marker format matches the classifier's persisted marker (FB114 M4):
``secret_redact_metadata`` maps leaf paths (lists of keys) to mask.
"""

from __future__ import annotations

PROVENANCE_ENV = "MNEMOS_PROVENANCE_HMAC_KEY"

# REDACT-class value in metadata (verified against the classifier: hex64).
_HEX64 = "9ac9d6f2c13fe6f2a456d4ac6b7ce4b46a34c23f9d1e77c6a2b809f4ee1c5d64"

# A SMALL valid stored-redaction marker as a JSON string (the batch-worker
# input shape redact_metadata_with_stored must keep handling).
_SMALL_MARKER = '{"secret_redact_metadata": [["digest"]], "digest": "%s"}' % _HEX64

# Bound (fallback mirrors the production constant; the first test asserts the
# constant exists so tuning the real value does not break these tests).
_TEST_BOUND_FALLBACK = 1024 * 1024


def _meta_bound():
    from mnemos.domain import models as models_mod

    return getattr(models_mod, "_MAX_META_JSON_PARSE_BYTES", _TEST_BOUND_FALLBACK)


# ---------------------------------------------------------------------------
# Finding 1 — documented probe: the HMAC key is CALL-time (no fix).
# ---------------------------------------------------------------------------


class TestKeyIsReadAtCallTime:
    def test_bound_constant_exists(self):
        """The bounded-parse constant is a module constant (tunable, not magic)."""
        from mnemos.domain import models as models_mod

        assert getattr(models_mod, "_MAX_META_JSON_PARSE_BYTES", None) is not None

    def test_setting_key_after_import_stamps_signed(self, monkeypatch):
        """Key set AFTER import (and after an unsigned stamp) → the next stamp is signed."""
        from mnemos.core.provenance import PROVENANCE_KEY, provenance_signing_key, stamp_provenance

        monkeypatch.delenv(PROVENANCE_ENV, raising=False)
        assert provenance_signing_key() is None

        unsigned = stamp_provenance({"job_id": "J-0"}, principal="agent-x")
        assert unsigned[PROVENANCE_KEY]["signed"] is False

        monkeypatch.setenv(PROVENANCE_ENV, "key-set-after-import")
        signed = stamp_provenance({"job_id": "J-1"}, principal="agent-x")
        assert signed[PROVENANCE_KEY]["signed"] is True
        assert signed[PROVENANCE_KEY]["sig"]

    def test_rotation_between_stamp_and_verify_flips_verify(self, monkeypatch):
        """Rotating the key after stamping makes the old signature unverified."""
        from mnemos.core.provenance import stamp_provenance, verify_provenance

        monkeypatch.setenv(PROVENANCE_ENV, "rotate-key-a")
        meta = stamp_provenance({"job_id": "J-1"}, principal="agent-x")
        assert verify_provenance(meta) is True

        monkeypatch.setenv(PROVENANCE_ENV, "rotate-key-b")
        assert verify_provenance(meta) is False


# ---------------------------------------------------------------------------
# Finding 2 — fail closed: unsigned blocks NEVER verify.
# ---------------------------------------------------------------------------


class TestUnsignedBlocksFailClosed:
    def test_unsigned_without_key_verifies_false(self, monkeypatch):
        """No key configured + unsigned block → False, not True. RED at base."""
        from mnemos.core.provenance import PROVENANCE_KEY, stamp_provenance, verify_provenance

        monkeypatch.delenv(PROVENANCE_ENV, raising=False)
        block = stamp_provenance({"job_id": "J-1"}, principal="agent-x")[PROVENANCE_KEY]
        assert block["signed"] is False and block["sig"] is None
        assert verify_provenance({"job_id": "J-1", PROVENANCE_KEY: block}) is False

    def test_unsigned_with_key_verifies_false(self, monkeypatch):
        """A configured key cannot attest a block that carries no signature."""
        from mnemos.core.provenance import PROVENANCE_KEY, stamp_provenance, verify_provenance

        monkeypatch.setenv(PROVENANCE_ENV, "fb118-key-present")
        block = stamp_provenance({"job_id": "J-1"}, principal="agent-x")[PROVENANCE_KEY]
        forged_unsigned = dict(block, signed=False, sig=None)  # no attestation carried
        assert verify_provenance({"job_id": "J-1", PROVENANCE_KEY: forged_unsigned}) is False

    def test_unsigned_then_key_set_still_false(self, monkeypatch):
        """Configuring a key later does not retro-attest an unsigned block."""
        from mnemos.core.provenance import PROVENANCE_KEY, stamp_provenance, verify_provenance

        monkeypatch.delenv(PROVENANCE_ENV, raising=False)
        meta = stamp_provenance({"job_id": "J-1"}, principal="agent-x")
        assert meta[PROVENANCE_KEY]["signed"] is False
        monkeypatch.setenv(PROVENANCE_ENV, "key-turned-on-after-the-fact")
        assert verify_provenance(meta) is False

    def test_forged_mix_unsigned_with_sig_is_false(self, monkeypatch):
        """A forgery mixing signed=false with a non-null sig is rejected too."""
        from mnemos.core.provenance import PROVENANCE_KEY, stamp_provenance, verify_provenance

        monkeypatch.delenv(PROVENANCE_ENV, raising=False)
        block = stamp_provenance({"job_id": "J-1"}, principal="agent-x")[PROVENANCE_KEY]
        forged = dict(block, sig="attacker-junk")
        assert verify_provenance({"job_id": "J-1", PROVENANCE_KEY: forged}) is False


class TestSignedBlocksVerify:
    def test_signed_block_verifies(self, monkeypatch):
        from mnemos.core.provenance import stamp_provenance, verify_provenance

        monkeypatch.setenv(PROVENANCE_ENV, "fb118-signed-ok")
        assert verify_provenance(stamp_provenance({"job_id": "J-1"}, principal="agent-x")) is True

    def test_signed_block_with_lost_key_verifies_false(self, monkeypatch):
        """A signed block cannot be verified once the key is gone (fail closed, kept)."""
        from mnemos.core.provenance import stamp_provenance, verify_provenance

        monkeypatch.setenv(PROVENANCE_ENV, "fb118-sign-then-lose-key")
        meta = stamp_provenance({"job_id": "J-1"}, principal="agent-x")
        monkeypatch.delenv(PROVENANCE_ENV, raising=False)
        assert verify_provenance(meta) is False


# ---------------------------------------------------------------------------
# Finding 3 — bounded parses in the metadata redaction path.
# ---------------------------------------------------------------------------


class TestRedactionParsesAreBounded:
    def test_bounded_helper_contract(self):
        """_bounded_meta_json_loads: parses small valid dicts, refuses over-bound."""
        from mnemos.domain.models import _bounded_meta_json_loads

        bound = _meta_bound()
        oversized = '{"filler": "%s"}' % ("x" * (bound + 1))
        assert _bounded_meta_json_loads(_SMALL_MARKER) == {
            "secret_redact_metadata": [["digest"]],
            "digest": _HEX64,
        }
        assert _bounded_meta_json_loads(oversized) is None
        assert _bounded_meta_json_loads("not json at all") is None

    def test_small_marker_still_masks(self):
        """The normal string path is untouched: parse + mask the flagged leaf."""
        from mnemos.domain.models import redact_metadata_with_stored

        out = redact_metadata_with_stored(_SMALL_MARKER, _SMALL_MARKER)
        assert out["digest"] == "[REDACTED]"

    def test_oversized_valid_marker_is_refused_untouched(self):
        """An over-bound VALID marker is returned untouched (parse refused).

        RED at base: the unbounded parse succeeds, the marker is applied and a
        dict (with the leaf masked) comes back instead of the raw string.
        """
        from mnemos.domain.models import redact_metadata_with_stored

        prefix = '{"secret_redact_metadata": [["digest"]], "digest": "%s", "filler": "' % _HEX64
        filler = "x" * (_meta_bound() + 64 - len(prefix) - len('"}'))
        assert len(prefix) + len(filler) + 2 > _meta_bound()
        oversized = prefix + filler + '"}'
        out = redact_metadata_with_stored(oversized, oversized)
        assert not isinstance(out, dict), "over-bound string was parsed and masked"
        assert out == oversized

    def test_string_marker_masks_where_base_leaked(self):
        """The (str, str) and (dict, str) shapes mask instead of leaking.

        Verified against the base tree (``71fe47fe``): both shapes came back
        with the digest in the clear, because the marker lookup ran on the
        still-string ``stored_meta`` and never found a path list. FB118 parses
        the stored marker too, so these now mask. Pinned here because it is
        the direction the rework moved (mask MORE, never less).
        """
        from mnemos.domain.models import redact_metadata_with_stored

        assert redact_metadata_with_stored(_SMALL_MARKER, _SMALL_MARKER)["digest"] == "[REDACTED]"
        as_dict = {"secret_redact_metadata": [["digest"]], "digest": _HEX64}
        assert redact_metadata_with_stored(as_dict, _SMALL_MARKER)["digest"] == "[REDACTED]"

    def test_over_bound_metadata_never_reaches_the_reader(self):
        """An over-bound metadata column is dropped, not returned unmasked.

        The bound refuses the parse, so no marker is consulted; the guarantee
        is that the blob itself never lands on the item — in the single-row
        arm AND the batch arm (structured review finding 2, verified).
        """
        import asyncio

        from mnemos.domain.models import DEFAULT_REDACT_THREAD_THRESHOLD_BYTES, row_to_memory, rows_to_memories

        prefix = '{"digest": "%s", "filler": "' % _HEX64
        filler = "x" * (_meta_bound() + 64 - len(prefix) - len('"}'))
        oversized = prefix + filler + '"}'
        assert len(oversized) > DEFAULT_REDACT_THREAD_THRESHOLD_BYTES

        row = {
            "id": "m-fb118",
            "content": "note",
            "category": "note",
            "created": "2026-11-02T00:00:00Z",
            "namespace": "default",
            "metadata": oversized,
        }
        single = row_to_memory(row, redact_secrets=True)
        assert single.metadata is None
        assert _HEX64 not in str(single.metadata)

        batched = asyncio.run(rows_to_memories([row], redact_secrets=True))
        assert batched[0].metadata is None
        assert _HEX64 not in str(batched[0].metadata)
