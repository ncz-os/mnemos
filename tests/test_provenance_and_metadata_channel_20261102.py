"""FB114 red-first tests: metadata-channel classification (audit M4) and
server-stamped provenance for status records (audit M3).

Written against mnemos master ``dd74b5f`` where:
  * caller ``metadata`` is carried through ``classify_persisted_text_fields``
    UNCLASSIFIED and returned verbatim to a non-root reader (M4);
  * status records carry only caller-asserted ``source_agent``-style columns
    and nothing binding them to the authenticated principal (M3).

Values in these tests were verified against the classifier itself:
``ghp_…40`` → VAULT (github_classic); a 64-hex string → REDACT (hex64).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers that the fix will provide; failing imports keep the file red until
# the implementation lands.
# ---------------------------------------------------------------------------

_TOK = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"  # VAULT-class (github_classic)
_HEX64 = "9ac9d6f2c13fe6f2a456d4ac6b7ce4b46a34c23f9d1e77c6a2b809f4ee1c5d64"  # REDACT (hex64)


def _classif(metadata, namespace="default"):
    from mnemos.core.persisted_text_classification import classify_persisted_text_fields

    return classify_persisted_text_fields(
        content="status checkpoint body",
        verbatim_content=None,
        metadata=metadata,
        namespace=namespace,
        classified_at="test",
        memory_id="mem_test",
    )


def _principal_classif(metadata, namespace="default"):
    from mnemos.core.persisted_text_classification import classify_persisted_text_fields

    return classify_persisted_text_fields(
        content="status checkpoint body",
        verbatim_content=None,
        metadata=metadata,
        namespace=namespace,
        classified_at="test",
        memory_id="mem_test",
        principal="agent-7",
        api_key_id="key-1",
    )


def _redacted_meta(metadata):
    """Simulate the non-root read path: ingest metadata goes in, redacted comes out."""
    from mnemos.core.persisted_text_classification import classify_persisted_text_fields
    from mnemos.domain.models import redact_metadata_with_stored

    classified = classify_persisted_text_fields(
        content="status checkpoint body",
        verbatim_content=None,
        metadata=metadata,
        namespace="default",
        classified_at="test",
    )
    return redact_metadata_with_stored(classified.metadata, classified.metadata)


# ---------------------------------------------------------------------------
# M4 — the metadata channel must be classified and redacted like content
# ---------------------------------------------------------------------------


class TestMetadataChannelClassified:
    def test_vault_leaf_in_metadata_fails_closed(self):
        """A ghp_ token in metadata vaults the record (pre-fix: reaches readers verbatim)."""
        res = _classif({"review_notes": _TOK})
        assert res.vaulted is True
        assert res.namespace == "vault"

    def test_redact_leaf_recorded_for_read_masking(self):
        """A REDACT-class string leaf in metadata is recorded for read-time masking."""
        res = _classif({"digest": _HEX64})
        assert not res.vaulted
        paths = res.metadata.get("secret_redact_metadata", [])
        assert paths, res.metadata
        # path recorded as a JSON-safe list of keys/indices
        assert paths[0] == ["digest"]

    def test_read_side_masks_redact_leaf(self):
        purple = _redacted_meta({"digest": _HEX64})
        assert purple["digest"] == "[REDACTED]"

    def test_read_side_masks_nested_leaf(self):
        purple = _redacted_meta({"status": {"job": {"digest": _HEX64}}})
        inner = purple["status"]["job"]
        assert inner["digest"] == "[REDACTED]"

    def test_typed_status_keys_are_exempt(self):
        """Status fields (audit M2 shapes) never trip the classifier via metadata."""
        meta = {
            "job_id": "J-1202608",
            "head_sha": _HEX64,
            "branch": "feature/refactor-core-Ab3Kx9c",
            "verdict": "approve",
            "phase": "import",
            "reviewer_model": "nemotron/archive/opencv",
        }
        res = _classif(meta)
        assert not res.vaulted
        assert not res.metadata.get("secret_reasons"), res.metadata.get("secret_reasons")
        assert not res.metadata.get("secret_redact_metadata")
        assert res.metadata["job_id"] == "J-1202608"

    def test_typed_key_with_vault_shaped_value_is_NOT_exempt(self):
        """A token smuggled into a typed field fails the shape test → classified."""
        res = _classif({"branch": _TOK})
        assert res.vaulted is True

    def test_exempt_key_with_wrong_shape_is_classified(self):
        """"sha" column carrying 500-hex is not a 64-hex value → classified."""
        res = _classif({"head_sha": _HEX64 + "ff" * 10})
        assert res.metadata.get("secret_redact_metadata") == [["head_sha"]]

    def test_non_string_leaves_skipped(self):
        res = _classif({"retry_count": 3, "ok": True, "notes": None, "tags": ["a", "b"]})
        assert not res.vaulted
        assert not res.metadata.get("secret_reasons")


# ---------------------------------------------------------------------------
# M3 — server-stamped provenance bound to the authenticated caller
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_stamp_binds_authenticated_principal(self, monkeypatch):
        from mnemos.core.provenance import PROVENANCE_KEY, verify_provenance

        # FB118: verify is fail closed — unsigned blocks are False — so this
        # principal-binding check stamps and verifies under a configured key.
        monkeypatch.setenv("MNEMOS_PROVENANCE_HMAC_KEY", "fb114-test-key")
        res = _principal_classif({"job_id": "J-1"})
        block = res.metadata[PROVENANCE_KEY]
        assert block["principal"] == "agent-7"
        assert block["key_id"] == "key-1"
        assert block.get("at")
        assert verify_provenance(res.metadata) is True

    def test_caller_forgery_is_stripped(self, monkeypatch):
        from mnemos.core.provenance import PROVENANCE_KEY, verify_provenance

        # FB118: same fail-closed re-point as above (keyed stamp/verify).
        monkeypatch.setenv("MNEMOS_PROVENANCE_HMAC_KEY", "fb114-test-key")
        res = _principal_classif(
            {
                "job_id": "J-1",
                PROVENANCE_KEY: {"principal": "another-agent", "sig": "forged"},
            }
        )
        block = res.metadata[PROVENANCE_KEY]
        assert block["principal"] == "agent-7"  # server-stamped, not the caller's
        assert verify_provenance(res.metadata) is True

    def test_verify_fails_on_tamper(self, monkeypatch):
        from mnemos.core.provenance import PROVENANCE_KEY, verify_provenance

        monkeypatch.setenv("MNEMOS_PROVENANCE_HMAC_KEY", "fb114-test-key")
        res = _principal_classif({"job_id": "J-1"})
        meta = dict(res.metadata)
        meta[PROVENANCE_KEY] = dict(meta[PROVENANCE_KEY], principal="someone-else")
        assert verify_provenance(meta) is False

    def test_unsigned_mode_when_no_key(self, monkeypatch):
        from mnemos.core.provenance import PROVENANCE_KEY

        monkeypatch.delenv("MNEMOS_PROVENANCE_HMAC_KEY", raising=False)
        res = _principal_classif({"job_id": "J-1"})
        block = res.metadata[PROVENANCE_KEY]
        assert block["principal"] == "agent-7"
        assert block.get("signed") is False
        assert block.get("sig") is None

    def test_unstamped_when_no_principal(self):
        """Peer/federation writes have no caller principal: nothing stamped."""
        from mnemos.core.provenance import PROVENANCE_KEY

        res = _classif({"job_id": "J-1"})
        assert PROVENANCE_KEY not in res.metadata


# ---------------------------------------------------------------------------
# Route wiring guard — the four authenticated write routes must pass the
# caller's principal into the classifier (source-level, like
# test_portability_v02_emission.py's guards; driving the whole app lifespan
# needs a live backend and stays out of scope here).
# ---------------------------------------------------------------------------


def test_authenticated_write_routes_pass_principal():
    import inspect

    from mnemos.api.routes import memories as memories_routes

    src = inspect.getsource(memories_routes)
    import re

    # The create, bulk-create and PATCH classification calls in the memory
    # routes must carry the caller's principal (M3). Count principal= in the
    # file; a later refactor that drops the wiring fails this guard.
    calls = len(re.findall(r"classify_persisted_text_fields\(", src))
    stamped = len(re.findall(r"principal=user\.user_id", src))
    assert calls >= 3, src
    assert stamped >= 3, "missing principal= at one of the three route call sites"


def test_session_ingest_route_passes_principal():
    import inspect

    from mnemos.api.routes import ingest as ingest_routes

    src = inspect.getsource(ingest_routes)
    assert "principal=user.user_id" in src
