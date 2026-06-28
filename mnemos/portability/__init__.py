"""CHARON portability — MIF (Modeled Information Format) 1.0 native adapter.

MNEMOS adopts MIF 1.0 (https://mif-spec.dev) as its native, full-Level-3
portability format, retiring the older MPF envelope. This package maps a MNEMOS
memory row to/from a MIF concept (canonical Markdown + lossless JSON-LD
projection) and validates against the published MIF JSON Schemas.

Architecture decision: MNEMOS `mem_1782679514682_85c817` (GRAEAE consult
e3f81616, 2026-06-28).
"""

from mnemos.portability.charon import export_bundle, import_bundle
from mnemos.portability.mif import (
    MIF_CONTEXT_URI,
    category_to_mif_type,
    concept_to_markdown,
    concept_to_memory,
    markdown_to_concept,
    memory_to_concept,
    mnemos_id_to_uuid,
    validate_concept,
)

__all__ = [
    "export_bundle",
    "import_bundle",
    "MIF_CONTEXT_URI",
    "category_to_mif_type",
    "concept_to_markdown",
    "concept_to_memory",
    "markdown_to_concept",
    "memory_to_concept",
    "mnemos_id_to_uuid",
    "validate_concept",
]
