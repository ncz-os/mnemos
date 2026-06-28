"""MNEMOS ↔ MIF 1.0 mapping (CHARON native adapter).

A MNEMOS memory row maps to a MIF *concept* in two equivalent representations:

* canonical **Markdown** concept file (YAML frontmatter + body), and
* the lossless **JSON-LD** projection (what the published MIF JSON Schemas
  validate; required keys ``@context``/``@type``/``@id``/``conceptType``/
  ``content``/``created``).

The JSON-LD form is the in-memory pivot: ``memory_to_concept`` builds it,
``concept_to_markdown`` / ``markdown_to_concept`` round-trip it to/from the
canonical ``.md``, and ``concept_to_memory`` reverses it back to a MNEMOS row.
Markdown ↔ JSON-LD is lossless by construction (frontmatter is authoritative).

Vault rule: a ``namespace == "vault"`` memory NEVER emits its secret body —
``content`` becomes ``[CONTENT ENCRYPTED]`` and provenance/embedding source
text are redacted. This mirrors the redact-at-boundary posture of the F1–F6
security review.

ADR: MNEMOS ``mem_1782679514682_85c817`` (GRAEAE consult e3f81616, 2026-06-28).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_SCHEMA_DIR = Path(__file__).parent / "mif_schemas"

#: Canonical MIF JSON-LD context (a resolvable URI; consumers fetch the term
#: definitions from here). Update in lock-step with the pinned schema release.
MIF_CONTEXT_URI = "https://mif-spec.dev/ns/context.jsonld"
MIF_TYPE = "Concept"

#: Fixed namespace UUID for deterministic MNEMOS-id → UUIDv5. Stable forever:
#: the same ``mem_…`` id always yields the same MIF ``@id``, so export/import
#: round-trips and re-exports do not churn identities.
MNEMOS_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://mnemos.dev/id")

VAULT_NAMESPACE = "vault"
VAULT_REDACTED_BODY = "[CONTENT ENCRYPTED]"
VAULT_REDACTED_REF = "redacted"

#: category → MIF base type. **Migration fallback only** — the authoritative
#: source is the native ``mif_type`` field set at ingest/classification (ADR).
#: MIF's triad is *ontological* (nature of the memory), not *topical* (subject),
#: so this map is a best-effort default for legacy rows lacking ``mif_type``.
_CATEGORY_TYPE_MAP = {
    # declarative / context-free knowledge
    "facts": "semantic",
    "infrastructure": "semantic",
    "reference": "semantic",
    "standards": "semantic",
    "patterns": "semantic",
    "preferences": "semantic",
    "solutions": "semantic",
    "decisions": "semantic",
    "documentation": "semantic",
    # time/place-bound records
    "git_commit": "episodic",
    "sessions": "episodic",
    "project_activity": "episodic",
    "graeae_consultation": "episodic",
    # how-to knowledge
    "rules": "procedural",
    "projects": "procedural",
}
_VALID_TYPES = frozenset({"semantic", "episodic", "procedural"})

# MNEMOS provenance source-kind → MIF Provenance.sourceType enum.
_SOURCE_TYPE_MAP = {
    "user": "user_explicit",
    "user_explicit": "user_explicit",
    "user_implicit": "user_implicit",
    "agent": "agent_inferred",
    "agent_inferred": "agent_inferred",
    "import": "external_import",
    "external_import": "external_import",
    "system": "system_generated",
    "system_generated": "system_generated",
}


def category_to_mif_type(category: str | None) -> str:
    """Best-effort base type for a legacy row (migration fallback). Unknown →
    ``semantic`` (the safe declarative default)."""
    return _CATEGORY_TYPE_MAP.get((category or "").strip().lower(), "semantic")


def resolve_mif_type(memory: dict[str, Any]) -> str:
    """Authoritative base type: the native ``mif_type`` field when present and
    valid, else the category migration fallback."""
    native = (memory.get("mif_type") or "").strip().lower()
    if native in _VALID_TYPES:
        return native
    return category_to_mif_type(memory.get("category"))


def mnemos_id_to_uuid(mem_id: str) -> str:
    """Deterministic UUIDv5 for a MNEMOS id — the MIF ``@id`` (sans urn prefix).
    Same id → same UUID, always."""
    return str(uuid.uuid5(MNEMOS_UUID_NAMESPACE, mem_id))


def _iso(value: Any) -> str | None:
    """Normalize a datetime / ISO string to RFC3339 with explicit offset."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    s = str(value).strip()
    if not s:
        return None
    # Accept a trailing Z; keep as-is otherwise (already ISO).
    return s


def _coerce_meta(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str) and metadata.strip():
        try:
            parsed = json.loads(metadata)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def memory_to_concept(memory: dict[str, Any], *, redact_vault: bool = True) -> dict[str, Any]:
    """Map a MNEMOS memory row to a MIF JSON-LD concept (Level 3 where data
    supports it). The MNEMOS-native fields that have no MIF home are preserved
    under ``properties.mnemos`` so the round-trip is lossless."""
    mem_id = memory["id"]
    namespace = memory.get("namespace") or "default"
    is_vault = namespace == VAULT_NAMESPACE and redact_vault
    meta = _coerce_meta(memory.get("metadata"))

    content = VAULT_REDACTED_BODY if is_vault else (memory.get("content") or "")

    concept: dict[str, Any] = {
        "@context": MIF_CONTEXT_URI,
        "@type": MIF_TYPE,
        "@id": f"urn:mif:{mnemos_id_to_uuid(mem_id)}",
        "conceptType": resolve_mif_type(memory),
        "content": content,
        "created": _iso(memory.get("created")) or _iso(datetime.now(timezone.utc)),
    }

    modified = _iso(memory.get("updated"))
    if modified:
        concept["modified"] = modified
    if namespace:
        concept["namespace"] = namespace

    # MNEMOS taxonomy → tags (category/subcategory are topical, distinct from
    # the ontological conceptType). Also kept verbatim under properties.mnemos.
    tags = [t for t in (memory.get("category"), memory.get("subcategory")) if t]
    extra_tags = meta.get("tags")
    if isinstance(extra_tags, list):
        tags.extend(str(t) for t in extra_tags)
    if tags:
        concept["tags"] = list(dict.fromkeys(tags))  # de-dupe, keep order

    # Provenance (#85 layer): MNEMOS source_* + quality_rating → MIF Provenance.
    prov: dict[str, Any] = {"@type": "Provenance"}
    if memory.get("source_agent"):
        prov["agent"] = str(memory["source_agent"])
    if memory.get("source_provider") or memory.get("source_model"):
        prov["agentVersion"] = "/".join(
            str(x) for x in (memory.get("source_provider"), memory.get("source_model")) if x
        )
    src = memory.get("source")
    if src:
        prov["sourceType"] = _SOURCE_TYPE_MAP.get(str(src).strip().lower(), "external_import")
    if is_vault:
        prov["sourceRef"] = VAULT_REDACTED_REF
    elif memory.get("source_session"):
        prov["sourceRef"] = str(memory["source_session"])
    qr = memory.get("quality_rating")
    if isinstance(qr, (int, float)):
        prov["confidence"] = round(max(0.0, min(100.0, float(qr))) / 100.0, 4)
    if len(prov) > 1:  # more than just @type
        concept["provenance"] = prov

    # Embedding metadata (never the raw vector here; vault omits sourceText).
    if memory.get("embedding_model") or memory.get("embedding_dim"):
        emb: dict[str, Any] = {"@type": "EmbeddingReference"}
        if memory.get("embedding_model"):
            emb["model"] = str(memory["embedding_model"])
        if memory.get("embedding_dim"):
            emb["dimensions"] = int(memory["embedding_dim"])
        if not is_vault:
            emb["sourceText"] = memory.get("content") or ""
        concept["embedding"] = emb

    # Level 3 compression: MNEMOS compressed_content → summary (not for vault).
    if not is_vault and memory.get("compressed_content"):
        concept["summary"] = str(memory["compressed_content"])

    # MNEMOS-native fields with no MIF home → properties (a flat scalar map per
    # the MIF schema), namespaced under a `mnemos:` key prefix. Lossless and
    # schema-valid (values are string/number/boolean only).
    props: dict[str, Any] = {"mnemos:id": mem_id}
    for key in ("category", "subcategory", "owner_id", "group_id", "permission_mode"):
        if memory.get(key) is not None:
            props[f"mnemos:{key}"] = memory[key]
    if is_vault:
        props["mnemos:redacted"] = True
    concept["properties"] = props

    return concept


def concept_to_memory(concept: dict[str, Any]) -> dict[str, Any]:
    """Reverse of :func:`memory_to_concept` — a MNEMOS memory row from a MIF
    concept. The original MNEMOS id (and category/subcategory) come from
    ``properties.mnemos`` when present; otherwise the concept is treated as a
    foreign import and a fresh ``mem_…`` id is left for the caller to assign."""
    props = concept.get("properties") or {}
    memory: dict[str, Any] = {
        "content": concept.get("content") or "",
        "mif_type": concept.get("conceptType"),
        "namespace": concept.get("namespace") or "default",
        "created": concept.get("created"),
        "updated": concept.get("modified") or concept.get("created"),
    }
    if props.get("mnemos:id"):
        memory["id"] = props["mnemos:id"]
    for key in ("category", "subcategory", "owner_id", "group_id", "permission_mode"):
        if props.get(f"mnemos:{key}") is not None:
            memory[key] = props[f"mnemos:{key}"]
    # Fall back to tags for category when the mnemos extension is absent
    # (foreign concept import).
    if "category" not in memory and concept.get("tags"):
        memory["category"] = concept["tags"][0]
    prov = concept.get("provenance") or {}
    if prov.get("agent"):
        memory["source_agent"] = prov["agent"]
    if prov.get("sourceRef") and prov["sourceRef"] != VAULT_REDACTED_REF:
        memory["source_session"] = prov["sourceRef"]
    emb = concept.get("embedding") or {}
    if emb.get("model"):
        memory["embedding_model"] = emb["model"]
    if emb.get("dimensions"):
        memory["embedding_dim"] = emb["dimensions"]
    if concept.get("summary"):
        memory["compressed_content"] = concept["summary"]
    return memory


# ── Markdown ↔ JSON-LD ───────────────────────────────────────────────────────
# The canonical .md is YAML frontmatter + a Markdown body. Frontmatter is
# authoritative. We map the JSON-LD keys to their frontmatter names (@id→id,
# conceptType→type, properties→extensions) and put `content` in the body.

_FRONTMATTER_FENCE = "---"


def concept_to_markdown(concept: dict[str, Any]) -> str:
    """Serialize a MIF JSON-LD concept to a canonical Markdown concept file."""
    fm: dict[str, Any] = {
        "id": _strip_urn(concept["@id"]),
        "type": concept["conceptType"],
        "created": concept["created"],
    }
    for jsonld_key, fm_key in (
        ("modified", "modified"),
        ("namespace", "namespace"),
        ("title", "title"),
        ("tags", "tags"),
        ("ontology", "ontology"),
        ("temporal", "temporal"),
        ("provenance", "provenance"),
        ("embedding", "embedding"),
        ("citations", "citations"),
        ("documents", "documents"),
        ("relationships", "relationships"),
        ("aliases", "aliases"),
        ("summary", "summary"),
        ("properties", "extensions"),
    ):
        if jsonld_key in concept and concept[jsonld_key] not in (None, [], {}):
            fm[fm_key] = concept[jsonld_key]

    yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()
    body = concept.get("content") or ""
    return f"{_FRONTMATTER_FENCE}\n{yaml_block}\n{_FRONTMATTER_FENCE}\n\n{body}\n"


def markdown_to_concept(md: str) -> dict[str, Any]:
    """Parse a canonical Markdown concept file back to a MIF JSON-LD concept
    (the exact inverse of :func:`concept_to_markdown`)."""
    fm, body = _split_frontmatter(md)
    concept: dict[str, Any] = {
        "@context": MIF_CONTEXT_URI,
        "@type": MIF_TYPE,
        "@id": fm["id"] if str(fm["id"]).startswith("urn:") else f"urn:mif:{fm['id']}",
        "conceptType": fm["type"],
        "content": body,
        "created": fm["created"],
    }
    for fm_key, jsonld_key in (
        ("modified", "modified"),
        ("namespace", "namespace"),
        ("title", "title"),
        ("tags", "tags"),
        ("ontology", "ontology"),
        ("temporal", "temporal"),
        ("provenance", "provenance"),
        ("embedding", "embedding"),
        ("citations", "citations"),
        ("documents", "documents"),
        ("relationships", "relationships"),
        ("aliases", "aliases"),
        ("summary", "summary"),
        ("extensions", "properties"),
    ):
        if fm_key in fm and fm[fm_key] not in (None, [], {}):
            concept[jsonld_key] = fm[fm_key]
    return concept


def _strip_urn(value: str) -> str:
    return value[len("urn:mif:") :] if value.startswith("urn:mif:") else value


def _split_frontmatter(md: str) -> tuple[dict[str, Any], str]:
    text = md.lstrip("﻿")
    if not text.startswith(_FRONTMATTER_FENCE):
        raise ValueError("MIF Markdown file must start with a YAML frontmatter fence (---)")
    rest = text[len(_FRONTMATTER_FENCE) :].lstrip("\n")
    end = rest.find(f"\n{_FRONTMATTER_FENCE}")
    if end == -1:
        raise ValueError("unterminated YAML frontmatter (missing closing ---)")
    yaml_block = rest[:end]
    body = rest[end + len(f"\n{_FRONTMATTER_FENCE}") :]
    # body: drop the single blank line that follows the closing fence
    if body.startswith("\n"):
        body = body[1:]
    if body.startswith("\n"):
        body = body[1:]
    fm = yaml.safe_load(yaml_block) or {}
    if not isinstance(fm, dict):
        raise ValueError("MIF frontmatter must be a YAML mapping")
    return fm, body.rstrip("\n")


# ── Schema validation ────────────────────────────────────────────────────────

_VALIDATOR = None


def _build_validator():
    global _VALIDATOR
    if _VALIDATOR is not None:
        return _VALIDATOR
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    resources = []
    for path in _SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        if "$id" in schema:
            resources.append((schema["$id"], Resource.from_contents(schema)))
    registry = Registry().with_resources(resources)
    root = json.loads((_SCHEMA_DIR / "mif.schema.json").read_text())
    _VALIDATOR = Draft202012Validator(root, registry=registry)
    return _VALIDATOR


def validate_concept(concept: dict[str, Any]) -> list[str]:
    """Validate a JSON-LD concept against the published MIF JSON Schema.
    Returns a list of human-readable error strings (empty == valid)."""
    validator = _build_validator()
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in validator.iter_errors(concept)]
