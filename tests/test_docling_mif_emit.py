"""MIF emit path for the Docling CLI importer (mnemos.tools.docling_import).

Exercises the --emit-mif behavior without a real Docling install or document by
stubbing _extract_text: ingested chunks must be written as a schema-validated
MIF 1.0 bundle (not POSTed), and must round-trip back through the MIF importer.

The MIF adapter is a first-party mnemos-core module; Docling itself is stubbed
so these tests do not require the optional heavy dependency.
"""

from pathlib import Path

import pytest

from mnemos.portability import charon
from mnemos.tools.docling_import import DoclingImporter, main


def _section(text="MNEMOS is a memory operating system. " * 40):
    return [{"text": text, "page": 1, "section": "intro", "title": "Doc A"}]


def test_docling_emit_mif_bundle_roundtrip(tmp_path):
    bundle = tmp_path / "bundle"
    imp = DoclingImporter(emit_mif=str(bundle), category="documents", tags=["docling-test"])
    imp._extract_text = lambda p: _section()  # bypass real Docling

    imp.import_file(Path("/tmp/sampleA.pdf"))
    assert imp.collected, "chunks should be staged in MIF mode"
    assert imp.collected[0]["id"].startswith("docling:"), "stable source id assigned"

    manifest = imp.write_mif_bundle()
    assert manifest["mif_version"] == "1.0.0"
    assert manifest["schema"].endswith("/mif.schema.json")
    assert manifest["count"] == len(imp.collected) >= 1
    assert (bundle / "mif-manifest.json").is_file()

    rows = charon.import_bundle(str(bundle))
    assert len(rows) == manifest["count"]
    assert "memory operating system" in (rows[0]["content"] or "").lower()


def test_docling_emit_mif_does_not_post(tmp_path, monkeypatch):
    # No charon needed: emit-mode import_file must not POST, regardless of bundle write.
    imp = DoclingImporter(emit_mif=str(tmp_path / "b"))
    posted = []
    monkeypatch.setattr(imp, "_post_memory", lambda m: (posted.append(m), True)[1])
    imp._extract_text = lambda p: _section("alpha beta gamma " * 30)

    imp.import_file(Path("/tmp/sampleB.pdf"))
    assert posted == [], "MIF emit mode must not POST chunks to the API"
    assert imp.collected, "chunks staged for the bundle instead"


def test_emit_mif_forces_stable_id_over_preexisting(tmp_path):
    imp = DoclingImporter(emit_mif=str(tmp_path / "d"))
    imp._extract_text = lambda p: _section("zeta " * 30)
    imp.import_file(Path("/tmp/sampleC.pdf"))
    assert all(m["id"].startswith("docling:") for m in imp.collected)


def test_write_mif_bundle_empty_raises(tmp_path):
    imp = DoclingImporter(emit_mif=str(tmp_path / "e"))
    with pytest.raises(RuntimeError, match="no chunks collected"):
        imp.write_mif_bundle()


def test_write_mif_bundle_duplicate_ids_raise(tmp_path):
    # Duplicate source ids (re-import / symlink+target) must fail loudly, before
    # any concept files are written. No charon needed (dup check precedes it).
    imp = DoclingImporter(emit_mif=str(tmp_path / "f"))
    dup = {"content": "x", "metadata": {"source_path": "/docs/a.pdf", "chunk_index": 0}, "id": "docling:/docs/a.pdf#0"}
    imp.collected = [dict(dup), dict(dup)]
    with pytest.raises(RuntimeError, match="duplicate MIF source ids"):
        imp.write_mif_bundle()


def test_emit_mif_empty_dir_rejected():
    with pytest.raises(SystemExit):
        main(["--file", "/tmp/x.pdf", "--emit-mif", ""])


def test_stable_id_is_deterministic(tmp_path):
    imp = DoclingImporter(emit_mif=str(tmp_path / "c"))
    mem = {"content": "x", "metadata": {"source_path": "/docs/a.pdf", "chunk_index": 3}}
    assert imp._mif_memory_id(mem) == "docling:/docs/a.pdf#3"
