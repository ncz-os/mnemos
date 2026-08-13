"""MIF 1.0 native portability — mapping + lossless round-trip + conformance.

Covers Phase 0+1 of the MNEMOS→native-MIF adoption (ADR mem_1782679514682_85c817):
the MNEMOS↔MIF concept mapping, schema validity against the published MIF
JSON Schema, the lossless Markdown↔JSON-LD round-trip MIF requires, vault
redaction, and the provenance (#85) mapping.
"""

from __future__ import annotations


import pytest

from mnemos.portability import mif


def _memory(**over):
    base = {
        "id": "mem_1782675392144_3ba026",
        "content": "MNEMOS adopts MIF natively.\n\n## Notes\nFull Level 3 support.",
        "category": "decisions",
        "subcategory": "architecture",
        "namespace": "default",
        "created": "2026-06-28T20:00:00+00:00",
        "updated": "2026-06-28T20:05:00+00:00",
        "source": "agent",
        "source_agent": "claude",
        "source_provider": "anthropic",
        "source_model": "opus",
        "source_session": "sess-1",
        "quality_rating": 85,
        "embedding_model": "nomic-embed-text",
        "embedding_dim": 768,
        "compressed_content": "MNEMOS goes native MIF L3.",
        "owner_id": "default",
        "permission_mode": 600,
    }
    base.update(over)
    return base


def test_concept_is_schema_valid():
    c = mif.memory_to_concept(_memory())
    assert mif.validate_concept(c) == [], "concept must validate against the published MIF schema"
    # required JSON-LD keys present
    for k in ("@context", "@type", "@id", "conceptType", "content", "created"):
        assert k in c


def test_id_is_deterministic_uuid5_urn():
    a = mif.memory_to_concept(_memory())["@id"]
    b = mif.memory_to_concept(_memory())["@id"]
    assert a == b, "same mnemos id must yield the same MIF @id"
    assert a.startswith("urn:mif:")
    # different id → different uuid
    assert mif.memory_to_concept(_memory(id="mem_other_9"))["@id"] != a


def test_markdown_round_trip_is_lossless():
    c = mif.memory_to_concept(_memory())
    md = mif.concept_to_markdown(c)
    c2 = mif.markdown_to_concept(md)
    assert c2 == c, "JSON-LD → Markdown → JSON-LD must be byte-for-structure identical"
    # the .md is a real frontmatter file with the content as body
    assert md.startswith("---\n")
    assert "MNEMOS adopts MIF natively." in md


def test_memory_round_trip_preserves_identity_and_taxonomy():
    mem = _memory()
    m2 = mif.concept_to_memory(mif.markdown_to_concept(mif.concept_to_markdown(mif.memory_to_concept(mem))))
    assert m2["id"] == mem["id"]
    assert m2["category"] == mem["category"]
    assert m2["subcategory"] == mem["subcategory"]
    assert m2["content"] == mem["content"]
    assert m2["namespace"] == mem["namespace"]
    assert m2["mif_type"] == "semantic"


def test_native_mif_type_overrides_category_map():
    # category 'decisions' maps to semantic; an explicit episodic must win.
    c = mif.memory_to_concept(_memory(mif_type="episodic"))
    assert c["conceptType"] == "episodic"


def test_category_type_migration_fallback():
    assert mif.category_to_mif_type("git_commit") == "episodic"
    assert mif.category_to_mif_type("rules") == "procedural"
    assert mif.category_to_mif_type("facts") == "semantic"
    assert mif.category_to_mif_type("totally-unknown") == "semantic"


def test_provenance_maps_source_fields():
    c = mif.memory_to_concept(_memory())
    prov = c["provenance"]
    assert prov["agent"] == "claude"
    assert prov["agentVersion"] == "anthropic/opus"
    assert prov["sourceType"] == "agent_inferred"
    assert prov["sourceRef"] == "sess-1"
    assert prov["confidence"] == pytest.approx(0.85)


def test_vault_memory_never_emits_secret_content():
    c = mif.memory_to_concept(_memory(id="mem_vault_1", namespace="vault", content="ROOT_PW=DenylistSelfTest@NotARealSecret1"))
    assert c["content"] == mif.VAULT_REDACTED_BODY
    assert "DenylistSelfTest@NotARealSecret1" not in mif.concept_to_markdown(c)
    assert c["provenance"]["sourceRef"] == mif.VAULT_REDACTED_REF
    assert "sourceText" not in c.get("embedding", {}), "vault embedding must not carry source text"
    assert "summary" not in c, "vault must not emit a plaintext compression summary"
    assert mif.validate_concept(c) == []


def test_vault_redaction_can_be_disabled_for_authorized_export():
    c = mif.memory_to_concept(_memory(id="mem_v2", namespace="vault", content="secret"), redact_vault=False)
    assert c["content"] == "secret"


def test_foreign_concept_import_without_mnemos_extension():
    # A concept authored elsewhere (no properties.mnemos) still imports.
    c = mif.memory_to_concept(_memory())
    c.pop("properties", None)
    m = mif.concept_to_memory(c)
    assert "id" not in m, "foreign concept leaves id for the caller to assign"
    assert m["category"] == "decisions"  # recovered from tags
    assert m["mif_type"] == "semantic"


def test_compression_summary_is_level3():
    c = mif.memory_to_concept(_memory())
    assert c["summary"] == "MNEMOS goes native MIF L3."


def test_minimal_memory_still_valid():
    # Only the bare minimum fields — must still produce a schema-valid concept.
    c = mif.memory_to_concept({"id": "mem_min_1", "content": "hi", "created": "2026-06-28T00:00:00+00:00"})
    assert mif.validate_concept(c) == []
    assert c["conceptType"] == "semantic"  # no category → safe default


def test_markdown_rejects_missing_frontmatter():
    with pytest.raises(ValueError):
        mif.markdown_to_concept("no frontmatter here")


def test_metadata_mif_type_is_authoritative_over_category():
    """Phase 2a: a persisted metadata.mif_type pins the base type (all-backend,
    no schema migration) over the category fallback."""
    # category 'facts' -> semantic by the map; metadata pins it episodic.
    c = mif.memory_to_concept(
        _memory(category="facts", mif_type=None, metadata={"mif_type": "episodic"})
    )
    assert c["conceptType"] == "episodic"


def test_explicit_mif_type_field_beats_metadata():
    c = mif.memory_to_concept(
        _memory(mif_type="procedural", metadata={"mif_type": "episodic"})
    )
    assert c["conceptType"] == "procedural"


def test_metadata_json_string_mif_type_resolves():
    import json as _json

    c = mif.memory_to_concept(_memory(category="facts", mif_type=None, metadata=_json.dumps({"mif_type": "procedural"})))
    assert c["conceptType"] == "procedural"
