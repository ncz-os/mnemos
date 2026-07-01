"""Paging + section selection for verbatim GRAEAE consultation recall.

Shared by the ``/v1/consultations/{id}/full`` REST route (the mnemos-graeae
add-on) and the ``graeae_get_consultation`` MCP tool (core), so the paging
semantics stay identical across both surfaces. Operates purely on the dict
produced by ``ConsultationsRepository.fetch_consultation_full`` /
``assemble_consultation_full`` — no DB access, no framework deps.

Contract:
  * ``section`` ∈ VALID_FULL_SECTIONS. "all" assembles every part in order
    (source, quorum, synthesis, then one part per muse); the others return
    just that slice.
  * ``page`` is 1-based; one part per page. A part whose serialised text is
    <= ``page_size`` occupies a single page; a larger part is split into
    numbered sub-pages of ``page_size`` chars (never mid-codepoint — Python
    str slicing is codepoint-safe).
  * ``total_pages`` == number of rendered (post-split) parts; a section with
    no parts yields ``([], 0)`` rather than raising.
"""

from __future__ import annotations

import json
from typing import Any

VALID_FULL_SECTIONS = ("all", "source", "quorum", "synthesis", "muses")


def _serialise_part(part: dict[str, Any]) -> tuple[str, int]:
    """Return ``(rendered_json, char_len)`` for a part, for paging math."""
    rendered = json.dumps(part, ensure_ascii=False, default=str)
    return rendered, len(rendered)


def select_full_parts(full: dict[str, Any], section: str) -> list[dict[str, Any]]:
    """Ordered list of classified parts for the requested section.

    Each part carries a ``type`` key so the paging layer preserves identity
    through sub-page chunking.
    """
    parts: list[dict[str, Any]] = []
    if section in ("all", "source"):
        parts.append({"type": "source", **(full.get("source") or {})})
    if section in ("all", "quorum"):
        parts.append({"type": "quorum", **(full.get("quorum") or {})})
    if section in ("all", "synthesis"):
        parts.append({"type": "synthesis", **(full.get("synthesis") or {})})
    if section in ("all", "muses"):
        muses = full.get("muses") or []
        for idx, muse in enumerate(muses, start=1):
            parts.append({"type": f"muse:{idx}/{len(muses)}", **muse})
    return parts


def paginate_full_parts(
    parts: list[dict[str, Any]], page_size: int, page: int
) -> tuple[list[dict[str, Any]], int]:
    """Part-level paging. Parts longer than ``page_size`` split into numbered
    sub-pages (never mid-part, never mid-codepoint). Returns
    ``(page_parts, total_pages)``; an out-of-range or empty request yields
    ``([], total_pages)``.
    """
    rendered: list[dict[str, Any]] = []
    for part in parts:
        text, length = _serialise_part(part)
        if length <= page_size:
            rendered.append(part)
            continue
        chunks = [text[i : i + page_size] for i in range(0, length, page_size)]
        base_type = part.get("type", "part")
        for idx, chunk in enumerate(chunks, start=1):
            rendered.append(
                {
                    "type": f"{base_type}#{idx}/{len(chunks)}",
                    "_chunk": idx,
                    "_chunks": len(chunks),
                    "text": chunk,
                }
            )
    total_pages = len(rendered)
    if page < 1 or page > total_pages:
        return [], total_pages
    return [rendered[page - 1]], total_pages
