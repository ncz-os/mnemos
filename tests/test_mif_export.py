"""MIF bundle export via the CHARON CLI tool (`mnemos.tools.memory_export mif`).

Exercises cmd_mif end-to-end with the API fetch mocked — it feeds flat memory
rows into the mnemos-core CHARON MIF bundle primitive.
"""

from __future__ import annotations

import argparse
import json

from mnemos.tools import memory_export


def _args(out, **over):
    base = dict(
        endpoint="http://localhost:5002",
        api_key=None,
        category=None,
        limit=10_000,
        owner_id=None,
        namespace=None,
        out=str(out),
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_mif_writes_a_bundle(tmp_path, monkeypatch, capsys):
    memories = [
        {
            "id": "mem_1782675392144_3ba026",
            "content": "MNEMOS adopts MIF natively.",
            "category": "decisions",
            "namespace": "default",
            "created": "2026-06-28T20:00:00+00:00",
        },
        {
            "id": "mem_run_1",
            "content": "Restart the gateway.",
            "category": "rules",
            "namespace": "default",
            "created": "2026-06-28T20:01:00+00:00",
            "mif_type": "procedural",
        },
    ]
    monkeypatch.setattr(memory_export, "_fetch_memories_flat", lambda *a, **k: memories)

    out = tmp_path / "bundle"
    memory_export.cmd_mif(_args(out))

    manifest = json.loads((out / "mif-manifest.json").read_text())
    assert manifest["count"] == 2
    assert manifest["mif_version"] == "1.0.0"
    md_files = list(out.rglob("*.md"))
    assert len(md_files) == 2
    assert (out / "procedural").is_dir()  # explicit mif_type honored
    assert "MIF 1.0.0 bundle" in capsys.readouterr().out


def test_cmd_mif_round_trips_through_import_bundle(tmp_path, monkeypatch):
    from mnemos.portability import charon as mif_charon

    memories = [
        {
            "id": "mem_x",
            "content": "round trip",
            "category": "facts",
            "namespace": "default",
            "created": "2026-06-28T20:00:00+00:00",
        }
    ]
    monkeypatch.setattr(memory_export, "_fetch_memories_flat", lambda *a, **k: memories)
    out = tmp_path / "bundle"
    memory_export.cmd_mif(_args(out))

    back = mif_charon.import_bundle(out)
    assert back[0]["id"] == "mem_x"
    assert back[0]["content"] == "round trip"
    assert back[0]["mif_type"] == "semantic"


def test_streamed_manifest_preserves_bundle_schema_and_counts(tmp_path):
    from mnemos.portability.charon import export_bundle

    memories = ({"id": f"mem_{i}", "content": f"fact {i}", "category": "facts"} for i in range(5))
    result = export_bundle(memories, tmp_path, stream_manifest=True)
    manifest = json.loads((tmp_path / "mif-manifest.json").read_text())
    assert result["count"] == manifest["count"] == 5
    assert len(manifest["concepts"]) == 5
    assert all((tmp_path / entry["path"]).exists() for entry in manifest["concepts"])
