"""Offline MPF → MIF 1.0 migration tool."""

from __future__ import annotations

import json

from mnemos.portability import charon as mif_charon
from mnemos.tools import mpf_to_mif


def _mpf_envelope():
    return {
        "mpf_version": "0.1.1",
        "records": [
            {
                "kind": "memory",
                "id": "mem_a",
                "payload": {
                    "content": "MNEMOS adopts MIF.",
                    "category": "decisions",
                    "namespace": "default",
                    "created": "2026-06-28T20:00:00+00:00",
                },
            },
            {
                "kind": "memory",
                "id": "mem_b",
                "payload": {
                    "content": "Restart the gateway.",
                    "category": "rules",
                    "namespace": "default",
                    "created": "2026-06-28T20:01:00+00:00",
                    "mif_type": "procedural",
                },
            },
            {"kind": "kg_triple", "id": "t1", "payload": {"s": "x"}},  # non-memory ignored
        ],
    }


def test_convert_envelope_to_bundle(tmp_path):
    src = tmp_path / "memories.json"
    src.write_text(json.dumps(_mpf_envelope()), encoding="utf-8")
    out = tmp_path / "bundle"

    manifest = mpf_to_mif.convert(str(src), str(out))
    assert manifest["count"] == 2  # the kg_triple record is skipped
    assert manifest["mif_version"] == "1.0.0"

    # The migrated bundle imports back to the same memories.
    back = {m["content"]: m for m in mif_charon.import_bundle(out)}
    assert set(back) == {"MNEMOS adopts MIF.", "Restart the gateway."}
    assert back["Restart the gateway."]["mif_type"] == "procedural"


def test_convert_jsonl_with_sidecar_trailer(tmp_path):
    env = _mpf_envelope()
    src = tmp_path / "memories.jsonl"
    lines = [json.dumps(r) for r in env["records"]]
    lines.append(json.dumps({"mpf_sidecars": True, "kg_triples": [{"s": "x"}]}))  # trailer ignored
    src.write_text("\n".join(lines), encoding="utf-8")
    out = tmp_path / "bundle"

    # By default, an input that carries unsupported CHARON sidecars is
    # refused — the trailer would silently drop kg_triples, which is
    # data loss. Operators must opt in via --drop-unsupported-sidecars
    # (the new flag added in the fix).
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="kg_triples"):
        mpf_to_mif.convert(str(src), str(out))

    # Opt-in: conversion proceeds with sidecars dropped.
    manifest = mpf_to_mif.convert(str(src), str(out), drop_unsupported_sidecars=True)
    assert manifest["count"] == 2


def test_cli_main(tmp_path, capsys):
    src = tmp_path / "memories.json"
    src.write_text(json.dumps(_mpf_envelope()), encoding="utf-8")
    out = tmp_path / "bundle"
    mpf_to_mif.main(["--file", str(src), "--out", str(out)])
    assert "MIF 1.0.0 bundle" in capsys.readouterr().out
    assert (out / "mif-manifest.json").is_file()
