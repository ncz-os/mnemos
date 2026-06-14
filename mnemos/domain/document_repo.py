"""Document-import persistence helpers.

The document import API owns parsing and response shaping; this module owns the
backend-specific chunk write so routes do not reach for raw driver pools.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from mnemos.core.persisted_text_classification import classify_persisted_text_fields
from mnemos.persistence.base import PersistenceBackend, Transaction


class DocumentChunkSoftDeletedConflictError(ValueError):
    """Raised when a chunk key matches a soft-deleted memory."""


@dataclass(frozen=True)
class ImportedDocumentChunk:
    memory_id: str
    emit_created_event: bool = True


class DocumentRepository:
    """Repository facade for document-import chunk writes."""

    async def import_chunk(
        self,
        backend: PersistenceBackend,
        tx: Transaction,
        *,
        memory_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        owner_id: str,
        namespace: str,
        permission_mode: int,
        chunk_key: str,
        legacy_chunk_key: str,
    ) -> ImportedDocumentChunk:
        """Insert or resolve one document-import chunk.

        Postgres preserves the historical ``import_chunk_key`` idempotency
        contract. Other backends currently fall back to the generic memory
        repository insert path, which keeps the route portable while backend
        native chunk-key columns catch up.
        """
        try:
            from mnemos.persistence.postgres import PostgresTransaction
        except ImportError:  # pragma: no cover - defensive for stripped builds.
            PostgresTransaction = None  # type: ignore[assignment]

        import json

        try:
            metadata_obj = json.loads(metadata_json) if metadata_json else {}
        except Exception:
            metadata_obj = {}
        classified = classify_persisted_text_fields(
            content=content,
            verbatim_content=content,
            metadata=metadata_obj,
            namespace=namespace,
            classified_at="document_import",
            memory_id=memory_id,
        )
        metadata_json = json.dumps(classified.metadata)
        namespace = classified.namespace

        if PostgresTransaction is not None and isinstance(tx, PostgresTransaction):
            return await self._import_postgres_chunk(
                tx.conn,
                memory_id=memory_id,
                content=content,
                category=category,
                subcategory=subcategory,
                metadata_json=metadata_json,
                owner_id=owner_id,
                namespace=namespace,
                permission_mode=permission_mode,
                chunk_key=chunk_key,
                legacy_chunk_key=legacy_chunk_key,
            )

        now = datetime.now(timezone.utc)
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata_json=metadata_json,
            quality_rating=75,
            owner_id=owner_id,
            namespace=namespace,
            permission_mode=permission_mode,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=content,
            created=now,
            updated=now,
        )
        return ImportedDocumentChunk(memory_id=memory_id)

    async def _import_postgres_chunk(
        self,
        conn: Any,
        *,
        memory_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        owner_id: str,
        namespace: str,
        permission_mode: int,
        chunk_key: str,
        legacy_chunk_key: str,
    ) -> ImportedDocumentChunk:
        legacy_id = None
        try:
            async with conn.transaction():
                legacy_id = await conn.fetchval(
                    "UPDATE memories "
                    "SET import_chunk_key = $1 "
                    "WHERE import_chunk_key = $2 "
                    "  AND owner_id = $3 "
                    "  AND namespace = $4 "
                    "  AND permission_mode = $5 "
                    "  AND category IS NOT DISTINCT FROM $6 "
                    "  AND subcategory IS NOT DISTINCT FROM $7 "
                    "  AND deleted_at IS NULL "
                    "RETURNING id",
                    chunk_key,
                    legacy_chunk_key,
                    owner_id,
                    namespace,
                    permission_mode,
                    category,
                    subcategory,
                )
        except asyncpg.UniqueViolationError as uv:
            constraint_name = getattr(uv, "constraint_name", None) or ""
            message = str(uv)
            is_chunk_key_uniq = (
                constraint_name == "memories_import_chunk_key_uniq" or "memories_import_chunk_key_uniq" in message
            )
            if not is_chunk_key_uniq:
                raise
            legacy_id = None

        if legacy_id is not None:
            return ImportedDocumentChunk(memory_id=str(legacy_id), emit_created_event=False)

        canonical_id = await conn.fetchval(
            "INSERT INTO memories "
            "(id, content, category, subcategory, metadata, quality_rating, "
            " verbatim_content, owner_id, namespace, permission_mode, "
            " import_chunk_key) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, $9, $10) "
            "ON CONFLICT (import_chunk_key) DO UPDATE "
            "  SET import_chunk_key = EXCLUDED.import_chunk_key "
            "  WHERE memories.deleted_at IS NULL "
            "RETURNING id",
            memory_id,
            content,
            category,
            subcategory,
            metadata_json,
            content,
            owner_id,
            namespace,
            permission_mode,
            chunk_key,
        )
        if canonical_id is None:
            raise DocumentChunkSoftDeletedConflictError(
                "Document chunk matches a soft-deleted memory; restore it before retrying this import"
            )
        return ImportedDocumentChunk(memory_id=str(canonical_id))
