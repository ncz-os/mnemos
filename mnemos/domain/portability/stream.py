"""Bounded MPF pages from one repeatable-read snapshot.

Backend-neutral: the snapshot comes from
``backend.transactional(isolation="repeatable_read", readonly=True)``, which
every one of the six persistence backends honors (see that method for the
per-backend mapping, including how SQLite reaches the same guarantee through
WAL rather than through an isolation level).
"""

import asyncio
import json
from datetime import UTC, datetime

from mnemos.core.config import export_stream_timeout_seconds_env

from .export import (
    _build_export_sidecars,
    _export_record_batch_size,
    _iter_page_records,
    _resolve_deletion_log_cursor,
    _resolve_export_scope,
    _resolve_export_version,
    export_memories,
)
from .schemas import SOURCE_SYSTEM, SOURCE_VERSION


async def stream_export(backend, **options):
    """Yield envelopes and a completion marker; cancellation releases the snapshot.

    Memory pages and the independent tombstone cursor run inside ONE
    repeatable-read readonly transaction. A missing completion marker means the
    client must discard its staged export.
    """
    timeout = export_stream_timeout_seconds_env()
    async with asyncio.timeout(timeout):
        async with backend.transactional(isolation="repeatable_read", readonly=True) as tx:
            count = 0
            cursor = options.get("deletion_log_cursor")
            first = True
            while True:
                envelope = await export_memories(backend, tx, **options)
                page = envelope.model_dump(mode="json", exclude_none=True)
                records = page["records"]
                count += len(records)
                if first:
                    cursor = page.pop("deletion_log_next_cursor", None)
                    first = False
                else:
                    page.pop("deletion_log", None)
                    page.pop("deletion_log_next_cursor", None)
                yield json.dumps(page, separators=(",", ":")) + "\n"
                if len(records) < options["limit"]:
                    break
                last = records[-1]
                created = datetime.fromisoformat(last["payload"]["created"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                options["record_cursor"] = (created, last["id"])
                options["offset"] = 0
            seen = set()
            while cursor:
                if cursor in seen:
                    raise RuntimeError("deletion export cursor did not advance")
                seen.add(cursor)
                deletion_options = dict(
                    options,
                    deletion_log_cursor=cursor,
                    deletion_log_from=None,
                    deletion_log_to=None,
                    owner_id=None,
                    namespace=None,
                    offset=0,
                    limit=1,
                    record_cursor=None,
                )
                envelope = await export_memories(backend, tx, **deletion_options)
                page = envelope.model_dump(mode="json", exclude_none=True)
                cursor = page.get("deletion_log_next_cursor")
                yield (
                    json.dumps(
                        {
                            "records": [],
                            "deletion_log": page.get("deletion_log", []),
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            yield (json.dumps({"export_complete": True, "record_count": count}) + "\n")


# ── Intra-page streaming export (CHARON roadmap Feature 4/5) ──────────────
#
# `stream_export()` above streams ACROSS pages but buffers WITHIN one: it
# calls `export_memories()`, which materializes the whole page's records, and
# then emits the page as a single ndjson line. `stream_export_records()` keeps
# every guarantee that function makes and additionally emits ONE ndjson LINE
# PER RECORD, so memory-record buffering is O(batch) instead of O(limit).
# Page IDs and capped sidecars still consume page-sized memory. The client
# sees the first record without waiting for the last.
#
# Wire format (opt-in via `stream_records=true`; the legacy format is
# untouched and remains the default):
#
#   {"records":[],"mpf_version":"...","source_system":"...",   <- header, once
#    "source_version":"...","exported_at":"..."}
#   {"records":[<record>]}                                     <- one per record
#   {"records":[<record>]}
#   {"records":[],"kg_triples":[...],"memory_versions":[...],  <- page boundary
#    "compression_manifest":[...],"deletion_log":[...],
#    "deletion_log_next_cursor":"..."}
#   ... (further pages: records then boundary) ...
#   {"records":[],"deletion_log":[...]}                        <- cursor drain
#   {"export_complete":true,"record_count":N}
#
# Every line is a well-formed page in the sense `mnemos.tools.export_stream`
# already understands: `write_export` takes envelope metadata from the FIRST
# page (hence the header line), tolerates pages with no records (its
# "did not advance" check is guarded on a non-empty records list), dedupes
# sidecars by identity, and checks the completion count against the sum of
# `len(page["records"])`. So the existing client consumes this format with no
# changes -- no coalescing shim is required on the reader side.


async def stream_export_records(backend, **options):
    """Stream one ndjson line per record; identical snapshot to stream_export.

    The invariant, and how it is guaranteed rather than hoped for:

    * ONE `repeatable_read` readonly transaction wraps the entire export --
      opened here and never left until the generator is exhausted or closed,
      exactly as in `stream_export()`. Every record fetch, every sidecar fetch
      and the deletion-log drain all run on that `tx`, so they all observe one
      snapshot. Nothing is fetched in a second transaction.
    * Records come from `export._iter_page_records`, which is the SAME
      generator `export_memories()` uses for the buffered path, and which
      only ever reaches the database through
      `backend.memories.fetch_memory_export`. The
      WHERE clause, the tenant/vault filters and `ORDER BY created ASC,
      id ASC` therefore cannot drift from the buffered path -- there is no
      second copy of that SQL to drift.
    * Authorization and tenant scope come from `_resolve_export_scope` and
      `_resolve_deletion_log_cursor`, the same helpers `export_memories()`
      calls, so `include_secrets requires root`, the cross-owner and
      cross-namespace 403s and cursor forgery rejection all apply here too.
    * Sidecars come from `_build_export_sidecars`, the same helper, keyed off
      the page's own memory ids.

    The only intended difference from `stream_export()` is WHEN bytes leave
    the process, never WHICH bytes.
    """
    limit = options["limit"]
    user = options.get("user")
    category = options.get("category")
    include_sidecars = bool(options.get("include_sidecars"))
    include_unattached_kg = bool(options.get("include_unattached_kg"))
    include_secrets = bool(options.get("include_secrets"))
    emit_version = _resolve_export_version(options.get("mpf_version"))

    # Authorize BEFORE opening a transaction: a 403 must not first check out a
    # connection and open a snapshot it will immediately abandon.
    effective_owner, effective_ns, redact_secrets = _resolve_export_scope(
        user,
        owner_id=options.get("owner_id"),
        namespace=options.get("namespace"),
        category=category,
        include_sidecars=include_sidecars,
        include_secrets=include_secrets,
    )
    cursor_state = _resolve_deletion_log_cursor(
        options.get("deletion_log_cursor"),
        user=user,
        emit_version=emit_version,
        effective_owner=effective_owner,
        effective_ns=effective_ns,
    )
    effective_owner = cursor_state["effective_owner"]
    effective_ns = cursor_state["effective_ns"]
    batch_size = _export_record_batch_size(limit)

    timeout = export_stream_timeout_seconds_env()
    async with asyncio.timeout(timeout):
        async with backend.transactional(isolation="repeatable_read", readonly=True) as tx:
            header = {
                "records": [],
                "mpf_version": emit_version,
                "source_system": SOURCE_SYSTEM,
                "source_version": SOURCE_VERSION,
                "exported_at": datetime.now(UTC).isoformat(),
            }
            if include_secrets:
                header["includes_secrets"] = True
            yield json.dumps(header, separators=(",", ":")) + "\n"

            count = 0
            deletion_cursor = options.get("deletion_log_cursor")
            first = True
            record_cursor = options.get("record_cursor")
            page_offset = int(options.get("offset") or 0)
            while True:
                memory_ids = []
                last_keyset = None
                async for record, created in _iter_page_records(
                    backend,
                    tx,
                    effective_owner=effective_owner,
                    effective_ns=effective_ns,
                    category=category,
                    limit=limit,
                    offset=page_offset,
                    include_secrets=include_secrets,
                    record_cursor=record_cursor,
                    emit_version=emit_version,
                    redact_secrets=redact_secrets,
                    batch_size=batch_size,
                ):
                    memory_ids.append(record.id)
                    # Keyset advances from the raw DB `created`, not from
                    # the serialized payload -- see _iter_page_records.
                    last_keyset = (created, record.id)
                    count += 1
                    yield (
                        json.dumps(
                            {"records": [record.model_dump(mode="json", exclude_none=True)]},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                page_size = len(memory_ids)

                # Sidecars are page-level, so they are computed once the
                # page's ids are known and emitted on a boundary line.
                # The deletion_log and its cursor ride the FIRST page only,
                # mirroring stream_export()'s page.pop on later pages.
                sidecars = await _build_export_sidecars(
                    backend,
                    tx,
                    memory_ids=memory_ids,
                    include_sidecars=include_sidecars,
                    include_unattached_kg=include_unattached_kg,
                    include_secrets=include_secrets,
                    emit_version=emit_version,
                    redact_secrets=redact_secrets,
                    effective_owner=effective_owner,
                    effective_ns=effective_ns,
                    cursor_data=cursor_state["cursor_data"],
                    cursor_dl_from=cursor_state["cursor_dl_from"],
                    cursor_dl_to=cursor_state["cursor_dl_to"],
                    cursor_export_as_of=cursor_state["cursor_export_as_of"],
                    cursor_executed_at=cursor_state["cursor_executed_at"],
                    cursor_row_id=cursor_state["cursor_row_id"],
                    deletion_log_from=options.get("deletion_log_from"),
                    deletion_log_to=options.get("deletion_log_to"),
                )
                boundary = {"records": []}
                for surface in (
                    "kg_triples",
                    "memory_versions",
                    "compression_manifest",
                ):
                    if sidecars[surface] is not None:
                        boundary[surface] = sidecars[surface]
                if first:
                    if sidecars["deletion_log"] is not None:
                        boundary["deletion_log"] = sidecars["deletion_log"]
                    deletion_cursor = sidecars["deletion_log_next_cursor"]
                    if deletion_cursor is not None:
                        boundary["deletion_log_next_cursor"] = deletion_cursor
                    first = False
                yield json.dumps(boundary, separators=(",", ":")) + "\n"

                if page_size < limit or last_keyset is None:
                    break
                record_cursor = last_keyset
                page_offset = 0

            # Tombstones drain on their own cursor, independent of the
            # memory keyset -- same shape and same guard as stream_export.
            seen = set()
            while deletion_cursor:
                if deletion_cursor in seen:
                    raise RuntimeError("deletion export cursor did not advance")
                seen.add(deletion_cursor)
                deletion_options = dict(
                    options,
                    deletion_log_cursor=deletion_cursor,
                    deletion_log_from=None,
                    deletion_log_to=None,
                    owner_id=None,
                    namespace=None,
                    offset=0,
                    limit=1,
                    record_cursor=None,
                )
                envelope = await export_memories(backend, tx, **deletion_options)
                page = envelope.model_dump(mode="json", exclude_none=True)
                deletion_cursor = page.get("deletion_log_next_cursor")
                yield (
                    json.dumps(
                        {
                            "records": [],
                            "deletion_log": page.get("deletion_log", []),
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            yield json.dumps({"export_complete": True, "record_count": count}) + "\n"
