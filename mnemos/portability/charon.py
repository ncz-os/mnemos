"""CHARON — MIF bundle export/import for MNEMOS.

A MIF bundle is a directory of canonical Markdown concept files laid out by
base type (``<conceptType>/<uuid>.md`` — matching MIF's path-style relationship
targets like ``/semantic/other-concept.md``), plus a ``mif-manifest.json`` index
that names the spec version, the schema ``$id``, and every concept's id/type/
path/source. This is the format CHARON reads and writes, replacing the legacy
MPF envelope.

The row→concept mapping (incl. the vault redaction rule) lives in
:mod:`mnemos.portability.mif`; this module is the file-tree layer on top.

Backend-aware CHARON — the EXPORT/IMPORT flow that talks to a real
``PersistenceBackend`` (SQLite, Postgres, Oracle, Db2, MySQL/MariaDB) lives
below in :func:`export_bundle_from_backend` / :func:`import_bundle_to_backend`.
Sidecar design choice
---------------------
KG triples, memory versions and compressed variants are MNEMOS-native rows
that the MIF 1.0 *concept* schema has no clean home for:

* KG triples can target any string (``subject`` / ``object`` may be literals
  like ``"Ithaca"`` or unattached entity names), and a MIF ``Relationship``
  requires a ``target`` that is either a bundle-relative concept path or a
  ``urn:mif:`` identifier. MIF also requires a strength in [0,1] and forbids
  arbitrary top-level keys (``additionalProperties: false``). A round-trip
  through ``Relationship`` would be lossy and break schema validation for the
  unattached/literal-object case that MNEMOS actively supports.
* Memory version rows carry commit-graph fields (``commit_hash``,
  ``parent_version_id``, ``branch``, ``merge_parents``) — git-style memory
  history. There is no MIF concept type for that.
* Compressed variants are MNEMOS distillation-pipeline rows with no MIF
  counterpart (MIF's ``summary`` field is a per-concept summary string, not
  a compression-engine audit row).

So CHARON persists these as a sidecar directory under the bundle::

    <out_dir>/_sidecars/{
        memories.jsonl,          # one JSON object per full memory row
        kg_triples.jsonl,        # one JSON object per KG triple row
        memory_versions.jsonl,   # one JSON object per memory_version row
        compression.jsonl,       # one JSON object per memory_compressed_variants row
    }

JSONL is the natural choice: line-oriented so a streaming reader can
re-insert without holding the whole bundle in memory, and a JSON object per
row maps 1:1 onto the backend-neutral ``Row`` shape the export/import repos
already return. The manifest records sidecar presence + counts so an
importer can refuse a bundle whose sidecars are missing.

The memory *concept* format (and the validation envelope in
:func:`validate_concept`) is unchanged.

ADR: MNEMOS ``mem_1782679514682_85c817`` (2026-06-28).
"""

from __future__ import annotations

import array as _array
import json
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from time import time
from typing import Any

from mnemos.portability import mif

MANIFEST_NAME = "mif-manifest.json"
MIF_VERSION = "1.0.0"
MIF_SCHEMA_ID = "https://mif-spec.dev/schema/mif.schema.json"

#: Directory inside a CHARON bundle that holds the non-concept sidecars
#: (KG triples, memory versions, compressed variants). Leading underscore so
#: it sorts to the top of an `ls` and is unmistakably MNEMOS-native, not
#: part of the MIF concept tree.
SIDECAR_DIR = "_sidecars"

#: Sidecar filenames inside :data:`SIDECAR_DIR`. Stable across versions; the
#: manifest points at these by relative path so consumers don't guess.
MEMORIES_SIDECAR = "memories.jsonl"
KG_TRIPLES_SIDECAR = "kg_triples.jsonl"
MEMORY_VERSIONS_SIDECAR = "memory_versions.jsonl"
COMPRESSION_SIDECAR = "compression.jsonl"

#: Pagination size for the export-side ``list_memories`` walk. Sized so a
#: typical backend round-trips ~200 rows; small enough to keep peak memory
#: bounded on multi-million-row stores.
DEFAULT_PAGE_SIZE = 200

#: A hard cap on the count per sidecar file, mirroring the
#: ``hard_limit`` parameter on the backend export repos. The repos always
#: return ``hard_limit + 1`` rows when they're at the limit (their existing
#: "more-rows-present" sentinel); we surface that to the caller via the
#: returned manifest's ``sidecars[*].truncated`` flag instead of silently
#: dropping rows.
SIDECAR_HARD_LIMIT = 1_000_000

# Oracle's IN-list limit is 1000. Keep CHARON sidecar fetches comfortably
# below that across every backend.
DEFAULT_SIDECAR_BATCH_SIZE = 500

_MEMORY_INSERT_KEYS = frozenset(
    {
        "content",
        "category",
        "subcategory",
        "metadata",
        "metadata_json",
        "quality_rating",
        "owner_id",
        "namespace",
        "permission_mode",
        "source_model",
        "source_provider",
        "source_session",
        "source_agent",
        "verbatim_content",
        "embedding",
        "created",
        "updated",
    }
)

_MEMORY_POST_INSERT_UPDATE_KEYS = frozenset(
    {
        # insert_memory has no group_id / archived_at parameters today, but
        # update_memory exposes them on the backend-neutral memories repo.
        "group_id",
        "archived_at",
        "consolidated_into",
    }
)

_MEMORY_CONCEPT_AUTHORITY_KEYS = frozenset(
    {
        # The Markdown concept is the operator-editable source of truth for
        # visible memory text and taxonomy. The hidden sidecar must not win
        # conflicts for these fields on import.
        "content",
        "category",
        "subcategory",
    }
)

_TIMESTAMP_FIELDS = frozenset(
    {
        "created",
        "updated",
        "valid_from",
        "valid_until",
        "snapshot_at",
        "selected_at",
        "archived_at",
    }
)


def _concept_uuid(concept: dict[str, Any]) -> str:
    """The bare UUID from a concept's ``@id`` (``urn:mif:<uuid>``)."""
    at_id = concept["@id"]
    return at_id[len("urn:mif:") :] if at_id.startswith("urn:mif:") else at_id


def _jsonable(value: Any) -> Any:
    """Recursively coerce a backend ``Row`` value into something
    :func:`json.dumps` accepts.

    Backend rows commonly carry :class:`datetime.datetime` /
    :class:`datetime.date` (from SQLite ``CURRENT_TIMESTAMP``) or already-
    serialised JSON strings (e.g. ``merge_parents`` and ``metadata`` on
    several backends). We normalise both so the sidecar JSONL is portable
    and human-readable.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    # aiosqlite's ``Row`` acts dict-like but isn't a dict; fall back to
    # dict() which aiosqlite implements.
    if hasattr(value, "keys"):
        return {str(k): _jsonable(value[k]) for k in value.keys()}
    return str(value)


def _coerce_json_text(value: Any) -> Any:
    """For columns the backend stores as JSON *strings* (Postgres ``jsonb``,
    SQLite ``TEXT``-backed JSON, MySQL ``JSON``), ``_fetch_*`` may return
    either the parsed object (aiosqlite ``jsonb`` is text-only, asyncpg /
    aiomysql parse), or a string. On the import path we want the parsed
    object so we can serialise it back to the backend's preferred form.

    Heuristic: a ``str`` that parses as a JSON object or array is converted;
    a plain string stays a string.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys"):
        try:
            if key in row.keys():
                return row[key]
        except Exception:  # noqa: BLE001 - backend Row implementations vary
            return default
    return getattr(row, key, default)


def _plain_row(row: Any) -> dict[str, Any]:
    out = _jsonable(row)
    return out if isinstance(out, dict) else {"value": out}


def _is_vault_namespace(row: Any) -> bool:
    return _row_get(row, "namespace") == mif.VAULT_NAMESPACE


def _chunks(items: Sequence[str], size: int) -> Iterable[tuple[str, ...]]:
    for idx in range(0, len(items), size):
        yield tuple(items[idx : idx + size])


def _safe_sidecar_batch_size(value: int) -> int:
    return max(1, min(int(value), DEFAULT_SIDECAR_BATCH_SIZE))


def _resolve_bundle_path(root: Path, manifest_path: str) -> Path:
    """Resolve a manifest-controlled path and ensure it stays under root."""
    rel = Path(manifest_path)
    if rel.is_absolute():
        raise ValueError(f"bundle path must be relative: {manifest_path!r}")
    base = root.resolve(strict=False)
    resolved = (base / rel).resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"bundle path escapes root: {manifest_path!r}") from exc
    return resolved


def _parse_datetime(value: Any, *, field: str) -> Any:
    """Parse JSON ISO timestamp strings back to aware datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp for {field}: {value!r}") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_timestamp_fields(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for field in _TIMESTAMP_FIELDS:
        if field in out:
            out[field] = _parse_datetime(out[field], field=field)
    return out


def _json_metadata(value: Any) -> str:
    parsed = _coerce_json_text(value)
    if parsed is None:
        return "{}"
    if isinstance(parsed, str):
        return parsed
    return json.dumps(parsed, ensure_ascii=False)


def _coerce_merge_parents(value: Any) -> list[str]:
    parsed = _coerce_json_text(value)
    if parsed in (None, ""):
        return []
    if isinstance(parsed, (list, tuple)):
        return [str(item) for item in parsed if item]
    return [str(parsed)]


def _coerce_embedding(value: Any) -> list[float] | None:
    # array.array first: python-oracledb returns VECTOR columns as array.array
    # ('f'/'d') by default. Handle it before _coerce_json_text / the `in (None,"")`
    # membership test (which would do an elementwise/ambiguous compare on
    # array-likes) so Oracle->Oracle migrations don't silently drop embeddings.
    if isinstance(value, _array.array):
        return [float(item) for item in value]
    parsed = _coerce_json_text(value)
    if parsed is None or parsed == "":
        return None
    if isinstance(parsed, _array.array):
        return [float(item) for item in parsed]
    if isinstance(parsed, str):
        stripped = parsed.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            parsed = [part.strip() for part in stripped[1:-1].split(",") if part.strip()]
        else:
            return None
    if isinstance(parsed, (list, tuple)):
        seq: Any = parsed
    elif isinstance(parsed, (dict, bytes, bytearray)):
        return None
    elif hasattr(parsed, "__iter__"):
        # generic numeric sequence (e.g. numpy array, memoryview-of-floats)
        seq = list(parsed)
    else:
        return None
    try:
        return [float(item) for item in seq]
    except (TypeError, ValueError):
        return None


def _merge_memory_import_row(concept_memory: dict[str, Any], base_row: dict[str, Any] | None) -> dict[str, Any]:
    """Layer the full exported memory row onto concept-derived data.

    The concept remains authoritative for visible content and taxonomy; the
    sidecar restores backend-native fields that MIF cannot represent.
    """
    memory = dict(concept_memory)
    if base_row:
        base = _parse_timestamp_fields(base_row)
        for key in _MEMORY_INSERT_KEYS | _MEMORY_POST_INSERT_UPDATE_KEYS:
            if key in _MEMORY_CONCEPT_AUTHORITY_KEYS:
                continue
            if key in base and base[key] is not None:
                memory[key] = base[key]
        if "embedding" in memory:
            memory["embedding"] = _coerce_embedding(memory["embedding"])
        if "metadata" in base and base["metadata"] is not None:
            memory["metadata"] = _coerce_json_text(base["metadata"])
        if "metadata_json" in base and "metadata" not in memory:
            memory["metadata"] = _coerce_json_text(base["metadata_json"])
    return _parse_timestamp_fields(memory)


def _memory_metadata_json(memory: dict[str, Any]) -> str:
    if "metadata_json" in memory and memory["metadata_json"] is not None:
        return _json_metadata(memory["metadata_json"])
    return _json_metadata(memory.get("metadata"))


def _read_jsonl(
    path: Path,
    *,
    required: bool = False,
    expected_count: int | None = None,
    label: str = "sidecar",
) -> list[dict[str, Any]]:
    """Read a JSONL file. Declared sidecars are required and count-checked."""
    if not path.is_file():
        if required:
            raise ValueError(f"manifest declares {label} at {path}, but the file is missing")
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{label} row {line_no} is not a JSON object")
            out.append(row)
    if expected_count is not None and len(out) != expected_count:
        raise ValueError(f"manifest declares {expected_count} {label} rows, but {path} yielded {len(out)}")
    return out


def _sidecar_rows(
    src: Path,
    sidecars: dict[str, Any],
    name: str,
    filename: str,
) -> list[dict[str, Any]]:
    default_rel = f"{SIDECAR_DIR}/{filename}"
    if name not in sidecars:
        return []
    spec = sidecars.get(name) or {}
    rel = spec.get("path") or default_rel
    path = _resolve_bundle_path(src, str(rel))
    expected = None if spec.get("truncated") else int(spec.get("count", 0))
    return _read_jsonl(path, required=True, expected_count=expected, label=name)


def _declared_sidecars(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("sidecars_included") is not True:
        return {}
    sidecars = manifest.get("sidecars", {}) or {}
    if not isinstance(sidecars, dict):
        raise ValueError("manifest sidecars block must be a JSON object")
    return sidecars


async def _write_batched_sidecar(
    backend: Any,
    path: Path,
    *,
    memory_ids: Sequence[str],
    batch_size: int,
    include_empty_batch: bool,
    fetch_batch: Callable[[Any, tuple[str, ...], bool, int], Any],
) -> tuple[int, bool]:
    """Fetch sidecar rows in bounded memory-id chunks and stream JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    truncated = False
    batches = list(_chunks(memory_ids, _safe_sidecar_batch_size(batch_size)))
    if not batches and include_empty_batch:
        batches = [()]

    with path.open("w", encoding="utf-8") as fh:
        for index, batch in enumerate(batches):
            if count >= SIDECAR_HARD_LIMIT:
                truncated = True
                break
            remaining = SIDECAR_HARD_LIMIT - count
            async with backend.transactional() as tx:
                rows = await fetch_batch(tx, batch, index == 0, remaining)
            if len(rows) > remaining:
                truncated = True
                rows = rows[:remaining]
            for row in rows:
                plain = _plain_row(row)
                if _is_vault_namespace(plain):
                    continue
                if count >= SIDECAR_HARD_LIMIT:
                    truncated = True
                    break
                fh.write(json.dumps(plain, ensure_ascii=False))
                fh.write("\n")
                count += 1
            if truncated:
                break
    return count, truncated


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
        md_paths = [_resolve_bundle_path(src, str(e["path"])) for e in manifest.get("concepts", [])]
    else:
        md_paths = sorted(p for p in src.rglob("*.md"))
    memories: list[dict[str, Any]] = []
    for path in md_paths:
        concept = mif.markdown_to_concept(path.read_text(encoding="utf-8"))
        memories.append(mif.concept_to_memory(concept))
    return memories


# ── Backend-aware export / import ────────────────────────────────────────────
# These functions talk to a real :class:`PersistenceBackend` (via the
# backend-neutral repository accessors — memories / kg_triples /
# memory_versions / compression — and ``backend.transactional()`` for the
# tx handle). They contain NO backend-specific SQL; backend-specific code
# lives in each backend's repository implementation in mnemos.persistence.*.

# The set of "sidecar" fields we strip from a memory concept before passing
# it through ``mif.memory_to_concept`` for an export — these come from the
# backend ``Row`` but aren't user-facing on the MIF concept side. Keeping
# them on the row so the round-trip loses nothing; the MIF mapping already
# does its own field selection.
_NON_CONCEPT_ROW_KEYS = frozenset(
    {
        # present on backend rows but ignored by memory_to_concept (or absent
        # from the MIF concept schema). Listing them here documents the
        # intent — the mapping layer is the source of truth, this is a
        # defensive note.
    }
)


def _row_to_memory_dict(row: Any) -> dict[str, Any]:
    """Normalise a backend ``Row`` into a plain dict suitable for
    :func:`mif.memory_to_concept` and JSONL sidecars.

    * Coerces JSON-shaped strings (``metadata``, ``merge_parents``) to dicts
      so the concept mapping sees the parsed object.
    * Strips backend-only metadata (``Row`` proxies from aiosqlite carry a
      ``keys()`` interface — we collapse to ``dict`` so callers can mutate).
    """
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in {"metadata", "merge_parents"}:
            value = _coerce_json_text(value)
        elif key == "embedding":
            value = _coerce_embedding(value)
        out[key] = value
    return out


def _new_memory_id() -> str:
    return f"mem_{int(time() * 1_000_000)}_{uuid.uuid4().hex[:6]}"


def _new_kg_triple_id(*, requires_uuid: bool = False) -> str:
    if requires_uuid:
        return str(uuid.uuid4())
    return f"kg_{uuid.uuid4().hex}"


def _new_version_id() -> str:
    return str(uuid.uuid4())


def _is_valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _repo_requires_uuid_ids(repo: Any) -> bool:
    override = getattr(repo, "requires_uuid_ids", None)
    if override is not None:
        return bool(override)
    repo_type = type(repo)
    return repo_type.__module__ == "mnemos.persistence.postgres" and repo_type.__name__ == "PostgresVersionRepository"


def _sidecar_id_for_insert(
    original_id: Any,
    *,
    preserve_ids: bool,
    requires_uuid: bool,
    new_id: Callable[[], str],
) -> str:
    if preserve_ids and (not requires_uuid or _is_valid_uuid(original_id)):
        return str(original_id)
    return new_id()


def _concept_to_memory_row(concept: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reverse of :func:`_row_to_memory_dict`: turn a MIF concept back into
    a dict shaped like a memory ``insert_memory`` call wants.

    The MIF mapping (see :func:`mif.concept_to_memory`) is authoritative
    for the user-visible fields; here we layer in the backend-required
    columns (``metadata_json``, ``quality_rating``, ``verbatim_content``,
    ``source_provider``, ``source_model``) from the original ``base`` row
    when one is provided (preserve-on-import semantics), or sensible
    defaults when not.
    """
    memory = mif.concept_to_memory(concept)
    # Concept-derived embeddings are a reference, not a vector — drop them.
    memory.pop("embedding_model", None)
    memory.pop("embedding_dim", None)

    # Default the backend-mandatory columns.
    memory.setdefault("metadata_json", "{}")
    memory.setdefault("quality_rating", 50)
    memory.setdefault("verbatim_content", memory.get("content", ""))
    memory.setdefault("source_provider", None)
    memory.setdefault("source_model", None)
    memory.setdefault("group_id", None)
    memory.setdefault("source_session", memory.get("source_session"))
    memory.setdefault("source_agent", memory.get("source_agent"))

    if base:
        # Preserve-on-import: layer backend-only fields back on top so an
        # identical-shaped insert lands the same data.
        for key in (
            "metadata",
            "metadata_json",
            "quality_rating",
            "verbatim_content",
            "source_provider",
            "source_model",
            "source_session",
            "source_agent",
            "group_id",
            "permission_mode",
        ):
            if key in base and base[key] is not None:
                memory[key] = base[key]
    return memory


def _write_jsonl(rows: Iterable[Any], path: Path) -> int:
    """Write one JSON object per row to ``path``. Returns the count written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(_jsonable(row), ensure_ascii=False))
            fh.write("\n")
            count += 1
    return count


async def export_bundle_from_backend(
    backend: Any,
    out_dir: str | Path,
    *,
    owner_id: str | None = None,
    namespace: str | None = None,
    include_sidecars: bool = True,
    redact_vault: bool = True,
    page_size: int = DEFAULT_PAGE_SIZE,
    sidecar_batch_size: int = DEFAULT_SIDECAR_BATCH_SIZE,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Export every visible memory from ``backend`` as a MIF bundle under
    ``out_dir``, optionally with KG / version / compression sidecars.

    The export is backend-agnostic: it walks full memory rows via
    ``backend.memories.fetch_memory_export`` and applies CHARON's vault /
    archive policy in this orchestration layer, then pulls each sidecar
    slice via the corresponding repo's ``fetch_*_for_export`` method. No
    backend-specific SQL appears in this module.

    Parameters
    ----------
    backend:
        Any :class:`PersistenceBackend` — SQLite, Postgres, Oracle, Db2,
        MySQL/MariaDB all expose the same ``memories`` / ``kg_triples`` /
        ``memory_versions`` / ``compression`` / ``transactional()`` shape.
    out_dir:
        Bundle root. Will be created if missing.
    owner_id, namespace:
        Optional owner / namespace narrowing. Both ``None`` → cross-tenant
        ROOT_BYPASS export (the vault is always excluded).
    include_sidecars:
        When ``True`` (default) emit ``_sidecars/{kg_triples,memory_versions,
        compression}.jsonl`` and record their counts in the manifest. The
        three repos' ``hard_limit + 1`` sentinel row surfaces as the
        manifest's ``sidecars[*].truncated`` flag.
    redact_vault:
        Forwarded to :func:`mif.memory_to_concept`. Default ``True`` —
        vault memories are written as ``[CONTENT ENCRYPTED]`` with their
        provenance masked. Set ``False`` only for a trusted
        restore-to-same-operator flow (CHARON itself does not redact on
        import — the caller controls vault semantics on the target
        backend).
    page_size:
        Page size for the ``fetch_memory_export`` walk. Bounded so peak memory
        stays sane on multi-million-row stores; the repo already does
        LIMIT/OFFSET pagination.
    sidecar_batch_size:
        Maximum memory ids per sidecar repository fetch. Capped at 500 to
        stay below Oracle's IN-list limit and to keep bind arrays bounded.
    include_archived:
        Defaults to ``False`` to mirror the existing memory list semantics
        — archived memories are operator-visible, but the default CHARON
        export is the live set.

    Returns the bundle manifest (``mif-manifest.json`` shape, with an
    added ``sidecars`` block when ``include_sidecars`` is true).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    memories_for_concept: list[dict[str, Any]] = []
    memory_ids: list[str] = []
    offset = 0
    while True:
        async with backend.transactional() as tx:
            rows = await backend.memories.fetch_memory_export(
                tx,
                effective_owner=owner_id,
                effective_ns=namespace,
                category=None,
                limit=page_size,
                offset=offset,
            )
        if not rows:
            break
        for row in rows:
            memory = _row_to_memory_dict(row)
            if _is_vault_namespace(memory):
                continue
            if not include_archived and memory.get("archived_at") is not None:
                continue
            memories_for_concept.append(memory)
            mid = memory.get("id")
            if mid is not None:
                memory_ids.append(str(mid))
        offset += len(rows)
        if len(rows) < page_size:
            break

    # Reuse the pure ``export_bundle`` for the concept layer — keeps the
    # existing MIF-side behaviour (validation, vault redaction, manifest
    # shape) byte-identical.
    manifest = export_bundle(
        memories_for_concept,
        out,
        redact_vault=redact_vault,
        validate=True,
    )

    sidecar_block: dict[str, Any] = {}
    if include_sidecars:
        sidecar_dir = out / SIDECAR_DIR
        sidecar_dir.mkdir(parents=True, exist_ok=True)

        memory_count = _write_jsonl(memories_for_concept, sidecar_dir / MEMORIES_SIDECAR)
        sidecar_block["memories"] = {
            "path": f"{SIDECAR_DIR}/{MEMORIES_SIDECAR}",
            "count": memory_count,
            "truncated": False,
        }

        # KG triples: include_unattached=True so cross-entity facts (no
        # backing memory) survive a round-trip. Only the first batch asks
        # for unattached rows, preventing duplicates across chunks.
        async def _fetch_kg_batch(tx: Any, batch: tuple[str, ...], first: bool, hard_limit: int) -> list[Any]:
            return await backend.kg_triples.fetch_kg_triples_for_export(
                tx,
                memory_ids=batch,
                effective_owner=owner_id,
                effective_ns=namespace,
                include_unattached=first,
                hard_limit=hard_limit,
            )

        kg_count, kg_truncated = await _write_batched_sidecar(
            backend,
            sidecar_dir / KG_TRIPLES_SIDECAR,
            memory_ids=memory_ids,
            batch_size=sidecar_batch_size,
            include_empty_batch=True,
            fetch_batch=_fetch_kg_batch,
        )
        sidecar_block["kg_triples"] = {
            "path": f"{SIDECAR_DIR}/{KG_TRIPLES_SIDECAR}",
            "count": kg_count,
            "truncated": kg_truncated,
        }

        # Memory versions: bound to a known memory set (an unattached
        # version row is not a thing — versions always hang off a memory).
        async def _fetch_version_batch(tx: Any, batch: tuple[str, ...], _first: bool, hard_limit: int) -> list[Any]:
            if not batch:
                return []
            return await backend.memory_versions.fetch_memory_versions_for_export(
                tx,
                memory_ids=batch,
                effective_owner=owner_id,
                effective_ns=namespace,
                hard_limit=hard_limit,
            )

        ver_count, ver_truncated = await _write_batched_sidecar(
            backend,
            sidecar_dir / MEMORY_VERSIONS_SIDECAR,
            memory_ids=memory_ids,
            batch_size=sidecar_batch_size,
            include_empty_batch=False,
            fetch_batch=_fetch_version_batch,
        )
        sidecar_block["memory_versions"] = {
            "path": f"{SIDECAR_DIR}/{MEMORY_VERSIONS_SIDECAR}",
            "count": ver_count,
            "truncated": ver_truncated,
        }

        # Compressed variants: also memory-bound.
        async def _fetch_compression_batch(tx: Any, batch: tuple[str, ...], _first: bool, hard_limit: int) -> list[Any]:
            if not batch:
                return []
            return await backend.compression.fetch_compressed_variants_for_export(
                tx,
                memory_ids=batch,
                effective_owner=owner_id,
                hard_limit=hard_limit,
            )

        comp_count, comp_truncated = await _write_batched_sidecar(
            backend,
            sidecar_dir / COMPRESSION_SIDECAR,
            memory_ids=memory_ids,
            batch_size=sidecar_batch_size,
            include_empty_batch=False,
            fetch_batch=_fetch_compression_batch,
        )
        sidecar_block["compression"] = {
            "path": f"{SIDECAR_DIR}/{COMPRESSION_SIDECAR}",
            "count": comp_count,
            "truncated": comp_truncated,
        }

        manifest["sidecars"] = sidecar_block
        manifest["sidecars_included"] = True
        manifest["backend"] = type(backend).__name__
    else:
        manifest["sidecars_included"] = False
        manifest["backend"] = type(backend).__name__

    # Re-emit with the new keys merged in (export_bundle wrote its own
    # manifest; we re-write to preserve the same on-disk shape).
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


async def import_bundle_to_backend(
    backend: Any,
    in_dir: str | Path,
    *,
    preserve_ids: bool = True,
    redact_vault: bool = True,
) -> dict[str, Any]:
    """Import a MIF bundle (with optional sidecars) into ``backend``.

    Reuses the pure :func:`import_bundle` to parse the concept layer, then
    re-inserts every memory via ``backend.memories.insert_memory``, and
    replays each present sidecar (``kg_triples`` / ``memory_versions`` /
    ``compression``) through the corresponding ``insert_*`` repo method.

    All inserts run inside a single ``backend.transactional()`` so a
    mid-import failure rolls back cleanly. ``backend.memories.insert_memory``
    raises :class:`DuplicateMemoryError` on a re-import — we treat the
    existing-row case as an idempotent skip rather than a fatal, so a
    bundle can be re-applied safely.

    Parameters
    ----------
    backend:
        Any :class:`PersistenceBackend` — same contract as
        :func:`export_bundle_from_backend`.
    in_dir:
        Bundle root. The manifest is read if present; otherwise the
        ``.md`` walk from :func:`import_bundle` is used.
    preserve_ids:
        When ``True`` (default) the original ``mem_…`` ids round-trip —
        CHARON maps MIF ``@id`` → MNEMOS id via the ``properties.mnemos:id``
        extension, so a re-import lands on the same row identity. When
        ``False`` the caller wants fresh ids; CHARON leaves ``id`` unset
        on the row and the backend's insert assigns one. The current
        backend contract requires a caller-supplied id, so ``preserve_ids=
        False`` falls back to minting a fresh ``mem_…`` id (UUID4-hex).
    redact_vault:
        Forwarded to :func:`mif.memory_to_concept` on the export side; the
        *import* side has nothing to redact (vault semantics live on the
        target backend's ``namespace``), so this is currently a forward-
        compat parameter only.

    Returns a small report: ``{"memories_inserted": N, "memories_skipped":
    N, "kg_triples_inserted": N, "memory_versions_inserted": N,
    "compressed_variants_inserted": N}``. Sidecars absent from the bundle
    are simply omitted from the report (count == 0).
    """
    # Mirror the parameter — currently unused but documented so a future
    # redaction-on-import policy has a stable hook.
    del redact_vault
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    src = Path(in_dir)
    manifest_path = src / MANIFEST_NAME
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    memories = import_bundle(src)
    sidecars = _declared_sidecars(manifest)
    memory_sidecar_rows = _sidecar_rows(src, sidecars, "memories", MEMORIES_SIDECAR)
    memory_rows_by_id = {
        str(row["id"]): row for row in memory_sidecar_rows if row.get("id") is not None and not _is_vault_namespace(row)
    }

    versions_sidecar_declared = "memory_versions" in sidecars
    kg_rows = _sidecar_rows(src, sidecars, "kg_triples", KG_TRIPLES_SIDECAR)
    version_rows = _sidecar_rows(src, sidecars, "memory_versions", MEMORY_VERSIONS_SIDECAR)
    compression_rows = _sidecar_rows(src, sidecars, "compression", COMPRESSION_SIDECAR)

    memories_inserted = 0
    memories_skipped = 0
    kg_inserted = 0
    ver_inserted = 0
    comp_inserted = 0
    memory_id_map: dict[str, str] = {}
    kg_ids_require_uuid = _repo_requires_uuid_ids(backend.kg_triples)
    version_ids_require_uuid = _repo_requires_uuid_ids(backend.memory_versions)
    version_id_map = {
        str(row["id"]): _sidecar_id_for_insert(
            row["id"],
            preserve_ids=preserve_ids,
            requires_uuid=version_ids_require_uuid,
            new_id=_new_version_id,
        )
        for row in version_rows
        if row.get("id") is not None
    }
    pending_memory_updates: list[tuple[str, dict[str, Any]]] = []
    versioned_branch_keys_for_head_rebuild: set[tuple[str, str]] = set()

    root_visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
        exclude_namespaces=(),
    )

    def _mapped_version_ref(value: Any) -> str | None:
        if value is None:
            return None
        ref = str(value)
        mapped = version_id_map.get(ref)
        if mapped is not None:
            return mapped
        if version_ids_require_uuid and not _is_valid_uuid(ref):
            return None
        return ref

    async with backend.transactional() as tx:
        if versions_sidecar_declared:
            await backend.memories.set_suppress_version_snapshot(tx)

        # ── memories ───────────────────────────────────────────────────
        for concept_memory in memories:
            old_mid = concept_memory.get("id")
            base_row = memory_rows_by_id.get(str(old_mid)) if old_mid is not None else None
            memory = _merge_memory_import_row(concept_memory, base_row)
            mid = memory.get("id") if preserve_ids else None
            if not mid:
                # Backend contract requires a caller-supplied id; mint a
                # fresh ``mem_<ts>_<hex>`` so we stay schema-conformant.
                mid = _new_memory_id()
                memory["id"] = mid
            if old_mid is not None:
                memory_id_map[str(old_mid)] = str(mid)

            # The concept mapping may have left ``embedding_model`` /
            # ``embedding_dim`` keys behind (it strips them in
            # ``_concept_to_memory_row`` but the pure ``import_bundle``
            # path doesn't); the backend doesn't want them as kwargs.
            memory.pop("embedding_model", None)
            memory.pop("embedding_dim", None)
            # Backend-required scalar defaults.
            memory.setdefault("category", "facts")
            memory.setdefault("namespace", "default")
            memory.setdefault("permission_mode", 0)
            memory.setdefault("quality_rating", 50)
            try:
                await backend.memories.insert_memory(
                    tx,
                    memory_id=mid,
                    content=memory.get("content") or "",
                    category=memory.get("category") or "facts",
                    subcategory=memory.get("subcategory"),
                    metadata_json=_memory_metadata_json(memory),
                    quality_rating=int(memory.get("quality_rating") or 50),
                    owner_id=memory.get("owner_id") or "default",
                    namespace=memory.get("namespace") or "default",
                    permission_mode=int(memory.get("permission_mode") or 0),
                    source_model=memory.get("source_model"),
                    source_provider=memory.get("source_provider"),
                    source_session=memory.get("source_session"),
                    source_agent=memory.get("source_agent"),
                    verbatim_content=memory.get("verbatim_content") or memory.get("content") or "",
                    embedding=memory.get("embedding"),
                    created=_parse_datetime(memory.get("created"), field="created"),
                    updated=_parse_datetime(memory.get("updated"), field="updated"),
                )
                update_fields = {
                    key: memory[key]
                    for key in _MEMORY_POST_INSERT_UPDATE_KEYS
                    if key in memory and memory[key] is not None
                }
                if update_fields:
                    if memory.get("updated") is not None:
                        update_fields["updated"] = memory["updated"]
                    pending_memory_updates.append((str(mid), update_fields))
                memories_inserted += 1
            except Exception as exc:  # noqa: BLE001 — see below
                # Idempotent re-import: a pre-existing row is fine. The
                # backend raises DuplicateMemoryError, but some backends
                # use a different signal — anything that clearly indicates
                # "row already exists" counts as a skip.
                if _is_duplicate_memory_error(exc):
                    memories_skipped += 1
                    continue
                raise

        for mid, update_fields in pending_memory_updates:
            if "archived_at" in update_fields:
                update_fields["archived_at"] = _parse_datetime(update_fields["archived_at"], field="archived_at")
            if "updated" in update_fields:
                update_fields["updated"] = _parse_datetime(update_fields["updated"], field="updated")
            if "consolidated_into" in update_fields:
                target = update_fields["consolidated_into"]
                if target is not None:
                    update_fields["consolidated_into"] = memory_id_map.get(str(target), str(target))
            await backend.memories.update_memory(
                tx,
                mid,
                visibility=root_visibility,
                fields=update_fields,
            )

        # ── KG triples sidecar ─────────────────────────────────────────
        for row in kg_rows:
            if _is_vault_namespace(row):
                continue
            memory_id = row.get("memory_id")
            if memory_id is not None:
                memory_id = memory_id_map.get(str(memory_id))
                if memory_id is None:
                    continue
            await backend.kg_triples.insert_kg_triple(
                tx,
                triple_id=_sidecar_id_for_insert(
                    row["id"],
                    preserve_ids=preserve_ids,
                    requires_uuid=kg_ids_require_uuid,
                    new_id=lambda: _new_kg_triple_id(requires_uuid=kg_ids_require_uuid),
                ),
                subject=row.get("subject") or "",
                predicate=row.get("predicate") or "",
                obj=row.get("object") or "",
                subject_type=row.get("subject_type"),
                object_type=row.get("object_type"),
                valid_from=_parse_datetime(row.get("valid_from"), field="valid_from"),
                valid_until=_parse_datetime(row.get("valid_until"), field="valid_until"),
                memory_id=memory_id,
                confidence=row.get("confidence"),
                created=_parse_datetime(row.get("created"), field="created"),
                owner_id=row.get("owner_id") or "default",
                namespace=row.get("namespace") or "default",
            )
            kg_inserted += 1

        # ── memory versions sidecar ────────────────────────────────────
        for row in version_rows:
            if _is_vault_namespace(row):
                continue
            memory_id = memory_id_map.get(str(row.get("memory_id")))
            if memory_id is None:
                continue
            version_id = version_id_map.get(str(row["id"]), str(row["id"]))
            merge_parents = [
                mapped_parent
                for parent in _coerce_merge_parents(row.get("merge_parents"))
                if (mapped_parent := _mapped_version_ref(parent))
            ]
            parent_version_id = row.get("parent_version_id")
            if parent_version_id:
                parent_version_id = _mapped_version_ref(parent_version_id)
            branch_value = row.get("branch")
            branch = str(branch_value) if branch_value is not None else "main"
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id,
                memory_id=memory_id,
                version_num=int(row.get("version_num") or 1),
                content=row.get("content") or "",
                category=row.get("category"),
                subcategory=row.get("subcategory"),
                metadata_json=_json_metadata(row.get("metadata")),
                verbatim_content=row.get("verbatim_content"),
                owner_id=row.get("owner_id") or "default",
                namespace=row.get("namespace"),
                permission_mode=row.get("permission_mode"),
                source_model=row.get("source_model"),
                source_provider=row.get("source_provider"),
                source_session=row.get("source_session"),
                source_agent=row.get("source_agent"),
                snapshot_at=_parse_datetime(row.get("snapshot_at"), field="snapshot_at"),
                snapshot_by=row.get("snapshot_by"),
                change_type=row.get("change_type"),
                commit_hash=row.get("commit_hash"),
                parent_version_id=parent_version_id,
                branch=branch_value,
                merge_parents=merge_parents,
            )
            versioned_branch_keys_for_head_rebuild.add((memory_id, branch))
            ver_inserted += 1

        if versioned_branch_keys_for_head_rebuild:
            memory_ids_for_head_rebuild = sorted(
                {memory_id for memory_id, _branch in versioned_branch_keys_for_head_rebuild}
            )
            branch_heads = await backend.memory_branches.fetch_memory_branch_heads(
                tx,
                memory_ids_for_head_rebuild,
            )
            for head in branch_heads:
                memory_id = _row_get(head, "memory_id")
                branch = _row_get(head, "branch")
                head_version_id = _row_get(head, "head_version_id")
                if memory_id is None or branch is None or head_version_id is None:
                    continue
                if (str(memory_id), str(branch)) not in versioned_branch_keys_for_head_rebuild:
                    continue
                await backend.memory_branches.upsert_memory_branch_head(
                    tx,
                    memory_id=str(memory_id),
                    branch=str(branch),
                    head_version_id=head_version_id,
                )

        # ── compression sidecar ────────────────────────────────────────
        for row in compression_rows:
            if _is_vault_namespace(row):
                continue
            memory_id = memory_id_map.get(str(row.get("memory_id")))
            if memory_id is None:
                continue
            await backend.compression.insert_compressed_variant(
                tx,
                memory_id=memory_id,
                owner_id=row.get("owner_id") or "default",
                winner_candidate_id=row.get("winner_candidate_id"),
                engine_id=row.get("engine_id") or "unknown",
                engine_version=row.get("engine_version"),
                compressed_content=row.get("compressed_content"),
                compressed_tokens=row.get("compressed_tokens"),
                compression_ratio=row.get("compression_ratio"),
                quality_score=row.get("quality_score"),
                composite_score=row.get("composite_score"),
                scoring_profile=row.get("scoring_profile"),
                judge_model=row.get("judge_model"),
                selected_at=_parse_datetime(row.get("selected_at"), field="selected_at"),
            )
            comp_inserted += 1

    return {
        "memories_inserted": memories_inserted,
        "memories_skipped": memories_skipped,
        "kg_triples_inserted": kg_inserted,
        "memory_versions_inserted": ver_inserted,
        "compressed_variants_inserted": comp_inserted,
    }


def _is_duplicate_memory_error(exc: BaseException) -> bool:
    """Best-effort detection of "row already exists" across backends.

    The SQLite impl raises :class:`mnemos.persistence.base.DuplicateMemoryError`;
    Postgres raises its own (different) exception. Some backends surface it
    as an IntegrityError with a known message. We keep this narrow on
    purpose — anything else propagates so the caller learns about real
    failures.
    """
    name = type(exc).__name__
    if name == "DuplicateMemoryError":
        return True
    if name in {"IntegrityError", "UniqueViolation", "DuplicateKeyError"}:
        msg = str(exc).lower()
        if "unique" in msg or "duplicate" in msg or "primary key" in msg:
            return True
    return False
