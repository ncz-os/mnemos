"""MPF import orchestration."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException
from mnemos.core.persisted_text_classification import classify_persisted_text_fields
from mnemos.core.security import is_root
from mnemos.persistence.base import (
    AuditPersistence,
    Transaction,
    is_duplicate_memory_error,
)

from .allowlist import _build_referenced_memory_allowlist
from .ids import _derive_caller_scoped_id
from .phases import (
    _import_compression_manifest,
    _import_kg_triples,
    _import_memory_versions,
    _restore_memory_branches,
)
from .schemas import (
    MEMORY_PAYLOAD_VERSION,
    MPF_VERSION_PREFIX,
    MPF_VERSION_PREFIX_V0_2,
    ImportStats,
    MPFEnvelope,
)
from .serializers import _MPF_V0_2_RECORD_FIELDS_METADATA_KEY
from .timestamps import _parse_iso_naive, _same_instant

logger = logging.getLogger(__name__)


def _new_stats() -> ImportStats:
    return ImportStats(
        imported=0,
        skipped=0,
        failed=0,
        unsupported_kinds={},
        sidecars_imported={},
        sidecars_skipped={},
        sidecars_failed={},
        errors=[],
    )


# NOTE: `_fetch_verbatim_content_for_legacy_core` used to live here. It was a
# raw asyncpg ``$1`` query -- the compatibility shim for core projections that
# omitted verbatim_content from fetch_memory_by_id. That column is now part of
# the projection on all six backends, so the shim is deleted rather than
# translated, and with it the last driver-specific SQL in the import path.


def _validate_import_request(envelope: MPFEnvelope, preserve_owner: bool, user) -> None:
    supported_prefixes = (MPF_VERSION_PREFIX, MPF_VERSION_PREFIX_V0_2)
    if not envelope.mpf_version.startswith(supported_prefixes):
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported MPF version {envelope.mpf_version!r}; "
                f"expected {MPF_VERSION_PREFIX}x or {MPF_VERSION_PREFIX_V0_2}x"
            ),
        )

    if envelope.deletion_log:
        raise HTTPException(
            status_code=415,
            detail="MPF deletion_log sidecar import is not supported",
        )

    unsupported_envelope_fields = [
        field
        for field in (
            "relations",
            "compression_candidates",
            "attestations",
        )
        if getattr(envelope, field)
    ]
    if envelope.embeddings is not None:
        unsupported_envelope_fields.append("embeddings")
    if unsupported_envelope_fields:
        raise HTTPException(
            status_code=415,
            detail=(
                "MPF envelope fields are not supported for import without "
                "data loss: " + ", ".join(sorted(unsupported_envelope_fields))
            ),
        )

    if preserve_owner and not is_root(user):
        raise HTTPException(status_code=403, detail="preserve_owner=true requires root")

    if envelope.memory_versions and not (preserve_owner and is_root(user)):
        raise HTTPException(
            status_code=403,
            detail=(
                "memory_versions sidecar import requires root + "
                "preserve_owner=true (the admin/migration path; "
                "use --preserve-metadata in tools/memory_import.py "
                "with a root bearer token). Non-root callers can "
                "import records and rely on the trigger-fired "
                "default v1 history, or ship kg_triples / "
                "compression_manifest sidecars without restriction. "
                "The non-root + memory_versions sidecar combination "
                "is not supported under CHARON v0.2 due to "
                "deterministic-id stale-state interactions."
            ),
        )

    if envelope.memory_versions:
        memory_record_ids = {r.id for r in envelope.records if r.kind == "memory"}
        sidecar_versioned_ids = {e.get("record_id") for e in envelope.memory_versions if e.get("record_id")}
        uncovered = memory_record_ids - sidecar_versioned_ids
        if uncovered:
            raise HTTPException(
                status_code=400,
                detail=(
                    "memory_versions sidecar must cover every kind: memory "
                    f"record being imported. {len(uncovered)} record(s) have "
                    f"no version entry: {sorted(uncovered)[:5]}"
                    f"{'...' if len(uncovered) > 5 else ''}. Either ship a "
                    "complete sidecar or omit the memory_versions array "
                    "entirely (the trigger will synthesize default v1 history)."
                ),
            )


async def import_memories(
    backend,
    tx: Transaction,
    *,
    envelope: MPFEnvelope,
    preserve_owner: bool,
    user,
) -> ImportStats:
    _validate_import_request(envelope, preserve_owner, user)
    stats = _new_stats()

    # The caller owns the outer transaction (the route opens
    # backend.transactional()), so the whole import still commits or rolls
    # back atomically -- the difference is that the scope is now explicit at
    # the call site instead of being opened here on a driver connection.
    if envelope.memory_versions:
        await backend.memories.set_suppress_version_snapshot(tx)

    inserted_record_ids: set = set()
    rejected_persisted_ids: set = set()
    id_remap: dict[str, str] = {}
    non_root_id_rewrite = not (preserve_owner and is_root(user))

    for record in envelope.records:
        if record.kind != "memory":
            stats.unsupported_kinds[record.kind] = stats.unsupported_kinds.get(record.kind, 0) + 1
            continue

        if record.payload_version != MEMORY_PAYLOAD_VERSION:
            stats.skipped += 1
            stats.errors.append(
                f"{record.id}: unsupported payload_version "
                f"{record.payload_version!r}; expected {MEMORY_PAYLOAD_VERSION}"
            )
            rejected_persisted_ids.add(record.id)
            continue

        p = record.payload
        if not isinstance(p, dict):
            stats.failed += 1
            stats.errors.append(f"{record.id}: kind 'memory' payload must be an object; skipped")
            rejected_persisted_ids.add(record.id)
            continue
        if preserve_owner:
            imported_owner = p.get("owner_id") or user.user_id
            imported_ns = p.get("namespace") or user.namespace
        else:
            imported_owner = user.user_id
            imported_ns = user.namespace

        content = p.get("content")
        if not content or not str(content).strip():
            stats.failed += 1
            stats.errors.append(f"{record.id}: empty content; skipped")
            rejected_persisted_ids.add(record.id)
            continue

        category = p.get("category") or "imported"
        subcategory = p.get("subcategory")
        permission_mode = p.get("permission_mode")
        if permission_mode is None:
            permission_mode = 600
        if (
            not isinstance(permission_mode, int)
            or isinstance(permission_mode, bool)
            or permission_mode < 0
            or permission_mode > 777
            or any(digit not in "01234567" for digit in str(permission_mode))
        ):
            stats.failed += 1
            stats.errors.append(f"{record.id}: permission_mode must be octal-style 0-777; skipped")
            rejected_persisted_ids.add(record.id)
            continue
        metadata = p.get("metadata") or {}
        if not isinstance(metadata, dict):
            stats.failed += 1
            stats.errors.append(f"{record.id}: metadata must be an object; skipped")
            rejected_persisted_ids.add(record.id)
            continue
        metadata = dict(metadata)
        # MPF v0.2 record-level fields (provenance, valid_time_*,
        # transaction_time) are sibling-of-payload per the v0.2 spec
        # but the memories schema has no first-class columns for
        # them. Persist them as explicitly-untrusted imported data
        # under payload.metadata so a round-trip re-export restores
        # them via the serializer bridge — without ever letting them
        # authenticate identity or attribution. If the envelope's
        # payload.metadata already carries a customer-owned value at
        # the bridge key, the importer preserves that customer value
        # AND stores the imported v0.2 envelope fields at a sibling
        # sub-key so they survive the round-trip; without the sibling
        # sub-key the v0.2 fields would be silently dropped on
        # re-export (the export side reconstructs from row columns
        # rather than the bridge).
        record_level_v02: dict[str, Any] = {}
        if envelope.mpf_version.startswith(MPF_VERSION_PREFIX_V0_2):
            if record.provenance is not None:
                record_level_v02["provenance"] = record.provenance
            if record.valid_time_start is not None:
                record_level_v02["valid_time_start"] = record.valid_time_start
            if record.valid_time_end is not None:
                record_level_v02["valid_time_end"] = record.valid_time_end
            if record.transaction_time is not None:
                record_level_v02["transaction_time"] = record.transaction_time
        if record_level_v02:
            if _MPF_V0_2_RECORD_FIELDS_METADATA_KEY not in metadata:
                # Customer hasn't claimed the bridge key — store the
                # v0.2 fields there.
                metadata[_MPF_V0_2_RECORD_FIELDS_METADATA_KEY] = record_level_v02
            else:
                # Customer owns the bridge key with their own value.
                # Preserve the customer value AND persist the imported
                # v0.2 envelope fields at a sibling sub-key so they
                # survive a re-export round-trip instead of being
                # silently dropped (asserted by
                # test_import_does_not_replace_customer_metadata_with_v02_bridge
                # — the customer value is preserved verbatim; the
                # imported v0.2 fields are stored at the sibling key).
                metadata[_MPF_V0_2_RECORD_FIELDS_METADATA_KEY + "_imported_envelope"] = record_level_v02
        quality_rating = p.get("quality_rating")
        if quality_rating is None:
            quality_rating = 75
        if not isinstance(quality_rating, int) or isinstance(quality_rating, bool) or not 0 <= quality_rating <= 100:
            stats.failed += 1
            stats.errors.append(f"{record.id}: quality_rating must be an integer from 0 to 100; skipped")
            rejected_persisted_ids.add(record.id)
            continue
        # `or content` FABRICATES a value when the envelope carries no
        # verbatim_content. That fallback is load-bearing for foreign/legacy
        # producers that never emit the field, so it stays -- but remember
        # whether the envelope actually ASSERTED one, because the idempotency
        # comparison below must not reject a row over a field the envelope
        # never claimed. See where this flag is used.
        envelope_asserts_verbatim = p.get("verbatim_content") is not None
        verbatim_content = p.get("verbatim_content") or content

        classified = classify_persisted_text_fields(
            content=content,
            verbatim_content=verbatim_content,
            metadata=metadata,
            namespace=imported_ns,
            classified_at="mpf_record_import",
            memory_id=record.id,
        )
        metadata = classified.metadata
        imported_ns = classified.namespace

        if non_root_id_rewrite:
            persisted_id = _derive_caller_scoped_id(
                record.id,
                caller_owner=imported_owner,
                caller_namespace=imported_ns,
                content=str(content),
            )
        else:
            persisted_id = record.id
        id_remap[record.id] = persisted_id

        try:
            # insert_memory does NOT signal a conflict uniformly: postgres,
            # mysql, mariadb, oracle and db2 return "INSERT 0 0", while SQLite
            # RAISES DuplicateMemoryError (deliberately -- POST /v1/memories
            # maps it to a 409). Checking only the return string would turn
            # every SQLite re-import into a hard failure. The savepoint is what
            # makes the raising path survivable: it rolls back just this
            # record's insert and leaves the enclosing import transaction
            # usable, which a bare try/except on a shared transaction would
            # not (on most backends the transaction would be poisoned).
            conflicted = False
            try:
                async with tx.savepoint():
                    row = await backend.memories.insert_memory(
                        tx,
                        memory_id=persisted_id,
                        content=content,
                        category=category,
                        subcategory=subcategory,
                        metadata_json=json.dumps(metadata),
                        quality_rating=quality_rating,
                        owner_id=imported_owner,
                        namespace=imported_ns,
                        permission_mode=permission_mode,
                        source_model=p.get("source_model"),
                        source_provider=p.get("source_provider"),
                        source_session=p.get("source_session"),
                        source_agent=p.get("source_agent"),
                        verbatim_content=verbatim_content,
                        created=_parse_iso_naive(p.get("created")),
                        updated=_parse_iso_naive(p.get("updated")),
                    )
                conflicted = row == "INSERT 0 0"
            except Exception as exc:
                if not is_duplicate_memory_error(exc):
                    raise
                conflicted = True
            if conflicted:
                existing_mem = await backend.memories.fetch_memory_by_id(tx, persisted_id)
                envelope_metadata_json = json.dumps(metadata, sort_keys=True)
                existing_metadata_json = (
                    json.dumps(
                        existing_mem["metadata"]
                        if isinstance(existing_mem["metadata"], dict)
                        else json.loads(existing_mem["metadata"] or "{}"),
                        sort_keys=True,
                    )
                    if existing_mem is not None
                    else None
                )
                envelope_created = _parse_iso_naive(p.get("created"))
                envelope_updated = _parse_iso_naive(p.get("updated"))
                mismatched_fields: list[str] = []
                if existing_mem is None:
                    mismatched_fields.append("row missing")
                else:
                    # fetch_memory_by_id returns verbatim_content on all six
                    # backends as of the matching core release, so the old
                    # raw-SQL backfill query is gone. charon still declares
                    # mnemos-core>=6.2 though, so an older core can hand back a
                    # projection without the column: treat that as "the stored
                    # value is unknown" and skip the comparison rather than
                    # raising KeyError mid-import.
                    try:
                        existing_verbatim = existing_mem["verbatim_content"]
                        db_asserts_verbatim = True
                    except (KeyError, IndexError):
                        existing_verbatim = None
                        db_asserts_verbatim = False
                    checks = [
                        ("content", existing_mem["content"], content),
                        ("category", existing_mem["category"], category),
                        ("subcategory", existing_mem["subcategory"], subcategory),
                        ("metadata", existing_metadata_json, envelope_metadata_json),
                        (
                            "quality_rating",
                            existing_mem["quality_rating"],
                            quality_rating,
                        ),
                        ("owner_id", existing_mem["owner_id"], imported_owner),
                        ("namespace", existing_mem["namespace"], imported_ns),
                        (
                            "permission_mode",
                            existing_mem["permission_mode"],
                            permission_mode,
                        ),
                        (
                            "source_model",
                            existing_mem["source_model"],
                            p.get("source_model"),
                        ),
                        (
                            "source_provider",
                            existing_mem["source_provider"],
                            p.get("source_provider"),
                        ),
                        (
                            "source_session",
                            existing_mem["source_session"],
                            p.get("source_session"),
                        ),
                        (
                            "source_agent",
                            existing_mem["source_agent"],
                            p.get("source_agent"),
                        ),
                    ]
                    # `memories.verbatim_content` is nullable with no default,
                    # so a NULL there is a legitimate stored state. The export
                    # omits the key entirely for such a row (envelopes are
                    # serialized exclude_none), and the import fallback above
                    # then substitutes `content`. Comparing that substituted
                    # value against the stored NULL reports a mismatch the
                    # importer itself manufactured, and a re-import of an
                    # envelope this server just produced fails with
                    # "verbatim_content differ" -- taking the record into
                    # rejected_persisted_ids, dropping it from the allowlist,
                    # and making EVERY sidecar fail with a misleading
                    # "cross-tenant attachment refused". Only compare the field
                    # when the envelope actually asserted one. (Same shape as
                    # the existing `tolerate_selected_at` rule in phases.py.)
                    if envelope_asserts_verbatim and db_asserts_verbatim:
                        checks.append(("verbatim_content", existing_verbatim, verbatim_content))
                    for col, db_val, env_val in checks:
                        if db_val != env_val:
                            mismatched_fields.append(col)
                    # Compare INSTANTS, not Python objects: SQLite returns
                    # these columns as ISO strings while asyncpg returns
                    # datetimes, so a direct != flagged every SQLite row as
                    # mismatched regardless of its actual value.
                    if envelope_created is not None and not _same_instant(existing_mem["created"], envelope_created):
                        mismatched_fields.append("created")
                    if envelope_updated is not None and not _same_instant(existing_mem["updated"], envelope_updated):
                        mismatched_fields.append("updated")

                if mismatched_fields:
                    stats.failed += 1
                    stats.errors.append(
                        f"{record.id}: existing memory row doesn't match "
                        f"envelope payload ({', '.join(mismatched_fields)} "
                        "differ); sidecar attachment refused"
                    )
                    id_remap.pop(record.id, None)
                    rejected_persisted_ids.add(persisted_id)
                    continue
                stats.skipped += 1
            else:
                stats.imported += 1
                inserted_record_ids.add(persisted_id)
        except Exception as exc:
            stats.failed += 1
            stats.errors.append(f"{record.id}: {type(exc).__name__}: {exc}")
            logger.exception("MPF import failed for record %s", record.id)
            id_remap.pop(record.id, None)
            rejected_persisted_ids.add(persisted_id)
            continue

        # Audit write happens OUTSIDE the insert try/except. An
        # enabled audit chain must fail the entire import (rolled
        # back via the outer transaction) so memory mutations
        # don't commit with permanent gaps in the tamper-evident
        # chain. Catching audit failures here would let the
        # memory row land while the chain is silently incomplete.
        if inserted_record_ids and backend is not None and tx is not None:
            # The insert above only adds to inserted_record_ids on
            # the non-conflict path. The audit write needs the
            # memory_id, content, category, subcategory, metadata
            # for the row that just landed; recompute them via the
            # current record iteration to keep the call self-contained.
            last_inserted = persisted_id
            await _write_mpf_import_audit_entry(
                backend,
                tx,
                memory_id=last_inserted,
                content=content,
                category=category,
                subcategory=subcategory,
                metadata=metadata,
                writer_id=user.user_id,
            )

    if id_remap:
        for entry in envelope.kg_triples or []:
            mid = entry.get("memory_id")
            if mid and mid in id_remap:
                entry["memory_id"] = id_remap[mid]
        for entry in envelope.memory_versions or []:
            rid = entry.get("record_id")
            if rid and rid in id_remap:
                entry["record_id"] = id_remap[rid]
        for entry in envelope.compression_manifest or []:
            rid = entry.get("record_id")
            if rid and rid in id_remap:
                entry["record_id"] = id_remap[rid]

    if is_root(user) and preserve_owner:
        scope_owner: str | None = None
        scope_namespace: str | None = None
    else:
        scope_owner = user.user_id
        scope_namespace = user.namespace
    allowlist = await _build_referenced_memory_allowlist(
        backend,
        tx,
        envelope,
        scope_owner=scope_owner,
        scope_namespace=scope_namespace,
    )
    for rid in rejected_persisted_ids:
        allowlist.pop(rid, None)

    if envelope.kg_triples:
        await _import_kg_triples(
            backend,
            tx,
            envelope.kg_triples,
            caller_user_id=user.user_id,
            caller_namespace=user.namespace,
            preserve_owner=preserve_owner,
            stats=stats,
            allowlist=allowlist,
            inserted_record_ids=inserted_record_ids,
        )
    if envelope.memory_versions:
        (
            authorized_version_ids,
            failed_version_record_ids,
            authorized_version_uuids,
        ) = await _import_memory_versions(
            backend,
            tx,
            envelope.memory_versions,
            caller_user_id=user.user_id,
            caller_namespace=user.namespace,
            preserve_owner=preserve_owner,
            stats=stats,
            allowlist=allowlist,
            inserted_record_ids=inserted_record_ids,
        )
        fatal_record_ids = inserted_record_ids & failed_version_record_ids
        if fatal_record_ids:
            sample = sorted(fatal_record_ids)[:5]
            extra = "..." if len(fatal_record_ids) > 5 else ""
            raise HTTPException(
                status_code=500,
                detail=(
                    "CHARON import: memory_versions sidecar had failed "
                    f"entries for {len(fatal_record_ids)} newly inserted "
                    f"record(s): {sample}{extra}. Authoritative history "
                    "is all-or-nothing per record under trigger "
                    "suppression - partial history would be inconsistent. "
                    "Transaction rolled back; fix the sidecar and retry."
                ),
            )

        if inserted_record_ids:
            await backend.memory_branches.delete_memory_branches_for_memories(
                tx,
                list(inserted_record_ids),
            )
        if authorized_version_ids:
            await _restore_memory_branches(
                backend,
                tx,
                list(authorized_version_ids),
                authorized_version_uuids=list(authorized_version_uuids),
            )
    if envelope.compression_manifest:
        await _import_compression_manifest(
            backend,
            tx,
            envelope.compression_manifest,
            caller_user_id=user.user_id,
            caller_namespace=user.namespace,
            preserve_owner=preserve_owner,
            stats=stats,
            allowlist=allowlist,
            inserted_record_ids=inserted_record_ids,
        )

    if envelope.memory_versions:
        if inserted_record_ids:
            covered = await backend.memories.fetch_versioned_memory_ids(tx, list(inserted_record_ids))
            covered_ids = {r["memory_id"] for r in covered}
            uncovered = inserted_record_ids - covered_ids
            if uncovered:
                sample = sorted(uncovered)[:5]
                extra = "..." if len(uncovered) > 5 else ""
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "CHARON import inserted "
                        f"{len(uncovered)} memory record(s) without "
                        "version history under trigger suppression: "
                        f"{sample}{extra}. Sidecar likely contained "
                        "malformed or rejected entries that did not "
                        "produce rows. Transaction rolled back."
                    ),
                )

        touched_ids = inserted_record_ids | authorized_version_ids
        if touched_ids:
            head_check = await backend.memories.fetch_memory_head_checks(tx, list(touched_ids))
            missing_inserted = []
            divergent = []
            in_db_inserted = inserted_record_ids
            for r in head_check:
                rid = r["id"]
                head_content = r["head_content"]
                memory_content = r["memory_content"]
                if head_content is None:
                    if rid in in_db_inserted:
                        missing_inserted.append(rid)
                elif memory_content != head_content:
                    divergent.append(rid)
            if missing_inserted:
                sample = sorted(missing_inserted)[:5]
                extra = "..." if len(missing_inserted) > 5 else ""
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "CHARON import inserted "
                        f"{len(missing_inserted)} memory record(s) with "
                        f"no main-branch HEAD: {sample}{extra}. The "
                        "envelope's memory_versions sidecar must include "
                        "a branch='main' entry for every kind:memory "
                        "record being imported. Transaction rolled back."
                    ),
                )
            if divergent:
                sample = sorted(divergent)[:5]
                extra = "..." if len(divergent) > 5 else ""
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "CHARON import: live memory content diverges "
                        "from restored memory_versions HEAD for "
                        f"{len(divergent)} record(s): {sample}{extra}. "
                        "The envelope's memory_versions sidecar must "
                        "include an entry whose content matches each "
                        "touched memory's content. Transaction rolled back."
                    ),
                )

    logger.info(
        "[MPF] import: user=%s imported=%d skipped=%d failed=%d unsupported=%s sidecars_imported=%s",
        user.user_id,
        stats.imported,
        stats.skipped,
        stats.failed,
        stats.unsupported_kinds,
        stats.sidecars_imported,
    )
    return stats


async def _write_mpf_import_audit_entry(
    backend: AuditPersistence,
    tx: Transaction,
    *,
    memory_id: str,
    content: str,
    category: str,
    subcategory: str | None,
    metadata: dict[str, Any] | None,
    writer_id: str,
) -> None:
    if getattr(backend, "audit_chain", None) is None:
        return
    from mnemos.audit import write_audit_entry
    from mnemos.core.config import get_settings
    from mnemos.workers.audit_sealer import audit_chain_enabled

    if not audit_chain_enabled():
        return
    session_secret = (getattr(get_settings().server, "session_secret", "") or "").encode("utf-8")
    if not session_secret:
        # An enabled audit chain with no configured session_secret would
        # write a tampered/unsignable entry — that's a configuration
        # bug, not an optional degradation. Refuse the import so the
        # operator notices and either fixes the secret or disables the
        # chain explicitly (the disabled path is taken above).
        raise RuntimeError(
            "MNEMOS_AUDIT_CHAIN is enabled but session_secret is empty; "
            "refusing to import without a signing secret (set "
            "MNEMOS_SESSION_SECRET or unset MNEMOS_AUDIT_CHAIN)."
        )
    # An audit write failure re-raises so the savepoint rolls back AND
    # propagates out of the outer import transaction — without this, memory
    # mutations would commit with permanent gaps in the configured
    # tamper-evident chain. (This was `conn.transaction()`, which was a
    # savepoint only because asyncpg makes a nested transaction one; it is now
    # an explicit savepoint that behaves identically on all six backends.)
    async with tx.savepoint():
        await write_audit_entry(
            backend,
            tx,
            op="create",
            memory_id_str=memory_id,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata=metadata,
            embedding=None,
            writer_id=writer_id,
            session_secret=session_secret,
            enforce_continuity=True,
        )
