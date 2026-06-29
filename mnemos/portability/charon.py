"""CHARON — MIF bundle export/import for MNEMOS.

A MIF bundle is a directory of canonical Markdown concept files laid out by
base type (``<conceptType>/<uuid>.md`` — matching MIF's path-style relationship
targets like ``/semantic/other-concept.md``), plus a ``mif-manifest.json`` index
that names the spec version, the schema ``$id``, and every concept's id/type/
path/source. This is the format CHARON reads and writes, replacing the legacy
MPF envelope.

The row→concept mapping (incl. the vault redaction rule) lives in
:mod:`mnemos.portability.mif`; this module is the file-tree layer on top.

ADR: MNEMOS ``mem_1782679514682_85c817`` (2026-06-28).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mnemos.portability import mif

MANIFEST_NAME = "mif-manifest.json"
MIF_VERSION = "1.0.0"
MIF_SCHEMA_ID = "https://mif-spec.dev/schema/mif.schema.json"


def _concept_uuid(concept: dict[str, Any]) -> str:
    """The bare UUID from a concept's ``@id`` (``urn:mif:<uuid>``)."""
    at_id = concept["@id"]
    return at_id[len("urn:mif:") :] if at_id.startswith("urn:mif:") else at_id


def export_bundle(
    memories: Iterable[dict[str, Any]],
    out_dir: str | Path,
    *,
    redact_vault: bool = True,
    validate: bool = True,
) -> dict[str, Any]:
    """Write `memories` as a MIF bundle under `out_dir`. Returns the manifest.

    Each memory becomes ``<out_dir>/<conceptType>/<uuid>.md``. With
    ``validate`` (default on) every concept is checked against the published
    MIF JSON Schema before it is written, so an export is conformant or it
    raises. Vault memories are redacted per the mapping rule.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for memory in memories:
        concept = mif.memory_to_concept(memory, redact_vault=redact_vault)
        if validate:
            errors = mif.validate_concept(concept)
            if errors:
                raise ValueError(
                    f"memory {memory.get('id')!r} produced a non-conformant MIF concept: " + "; ".join(errors)
                )
        uuid = _concept_uuid(concept)
        ctype = concept["conceptType"]
        rel = f"{ctype}/{uuid}.md"
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mif.concept_to_markdown(concept), encoding="utf-8")
        entries.append(
            {
                "@id": concept["@id"],
                "conceptType": ctype,
                "path": rel,
                "mnemos_id": (concept.get("properties") or {}).get("mnemos:id"),
            }
        )
    manifest = {
        "mif_version": MIF_VERSION,
        "schema": MIF_SCHEMA_ID,
        "context": mif.MIF_CONTEXT_URI,
        "generator": "mnemos-charon",
        "count": len(entries),
        "concepts": entries,
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def import_bundle(in_dir: str | Path) -> list[dict[str, Any]]:
    """Read a MIF bundle (or any directory of MIF ``.md`` concept files) back
    into MNEMOS memory rows. Prefers the manifest's concept list for ordering
    and completeness; falls back to a recursive ``*.md`` walk when no manifest
    is present (so a hand-authored MIF directory imports too)."""
    src = Path(in_dir)
    manifest_path = src / MANIFEST_NAME
    md_paths: list[Path]
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        md_paths = [src / e["path"] for e in manifest.get("concepts", [])]
    else:
        md_paths = sorted(p for p in src.rglob("*.md"))
    memories: list[dict[str, Any]] = []
    for path in md_paths:
        concept = mif.markdown_to_concept(path.read_text(encoding="utf-8"))
        memories.append(mif.concept_to_memory(concept))
    return memories
