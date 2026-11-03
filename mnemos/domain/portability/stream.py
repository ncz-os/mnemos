"""Bounded MPF pages from one repeatable-read snapshot.

Backend-neutral: the snapshot comes from
``backend.transactional(isolation="repeatable_read", readonly=True)``, which
every one of the six persistence backends honors (see that method for the
per-backend mapping, including how SQLite reaches the same guarantee through
WAL rather than through an isolation level).

Every read this stream issues must therefore stay a SELECT: a write on a
``readonly=True`` scope now raises on ALL six backends (SQLite enforces it
with ``PRAGMA query_only=1``, backlog F-01), and this module contains none.
"""

import asyncio
import json
import logging
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

logger = logging.getLogger(__name__)


def _drop_seam_repeats(records, previous_ids, *, id_of):
    """Remove rows the previous page already emitted.

    A keyset cursor bound in a lossy domain (the Oracle thin-mode DATE
    coercion that drops fractional seconds, or the naive/aware shift fixed on
    2026-10-03) makes rows at or before the cursor compare as greater than it,
    so the next page starts by re-emitting the tail of the previous page.
    Observed live on PYTHIA 2026-11-03: three seam rows in a 28,455-row export,
    each byte-identical, and the completion marker counted them twice.

    Dropping them here is lossless (the snapshot already emitted them) and
    keeps the stream's invariant: every identity appears once, so the
    completion marker's ``record_count`` is the unique snapshot count.
    Only the previous page is consulted, which keeps memory O(limit).
    """
    if not previous_ids:
        return records, 0
    kept = [r for r in records if id_of(r) not in previous_ids]
    return kept, len(records) - len(kept)


def _log_seam_repeats(dropped, page_ids):
    if dropped:
        logger.warning(
            "export keyset re-emitted %d row(s) from the previous page; dropped "
            "(the cursor bind lost precision). ids: %s",
            dropped,
            ", ".join(sorted(page_ids)[:20]),
        )


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
            raw_last_created = None
            previous_ids = set()
            while True:
                envelope = await export_memories(backend, tx, **options)
                # The RAW `created` of the last row is internal state for
                # the keyset advance below — capture it BEFORE the
                # serialization that drops it (the wire envelope must not
                # carry backend-typed values).
                raw_last_created = envelope.raw_last_created
                page = envelope.model_dump(mode="json", exclude_none=True)
                page.pop("raw_last_created", None)
                raw_records = page["records"]
                records, dropped = _drop_seam_repeats(raw_records, previous_ids, id_of=lambda r: r["id"])
                if dropped:
                    _log_seam_repeats(dropped, {r["id"] for r in raw_records} & previous_ids)
                    if len(raw_records) >= options["limit"] and not records:
                        # A full page made only of rows already emitted: the
                        # keyset cannot advance and would loop forever.
                        raise RuntimeError("export keyset cursor did not advance past the page seam")
                    page["records"] = records
                count += len(records)
                if first:
                    cursor = page.pop("deletion_log_next_cursor", None)
                    first = False
                else:
                    page.pop("deletion_log", None)
                    page.pop("deletion_log_next_cursor", None)
                yield json.dumps(page, separators=(",", ":")) + "\n"
                if len(raw_records) < options["limit"]:
                    break
                previous_ids = {r["id"] for r in raw_records}
                last = raw_records[-1]
                # The cursor MUST be the RAW `created` value the backend's
                # export query binds against — the domain the keyset
                # predicate compares with on the next page. The serialized
                # payload re-renders `created` through the MPF serializer
                # (`_iso`), which normalizes to an aware ISO string and so
                # can shift the domain the raw row was stored/returned in:
                # e.g. SQLite stores naive ISO text and returns `str`, and
                # re-binding an aware datetime into that naive-text
                # comparison coerces the bind, so the boundary row compares
                # as greater than its own cursor and is re-emitted as the
                # first row of the next page (the 2026-10-03 STYX offsite
                # backup defect; same mechanism class as the 2026-05-23
                # federation feed ties).
                if raw_last_created is not None:
                    options["record_cursor"] = (raw_last_created, last["id"])
                else:
                    # Backward-compat fallback for a backend whose envelope
                    # does not carry the raw side channel: parse the payload
                    # and keep the result in the domain the payload was
                    # serialized in. The serializer (`_iso`) renders naive
                    # storage (SQLite ISO text, Oracle naive datetimes) as a
                    # naive string and aware storage as an aware string, so
                    # re-parsing WITHOUT re-attaching tzinfo preserves the
                    # original domain. Force-attaching UTC here would turn a
                    # naive cursor into an aware one and re-introduce the
                    # exact bind-coercion this fix removes on naive-text
                    # backends.
                    raw_created = datetime.fromisoformat(last["payload"]["created"])
                    options["record_cursor"] = (raw_created, last["id"])
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
            previous_ids = set()
            while True:
                memory_ids = []
                page_size = 0
                repeated = set()
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
                    page_size += 1
                    # Keyset advances from the raw DB `created`, not from
                    # the serialized payload -- see _iter_page_records.
                    last_keyset = (created, record.id)
                    if record.id in previous_ids:
                        # Seam re-emission; see _drop_seam_repeats.
                        repeated.add(record.id)
                        continue
                    memory_ids.append(record.id)
                    count += 1
                    yield (
                        json.dumps(
                            {"records": [record.model_dump(mode="json", exclude_none=True)]},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                _log_seam_repeats(len(repeated), repeated)
                if repeated and page_size >= limit and not memory_ids:
                    raise RuntimeError("export keyset cursor did not advance past the page seam")

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
                previous_ids = set(memory_ids) | repeated
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
