"""CHARON MIF bundle export/import (directory of concept files + manifest)."""

from __future__ import annotations

import json

from mnemos.portability import charon, mif


def _memories():
    return [
        {
            "id": "mem_1782675392144_3ba026",
            "content": "MNEMOS adopts MIF natively.",
            "category": "decisions",
            "subcategory": "architecture",
            "namespace": "default",
            "created": "2026-06-28T20:00:00+00:00",
            "updated": "2026-06-28T20:00:00+00:00",
            "source_agent": "claude",
            "quality_rating": 85,
        },
        {
            "id": "mem_1782675392200_run01",
            "content": "Restart the gateway, then re-run the migration.",
            "category": "rules",
            "namespace": "default",
            "created": "2026-06-28T20:01:00+00:00",
            "mif_type": "procedural",
        },
    ]


def test_export_layout_and_manifest(tmp_path):
    manifest = charon.export_bundle(_memories(), tmp_path)
    assert manifest["count"] == 2
    assert manifest["mif_version"] == "1.0.0"
    # files laid out by conceptType/<uuid>.md
    assert (tmp_path / charon.MANIFEST_NAME).is_file()
    assert (tmp_path / "semantic").is_dir()  # 'decisions' -> semantic
    assert (tmp_path / "procedural").is_dir()  # explicit mif_type
    md_files = list(tmp_path.rglob("*.md"))
    assert len(md_files) == 2
    # manifest entries point at real files
    for entry in manifest["concepts"]:
        assert (tmp_path / entry["path"]).is_file()
        assert entry["@id"].startswith("urn:mif:")


def test_round_trip_bundle_preserves_memories(tmp_path):
    src = _memories()
    charon.export_bundle(src, tmp_path)
    out = charon.import_bundle(tmp_path)
    by_id = {m["id"]: m for m in out}
    assert set(by_id) == {m["id"] for m in src}
    a = by_id["mem_1782675392144_3ba026"]
    assert a["content"] == "MNEMOS adopts MIF natively."
    assert a["category"] == "decisions"
    assert a["mif_type"] == "semantic"
    b = by_id["mem_1782675392200_run01"]
    assert b["mif_type"] == "procedural"


def test_export_is_schema_validated(tmp_path):
    # Every written concept conforms to the published MIF schema.
    charon.export_bundle(_memories(), tmp_path)
    for md in tmp_path.rglob("*.md"):
        concept = mif.markdown_to_concept(md.read_text())
        assert mif.validate_concept(concept) == []


def test_import_without_manifest_walks_md(tmp_path):
    charon.export_bundle(_memories(), tmp_path)
    (tmp_path / charon.MANIFEST_NAME).unlink()  # hand-authored dir, no manifest
    out = charon.import_bundle(tmp_path)
    assert len(out) == 2


def test_vault_memory_redacted_in_bundle(tmp_path):
    mems = _memories() + [
        {
            "id": "mem_vault_x",
            "content": "ROOT_PW=DenylistSelfTest@NotARealSecret1",
            "category": "infrastructure",
            "namespace": "vault",
            "created": "2026-06-28T20:02:00+00:00",
        }
    ]
    charon.export_bundle(mems, tmp_path)
    blob = "\n".join(p.read_text() for p in tmp_path.rglob("*.md"))
    assert "DenylistSelfTest@NotARealSecret1" not in blob
    assert mif.VAULT_REDACTED_BODY in blob


def test_manifest_is_valid_json(tmp_path):
    charon.export_bundle(_memories(), tmp_path)
    data = json.loads((tmp_path / charon.MANIFEST_NAME).read_text())
    assert data["schema"].endswith("mif.schema.json")
    assert len(data["concepts"]) == 2
