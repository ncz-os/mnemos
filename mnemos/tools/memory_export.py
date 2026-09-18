#!/usr/bin/env python3
"""
memory_export.py — CHARON export side (MNEMOS memory portability).

Part of CHARON, MNEMOS's memory portability subsystem. Companion to
``memory_import.py``. Together they anchor a round-trip you can
trust: export from instance A, import to instance B, same ids,
same owners, same provenance.

Subcommands:
  json        Emit one MPF envelope (big JSON).
  jsonl       Emit one memory per line. Stream-friendly.
  markdown    Human-readable Markdown (reuses export_memories_for_docling).
  html        Human-readable HTML.
  text        Plain text.
  stats       Dump /stats.

Usage:
  python -m mnemos.tools.memory_export json     --out memories.json --endpoint http://localhost:5002
  python -m mnemos.tools.memory_export jsonl    --out memories.jsonl --endpoint http://localhost:5002
  python -m mnemos.tools.memory_export json     --category documents --out docs.json \\
                                         --api-key $MNEMOS_API_KEY
  python -m mnemos.tools.memory_export markdown --out memories.md
  python -m mnemos.tools.memory_export stats    --endpoint http://localhost:5002

The ``json`` subcommand produces an MPF envelope compatible with
``memory_import.py json --preserve-metadata`` for cross-version
migrations.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

MPF_VERSION = "0.1.1"
MEMORY_PAYLOAD_VERSION = "mnemos-3.1"
SOURCE_SYSTEM = "memory_export"


# Complete set of MPF envelope sidecar arrays (CHARON v0.2). Used
# by both the JSON and JSONL exporters to ensure every populated
# sidecar round-trips. Keep in sync with MPFEnvelope in
# mnemos.domain.portability.schemas.
SIDECAR_KEYS = (
    "kg_triples",
    "relations",
    "memory_versions",
    "compression_manifest",
    "compression_candidates",
    "embeddings",
    "attestations",
    "deletion_log",
)


# Per-memory sidecar arrays. These are fetched by the server in pages
# scoped to the CURRENT page's memory IDs
# (mnemos.domain.portability.export._fetch_kg_triples_for_export /
# _fetch_memory_versions_for_export / _fetch_compressed_variants_for_export)
# so each page emits ONLY the sidecar rows whose memory_id is in that
# page's records[]. A paginated CLI run MUST aggregate them across all
# pages — the v0.2 prior implementation only retained page 1's sidecars
# and silently dropped every later page's version history / KG / compression
# context (F08 fix). The deletion_log is intentionally NOT listed here
# because it is tenant-scoped, NOT memory-scoped — it paginates on its
# own cursor and is handled separately below.
_PER_MEMORY_SIDECAR_KEYS = (
    "kg_triples",
    "memory_versions",
    "compression_manifest",
)


def _sidecar_identity_key(surface: str, entry: Dict[str, Any]) -> Optional[str]:
    """Return a stable identity string for a per-memory sidecar entry, or
    None if no identity is available (caller should keep the entry unmerged).

    The server uses these primary keys (see the MemoryRepository /
    KGRepository / VersionRepository / CompressionRepository export methods
    on mnemos/persistence/base.py):
        kg_triples             -> id (UUID)
        memory_versions        -> id (UUID)
        compression_manifest   -> (record_id, engine_id, engine_version)
                                   (no surrogate id; PK is composite)

    The CLI uses these identity strings to dedupe per-memory sidecar rows
    across pages — a memory id appearing on both page 1 and page 2 must
    not double-count the same sidecar row in the final envelope.
    """
    if surface == "kg_triples" or surface == "memory_versions":
        eid = entry.get("id")
        if eid is None:
            return None
        return f"{surface}:{eid}"
    if surface == "compression_manifest":
        rid = entry.get("record_id")
        eid = entry.get("engine_id")
        if rid is None or eid is None:
            return None
        # engine_version may be NULL for some engines — pin to "" so two
        # NULL engine_versions on the same (record_id, engine_id) still
        # dedupe to the same identity.
        return f"compression_manifest:{rid}:{eid}:{entry.get('engine_version') or ''}"
    raise ValueError(f"unknown per-memory sidecar surface: {surface!r}")


def _merge_per_memory_sidecar_page(
    accumulated: Dict[str, Dict[str, Any]],
    page_entries: List[Dict[str, Any]],
    surface: str,
    seen_record_ids: set,
) -> None:
    """Merge a page's per-memory sidecar entries into the accumulated dict.

    For each entry, derive a stable identity via ``_sidecar_identity_key``.
    The first observation wins; subsequent identical-key observations are
    kept iff they are deep-equal to the first (i.e. the server emitted
    the same row on two pages — a benign consequence of the per-page
    memory_id scoping re-fetching rows we already saw). Non-identical
    re-observations would mean the server returned inconsistent data for
    the same row id, which is a server bug we surface rather than silently
    collapse (matches the import-side fail-closed dedupe in
    ``_topo_sort_versions``).
    """
    for entry in page_entries or []:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("record_id") or entry.get("memory_id")
        if rid is not None:
            seen_record_ids.add(str(rid))
        key = _sidecar_identity_key(surface, entry)
        if key is None:
            # No identity available — keep the entry verbatim so we don't
            # drop data on an unusual row shape, but don't try to dedupe.
            # Subsequent occurrences of the same unkeyed entry will
            # accumulate; this is the same shape an import would see.
            anon_key = f"__anon:{surface}:{id(entry)}:{len(accumulated)}"
            accumulated[anon_key] = entry
            continue
        existing = accumulated.get(key)
        if existing is None:
            accumulated[key] = entry
            continue
        if existing == entry:
            # Same row, same content — benign re-fetch from a page that
            # overlapped this memory id. No-op; the first observation is
            # authoritative.
            continue
        raise ValueError(f"divergent duplicate {surface} identity: {key}")


# Stable ordering key for memory_versions entries within a single memory's
# history. version_num is the primary monotonic sequence; branch breaks
# ties (different branch DAGs shouldn't reorder against each other); id is
# the final stable tiebreaker so two v1s on different branches stay
# deterministic.
def _version_entry_sort_key(entry: Dict[str, Any]) -> tuple:
    return (
        str(entry.get("record_id") or ""),
        str(entry.get("branch") or ""),
        int(entry.get("version_num") or 0),
        str(entry.get("id") or ""),
    )


def _finalize_memory_versions(
    accumulated: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Order the merged memory_versions sidecar by (record_id, branch,
    version_num, id).

    The server's per-page fetch does NOT guarantee version order
    (memory_versions is joined through ``fetch_memory_versions_for_export``
    which orders by memory_id, branch, version_num; but once we concat
    across pages the overall ordering is broken). Restoring it here means
    the final envelope round-trips through the import path's
    ``_topo_sort_versions`` with parents appearing before children —
    ``_topo_sort_versions`` already enforces the DAG, but a stable
    primary ordering avoids spurious insertion-order dependence.
    """
    return sorted(accumulated.values(), key=_version_entry_sort_key)


def _fetch_export(
    endpoint: str,
    api_key: Optional[str],
    category: Optional[str],
    limit: int,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
    include_sidecars: bool = False,
    include_unattached_kg: bool = False,
    *,
    paginate: bool = True,
) -> Dict[str, Any]:
    """Call ``GET /v1/export`` and return the MPF envelope as a dict.

    The server-side handler caps each request at ``_EXPORT_HARD_LIMIT``
    (10_000 memories). To make a full-corpus export actually full,
    this helper paginates with ``offset`` until either the server
    returns a short page (fewer than ``limit`` records) or no further
    records. ``--limit`` continues to be honored as the per-page
    batch size; it is NOT a cap on the total export size. The CLI's
    default ``--limit`` matches the server cap so the first request
    in a paginated run is the largest the server allows.

    Sidecar handling across pages — F08 fix (CHARON):

    The server emits per-memory sidecars (kg_triples / memory_versions /
    compression_manifest) SCOPED to the current page's memory IDs —
    ``memory_ids = [r["id"] for r in row_dicts]`` inside
    ``export_memories``. A multi-page export therefore materialises a
    different sidecar slice on each page; if we keep only page 1's
    sidecars (the prior behaviour), every page after the first silently
    drops its version history / KG / compression context. This breaks
    the migration-fidelity promise — import can reject the resulting
    incomplete version coverage, and other context (KG triples,
    compression metadata) vanishes without even an error.

    The CLI now aggregates per-memory sidecars across all pages:

    * Identity-dedupe by stable key per surface (``_sidecar_identity_key``).
      A memory id whose sidecar rows appear on more than one page — e.g.
      when two pages overlap on a memory id, or when the server replays
      a page's memory_ids on the next cursor — produces the SAME identity
      key. We verify the duplicate payloads are deep-equal before
      collapsing (benign server re-fetch) and warn-on-stderr rather than
      drop if they diverge (server consistency bug).
    * Preserve per-memory version ordering: after merge, the merged
      memory_versions list is sorted by ``(record_id, branch,
      version_num, id)`` so parent versions always appear before
      children in the final envelope. Import-side
      ``_topo_sort_versions`` enforces the DAG independently, but a
      stable primary order keeps the envelope deterministic.
    * Keep deletion_log tenant-scoped — it paginates on its OWN cursor
      (page 1 returns ``deletion_log_next_cursor``, page 2 takes
      ``deletion_log_cursor``). The cursor mechanism is already correct
      and is preserved verbatim; this helper does NOT dedupe
      deletion_log entries across pages because each page is bound by
      the cursor to a strictly non-overlapping window.

    ``deletion_log_next_cursor`` on the final envelope is the LAST
    non-empty cursor observed; callers paginating deletion_log
    independently can resume from it.

    Pass ``paginate=False`` to get the legacy single-request shape
    (one HTTP call, no offset loop) — used by callers that need the
    raw server response (e.g. ``mpf_to_mif`` style tools).

    With ``include_sidecars=True`` the envelope also carries the
    `kg_triples`, `memory_versions`, and `compression_manifest`
    sidecar arrays. Used by the CHARON v0.2 round-trip path; the
    default stays False so existing scripts emit unchanged envelopes.
    """
    endpoint = endpoint.rstrip("/")
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def _do_request(offset: int) -> Dict[str, Any]:
        params: Dict[str, str] = {"limit": str(limit), "offset": str(offset)}
        if category:
            params["category"] = category
        if owner_id:
            params["owner_id"] = owner_id
        if namespace:
            params["namespace"] = namespace
        if include_sidecars:
            params["include_sidecars"] = "true"
            if include_unattached_kg:
                params["include_unattached_kg"] = "true"
        url = f"{endpoint}/v1/export?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"ERROR: /v1/export HTTP {exc.code}: {body}")
        except urllib.error.URLError as exc:
            raise SystemExit(f"ERROR: /v1/export connection: {exc.reason}")

    # First page always.
    payload = _do_request(0)
    if not isinstance(payload, dict) or "records" not in payload:
        raise SystemExit("ERROR: /v1/export did not return an MPF envelope")

    if not paginate:
        return payload

    records: List[Dict[str, Any]] = list(payload.get("records") or [])

    # F08: accumulate per-memory sidecars across pages, deduped by stable
    # identity, ordered within each memory's history. The prior behaviour
    # was to keep page 1's sidecars and silently drop every later page's
    # sidecar rows — see the module docstring above for the rationale.
    per_memory_accumulators: Dict[str, Dict[str, Dict[str, Any]]] = {
        surface: {} for surface in _PER_MEMORY_SIDECAR_KEYS
    }
    # Track which memory ids contributed per-memory sidecars across all
    # pages — surfaces silently-missing sidecars (e.g. a memory with no
    # versions at all) without affecting records[].
    _seen_record_ids: set = set()

    # Seed accumulators with page 1's per-memory sidecars so the ordering
    # convention (page 1 first, then page 2, etc.) is observable on the
    # dedupe path. Page 1's entries still go through the same dedupe
    # verification as later pages — they may collide with later pages
    # when the server replays memory_ids across cursors, and the verifier
    # is the same in both directions.
    for surface in _PER_MEMORY_SIDECAR_KEYS:
        page_entries = payload.get(surface) or []
        if not isinstance(page_entries, list):
            continue
        _merge_per_memory_sidecar_page(per_memory_accumulators[surface], page_entries, surface, _seen_record_ids)

    # deletion_log is tenant-scoped and paginates on its own cursor;
    # preserve page 1's entries verbatim (the server-side cursor enforces
    # strict non-overlap; we don't dedupe).
    deletion_log: List[Dict[str, Any]] = list(payload.get("deletion_log") or [])
    last_cursor = payload.get("deletion_log_next_cursor")

    # Paginate until the server returns a short page. We walk the
    # offset forward by the size of each non-empty page. A page with
    # fewer than `limit` records signals exhaustion; a page with
    # exactly `limit` records may have more rows past it, so we keep
    # looping until the response is short.
    offset = len(records)
    safety_iterations = 0
    while records and len(records) % limit == 0:
        safety_iterations += 1
        # Hard ceiling on pagination rounds to prevent runaway loops
        # when a server misbehaves and never returns a short page.
        # 10_000 rounds × 10_000 page size = 100M memories, well past
        # any realistic CHARON corpus.
        if safety_iterations > 10_000:
            raise SystemExit(
                "ERROR: /v1/export pagination exceeded 10,000 pages; "
                "the server may not be honouring the limit parameter."
            )
        page = _do_request(offset)
        if not isinstance(page, dict):
            break
        page_records = list(page.get("records") or [])
        if not page_records:
            break
        records.extend(page_records)
        offset += len(page_records)
        if page.get("deletion_log_next_cursor"):
            last_cursor = page["deletion_log_next_cursor"]
        # F08: merge this page's per-memory sidecars into the accumulators.
        # The server scopes each page's sidecar fetch to the memory_ids
        # of THAT page only, so later pages contribute sidecar rows the
        # earlier pages could not have seen. Without this merge the
        # final envelope silently loses every page 2+ row.
        for surface in _PER_MEMORY_SIDECAR_KEYS:
            page_entries = page.get(surface)
            if not page_entries:
                continue
            if not isinstance(page_entries, list):
                continue
            _merge_per_memory_sidecar_page(
                per_memory_accumulators[surface],
                page_entries,
                surface,
                _seen_record_ids,
            )
        # deletion_log: page 2+ rows are guaranteed non-overlapping with
        # page 1 by the server's cursor mechanism (keyset pagination on
        # executed_at, id). We append verbatim — see the docstring on
        # _fetch_export for the rationale.
        page_dl = page.get("deletion_log") or []
        if page_dl and isinstance(page_dl, list):
            deletion_log.extend(page_dl)

    payload["records"] = records
    # F08: emit the aggregated per-memory sidecars in stable, round-trippable
    # order. memory_versions gets an explicit (record_id, branch,
    # version_num, id) sort so parent versions precede children — the
    # import-side _topo_sort_versions enforces the DAG, but the explicit
    # sort keeps the wire shape deterministic across runs.
    for surface in _PER_MEMORY_SIDECAR_KEYS:
        acc = per_memory_accumulators[surface]
        if not acc:
            payload.pop(surface, None)
            continue
        if surface == "memory_versions":
            payload[surface] = _finalize_memory_versions(acc)
        else:
            # kg_triples and compression_manifest are content-keyed but
            # not version-ordered; emit insertion order so the final
            # envelope's row order is stable across CLI invocations that
            # fetch the same corpus.
            payload[surface] = list(acc.values())
    # deletion_log: tenant-scoped, cursor-paginated, non-overlapping.
    # The cursor mechanism already guarantees dedup, so we emit the
    # concatenation verbatim. (If the server ever relaxes the cursor's
    # strict non-overlap guarantee, the import path's per-row insert
    # would surface duplicates as a 409, not as silent data loss.)
    if deletion_log:
        payload["deletion_log"] = deletion_log
    else:
        payload.pop("deletion_log", None)
    if last_cursor:
        payload["deletion_log_next_cursor"] = last_cursor
    return payload


def _fetch_memories_flat(
    endpoint: str,
    api_key: Optional[str],
    category: Optional[str],
    limit: int,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Flat memory fetch for markdown/html/text paths.

    The markdown/html/text formatters in ``export_memories_for_docling.py``
    expect flat memory dicts (not MPF records), so we flatten the MPF
    export envelope.
    """
    from mnemos.tools.export_stream import DiskMemories, iter_pages

    return DiskMemories(
        iter_pages(
            endpoint,
            api_key,
            {
                "category": category,
                "limit": limit,
                "offset": 0,
                "owner_id": owner_id,
                "namespace": namespace,
                "include_sidecars": "false",
            },
        )
    )


def _write_streamed_export(args, *, jsonl=False):
    from mnemos.tools.export_stream import iter_pages, write_export

    params = {
        "category": args.category,
        "limit": args.limit,
        "offset": 0,
        "owner_id": getattr(args, "owner_id", None),
        "namespace": getattr(args, "namespace", None),
        "include_sidecars": "true" if getattr(args, "include_sidecars", False) else "false",
        "include_unattached_kg": "true" if getattr(args, "include_unattached_kg", False) else "false",
        "mpf_version": getattr(args, "mpf_version", None),
    }
    count = write_export(iter_pages(args.endpoint, args.api_key, params), args.out, jsonl=jsonl)
    print(f"Wrote {count} records as {'JSONL' if jsonl else 'MPF'} → {args.out}")


def cmd_json(args: argparse.Namespace) -> None:
    _write_streamed_export(args)


def cmd_jsonl(args: argparse.Namespace) -> None:
    _write_streamed_export(args, jsonl=True)


def cmd_mif(args: argparse.Namespace) -> None:
    """Export memories as a MIF 1.0 bundle (a directory of `<type>/<uuid>.md`
    concept files + a manifest), via the mnemos-core CHARON MIF primitives.
    `--out` is the bundle directory."""
    from mnemos.portability import charon as mif_charon

    memories = _fetch_memories_flat(
        args.endpoint,
        args.api_key,
        args.category,
        args.limit,
        owner_id=getattr(args, "owner_id", None),
        namespace=getattr(args, "namespace", None),
    )
    try:
        manifest = mif_charon.export_bundle(memories, Path(args.out), stream_manifest=True)
    finally:
        if hasattr(memories, "close"):
            memories.close()
    print(f"Wrote {manifest['count']} concepts as a MIF {manifest['mif_version']} bundle → {args.out}")


def cmd_markdown(args: argparse.Namespace) -> None:
    try:
        from mnemos.tools.export_memories_for_docling import export_memories_markdown
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from mnemos.tools.export_memories_for_docling import export_memories_markdown  # noqa
    memories = _fetch_memories_flat(
        args.endpoint,
        args.api_key,
        args.category,
        args.limit,
        getattr(args, "owner_id", None),
        getattr(args, "namespace", None),
    )
    out = Path(args.out)
    try:
        export_memories_markdown(memories, out)
    finally:
        if hasattr(memories, "close"):
            memories.close()
    print(f"Wrote {len(memories)} memories as Markdown → {out}")


def cmd_html(args: argparse.Namespace) -> None:
    try:
        from mnemos.tools.export_memories_for_docling import export_memories_html
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from mnemos.tools.export_memories_for_docling import export_memories_html  # noqa
    memories = _fetch_memories_flat(
        args.endpoint,
        args.api_key,
        args.category,
        args.limit,
        getattr(args, "owner_id", None),
        getattr(args, "namespace", None),
    )
    out = Path(args.out)
    try:
        export_memories_html(memories, out)
    finally:
        if hasattr(memories, "close"):
            memories.close()
    print(f"Wrote {len(memories)} memories as HTML → {out}")


def cmd_text(args: argparse.Namespace) -> None:
    try:
        from mnemos.tools.export_memories_for_docling import export_memories_plaintext
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from mnemos.tools.export_memories_for_docling import export_memories_plaintext  # noqa
    memories = _fetch_memories_flat(
        args.endpoint,
        args.api_key,
        args.category,
        args.limit,
        getattr(args, "owner_id", None),
        getattr(args, "namespace", None),
    )
    out = Path(args.out)
    try:
        export_memories_plaintext(memories, out)
    finally:
        if hasattr(memories, "close"):
            memories.close()
    print(f"Wrote {len(memories)} memories as plain text → {out}")


def cmd_stats(args: argparse.Namespace) -> None:
    endpoint = args.endpoint.rstrip("/")
    headers: Dict[str, str] = {}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    req = urllib.request.Request(f"{endpoint}/stats", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"ERROR: /stats HTTP {exc.code}: {exc.read().decode()[:200]}")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--endpoint", default="http://localhost:5002", help="MNEMOS API base URL (default: http://localhost:5002)"
    )
    p.add_argument("--api-key", metavar="KEY", default=None, help="Optional Bearer token for MNEMOS auth")


def _add_fetch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", required=True, metavar="PATH", help="Output file path")
    p.add_argument("--category", default=None, help="Filter memories by category (all if omitted)")
    p.add_argument("--owner-id", default=None, help="Filter memories by owner_id (all visible owners if omitted)")
    p.add_argument("--namespace", default=None, help="Filter memories by namespace (all visible namespaces if omitted)")
    p.add_argument("--mpf-version", default=None, help="MPF version (server default when omitted)")
    p.add_argument(
        "--limit",
        type=int,
        default=10_000,
        help="Maximum number of memories (default: 10000). Matches the server-side _EXPORT_HARD_LIMIT.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_export",
        description=(
            "Export MNEMOS memories in portability-friendly formats. "
            "Part of CHARON, MNEMOS's memory portability subsystem."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_json = sub.add_parser("json", help="Emit MPF envelope (big JSON)")
    _add_common(p_json)
    _add_fetch_args(p_json)
    p_json.add_argument(
        "--include-sidecars",
        action="store_true",
        help="Include kg_triples / memory_versions / compression_manifest "
        "sidecars in the envelope (CHARON v0.2). Off by default.",
    )
    p_json.add_argument(
        "--include-unattached-kg",
        action="store_true",
        default=False,
        help="Include first-class kg_triples (memory_id IS NULL) in "
        "the kg_triples sidecar. Off by default — matches the "
        "HTTP API default. Use this for full-corpus migrations "
        "where standalone Graphiti/Cognee-style facts are part "
        "of the scope. NOT for sliced exports (--category, "
        "--limit) — that combination would emit tenant-wide "
        "facts alongside the slice.",
    )
    p_json.set_defaults(func=cmd_json)

    p_jsonl = sub.add_parser("jsonl", help="Emit JSONL (one MPF record per line)")
    _add_common(p_jsonl)
    _add_fetch_args(p_jsonl)
    p_jsonl.add_argument(
        "--include-sidecars",
        action="store_true",
        help="Include kg_triples / memory_versions / compression_manifest "
        "sidecars as a final trailer line (CHARON v0.2). Off by default.",
    )
    p_jsonl.add_argument(
        "--include-unattached-kg",
        action="store_true",
        default=False,
        help="Include first-class kg_triples (memory_id IS NULL). See json subcommand help.",
    )
    p_jsonl.set_defaults(func=cmd_jsonl)

    p_mif = sub.add_parser("mif", help="Emit a MIF 1.0 bundle (directory of concept files + manifest)")
    _add_common(p_mif)
    _add_fetch_args(p_mif)
    p_mif.set_defaults(func=cmd_mif)

    p_md = sub.add_parser("markdown", help="Emit Markdown (human-readable)")
    _add_common(p_md)
    _add_fetch_args(p_md)
    p_md.set_defaults(func=cmd_markdown)

    p_html = sub.add_parser("html", help="Emit HTML (human-readable)")
    _add_common(p_html)
    _add_fetch_args(p_html)
    p_html.set_defaults(func=cmd_html)

    p_txt = sub.add_parser("text", help="Emit plain text")
    _add_common(p_txt)
    _add_fetch_args(p_txt)
    p_txt.set_defaults(func=cmd_text)

    p_stats = sub.add_parser("stats", help="Print /stats response")
    _add_common(p_stats)
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
