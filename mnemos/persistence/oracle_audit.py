"""Oracle audit chain repository + journal methods mixin.

This module hosts two pieces of Oracle persistence that historically lived
alongside the rest of :mod:`mnemos.persistence.oracle` (a 9k-line file):

1. :class:`OracleAuditChainRepository` -- the v6.2 M-2.2.1 audit chain
   repository. Tables: ``memory_audit_chain`` + ``memory_audit_roots``
   (migrations 0029 + 0030; shipped at 614d483 for Oracle).

2. :class:`OracleAuditJournalMixin` -- a mixin holding the three journal
   methods (``create_journal_entry``, ``list_journal_entries``,
   ``delete_journal_entry``) previously defined directly on
   :class:`~mnemos.persistence.oracle.OracleBackend`. The methods are mixed
   into ``OracleBackend`` via inheritance so the public surface and MRO
   are unchanged from a caller's perspective.

This split is the v7 roadmap Feature 3/5 proof-of-pattern: mechanical
extraction, no behavior change. See ROADMAP.md.

The three journal methods reference OracleBackend's ``self`` via the mixin
so a class that inherits :class:`OracleAuditJournalMixin` gets the
methods exactly as they were defined on ``OracleBackend`` before the move.
"""

from __future__ import annotations

import json
from typing import Any

from mnemos.persistence.base import AuditChainRepository, Transaction
from mnemos.persistence.oracle import _call, _conn_from_tx, _fetch_all_dicts, _row_to_dict
from mnemos.persistence.types import Row


class OracleAuditChainRepository(AuditChainRepository):
    """Oracle impl of v6.2 M-2.2.1 audit chain.

    Tables: ``memory_audit_chain`` + ``memory_audit_roots``
    (migrations 0029 + 0030; shipped at 614d483 for Oracle).

    Bytes columns are RAW(16/32/64) — bind via plain ``bytes``
    through python-oracledb (driver coerces). Timestamps are
    TIMESTAMP WITH TIME ZONE — bind ISO 8601 strings via
    ``CAST(:ts AS TIMESTAMP WITH TIME ZONE)``.

    Concurrent sealer instances coexist via Oracle's
    ``FOR UPDATE SKIP LOCKED`` (11g+).
    """

    async def get_latest_audit_entry(
        self,
        tx: Transaction,
        memory_id: bytes,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT * FROM (
                    SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                           op, payload_hash, writer_id, writer_pubkey,
                           signature, signed_at, global_root, global_seq
                    FROM memory_audit_chain
                    WHERE memory_id = :memory_id
                    ORDER BY signed_at DESC
                ) WHERE ROWNUM = 1
                """,
                {"memory_id": memory_id},
            )
            row = await _call(cursor.fetchone)
            if row is None:
                return None
            cols = [d[0].lower() for d in cursor.description]
            return dict(zip(cols, row))
        finally:
            await _call(cursor.close)

    async def insert_audit_entry(
        self,
        tx: Transaction,
        *,
        entry_id: bytes,
        memory_id: bytes,
        prev_entry_id: bytes | None,
        prev_entry_hash: bytes | None,
        op: str,
        payload_hash: bytes,
        writer_id: str,
        writer_pubkey: bytes,
        signature: bytes,
        signed_at: Any,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO memory_audit_chain (
                    entry_id, memory_id, prev_entry_id, prev_entry_hash,
                    op, payload_hash, writer_id, writer_pubkey,
                    signature, signed_at
                )
                VALUES (
                    :entry_id, :memory_id, :prev_entry_id, :prev_entry_hash,
                    :op, :payload_hash, :writer_id, :writer_pubkey,
                    :signature, TO_TIMESTAMP_TZ(:signed_at, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM')
                )
                """,
                {
                    "entry_id": entry_id,
                    "memory_id": memory_id,
                    "prev_entry_id": prev_entry_id,
                    "prev_entry_hash": prev_entry_hash,
                    "op": op,
                    "payload_hash": payload_hash,
                    "writer_id": writer_id,
                    "writer_pubkey": writer_pubkey,
                    "signature": signature,
                    "signed_at": signed_at,
                },
            )
        finally:
            await _call(cursor.close)

    async def claim_unsealed_window(
        self,
        tx: Transaction,
        *,
        max_window_seconds: int,
        limit: int,
    ) -> list[Row]:
        """Claim oldest unsealed entries older than the cutoff using
        ``FOR UPDATE SKIP LOCKED``. Oracle's NUMTODSINTERVAL is the
        equivalent of PG's ``interval '<n> seconds'``.
        """
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT entry_id, signature, signed_at
                FROM memory_audit_chain
                WHERE global_root IS NULL
                  AND signed_at <= SYSTIMESTAMP - NUMTODSINTERVAL(:secs, 'SECOND')
                  AND ROWNUM <= :max_rows
                ORDER BY signed_at ASC, entry_id ASC
                FOR UPDATE SKIP LOCKED
                """,
                {"secs": int(max_window_seconds), "max_rows": int(limit)},
            )
            rows = await _call(cursor.fetchall)
            if not rows:
                return []
            cols = [d[0].lower() for d in cursor.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            await _call(cursor.close)

    async def stamp_window_with_root(
        self,
        tx: Transaction,
        *,
        entry_ids: list[bytes],
        global_root: bytes,
        starting_seq: int,
    ) -> None:
        if not entry_ids:
            return
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            # Oracle has no array-unnest; loop UPDATE preserving
            # caller-supplied seq order. Hot-path correctness > batch
            # microseconds here — sealer runs at 60s cadence.
            for offset, eid in enumerate(entry_ids):
                await _call(
                    cursor.execute,
                    """
                    UPDATE memory_audit_chain
                    SET global_root = :root, global_seq = :seq
                    WHERE entry_id = :eid
                    """,
                    {
                        "root": global_root,
                        "seq": starting_seq + offset,
                        "eid": eid,
                    },
                )
        finally:
            await _call(cursor.close)

    async def insert_audit_root(
        self,
        tx: Transaction,
        *,
        global_root: bytes,
        window_start: Any,
        window_end: Any,
        entry_count: int,
        root_signature: bytes,
        signer_pubkey: bytes,
        sealed_at: Any,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO memory_audit_roots (
                    global_root, window_start, window_end, entry_count,
                    root_signature, signer_pubkey, sealed_at
                )
                VALUES (
                    :global_root,
                    TO_TIMESTAMP_TZ(:window_start, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM'),
                    TO_TIMESTAMP_TZ(:window_end, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM'),
                    :entry_count,
                    :root_signature, :signer_pubkey,
                    TO_TIMESTAMP_TZ(:sealed_at, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM')
                )
                """,
                {
                    "global_root": global_root,
                    "window_start": window_start,
                    "window_end": window_end,
                    "entry_count": int(entry_count),
                    "root_signature": root_signature,
                    "signer_pubkey": signer_pubkey,
                    "sealed_at": sealed_at,
                },
            )
        finally:
            await _call(cursor.close)

    async def list_window_entries(
        self,
        tx: Transaction,
        global_root: bytes,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT entry_id, memory_id, signature, signed_at,
                       global_seq, payload_hash, op
                FROM memory_audit_chain
                WHERE global_root = :root
                ORDER BY signed_at ASC, entry_id ASC
                """,
                {"root": global_root},
            )
            rows = await _call(cursor.fetchall)
            if not rows:
                return []
            cols = [d[0].lower() for d in cursor.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            await _call(cursor.close)

    async def get_audit_entry_by_id(
        self,
        tx: Transaction,
        entry_id: bytes,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                       op, payload_hash, writer_id, writer_pubkey,
                       signature, signed_at, global_root, global_seq
                FROM memory_audit_chain
                WHERE entry_id = :eid
                """,
                {"eid": entry_id},
            )
            row = await _call(cursor.fetchone)
            if row is None:
                return None
            cols = [d[0].lower() for d in cursor.description]
            return dict(zip(cols, row))
        finally:
            await _call(cursor.close)

    async def get_chain_stats(self, tx: Transaction) -> dict:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT
                    COUNT(*),
                    COUNT(CASE WHEN global_root IS NULL THEN 1 END),
                    MIN(CASE WHEN global_root IS NULL THEN signed_at END)
                FROM memory_audit_chain
                """,
            )
            crow = await _call(cursor.fetchone)
            total = int(crow[0] or 0)
            unsealed = int(crow[1] or 0)
            oldest = crow[2]
            await _call(
                cursor.execute,
                """
                SELECT COUNT(*), MAX(sealed_at)
                FROM memory_audit_roots
                """,
            )
            rrow = await _call(cursor.fetchone)
            root_count = int(rrow[0] or 0)
            last_sealed = rrow[1]
        finally:
            await _call(cursor.close)
        return {
            "total_entries": total,
            "unsealed_count": unsealed,
            "oldest_unsealed_signed_at": (
                oldest.isoformat() if hasattr(oldest, "isoformat") else (str(oldest) if oldest else None)
            ),
            "sealed_root_count": root_count,
            "last_sealed_at": (
                last_sealed.isoformat()
                if hasattr(last_sealed, "isoformat")
                else (str(last_sealed) if last_sealed else None)
            ),
        }

    async def get_latest_audit_entries_batch(
        self,
        tx: Transaction,
        memory_ids: list[bytes],
    ) -> dict[bytes, Row]:
        """Oracle 12c+ ROW_NUMBER() OVER PARTITION BY. Each memory_id
        binds individually since python-oracledb doesn't natively
        accept a list-binding for RAW types in an IN clause without
        an array-type registration step.
        """
        if not memory_ids:
            return {}
        placeholders = ",".join(f":m{i}" for i in range(len(memory_ids)))
        params = {f"m{i}": mid for i, mid in enumerate(memory_ids)}
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                f"""
                SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                       op, payload_hash, writer_id, writer_pubkey,
                       signature, signed_at, global_root, global_seq
                FROM (
                  SELECT m.*,
                         ROW_NUMBER() OVER (
                           PARTITION BY memory_id
                           ORDER BY signed_at DESC, entry_id DESC
                         ) AS rn
                  FROM memory_audit_chain m
                  WHERE memory_id IN ({placeholders})
                )
                WHERE rn = 1
                """,
                params,
            )
            rows = await _call(cursor.fetchall)
            if not rows:
                return {}
            cols = [d[0].lower() for d in cursor.description]
            out: dict[bytes, Row] = {}
            for r in rows:
                d = dict(zip(cols, r))
                out[d["memory_id"]] = d
            return out
        finally:
            await _call(cursor.close)


class OracleAuditJournalMixin:
    """Mixin providing ``create_journal_entry`` / ``list_journal_entries``
    / ``delete_journal_entry`` for ``OracleBackend``.

    These three methods previously lived as instance methods directly on
    :class:`~mnemos.persistence.oracle.OracleBackend`. They are extracted
    here as a mixin so the oracle persistence module can shrink without
    changing the public surface: ``OracleBackend`` inherits this mixin and
    continues to expose the methods with identical behavior. The method
    bodies reference no instance state beyond what ``OracleBackend``
    already provides (the ``self`` argument is enough); helpers
    (``_call``, ``_conn_from_tx``, ``_row_to_dict``, ``_fetch_all_dicts``)
    come from :mod:`mnemos.persistence.oracle`.
    """

    async def create_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
        entry_date: Any | None,
        topic: str,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> Row:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            # entry_date NULL -> today (TRUNC(SYSDATE)), mirroring Postgres CURRENT_DATE.
            await _call(
                cursor.execute,
                """
                INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                VALUES (:id, :owner_id, :namespace,
                        NVL(CAST(:entry_date AS DATE), TRUNC(SYSDATE)),
                        :topic, :content, :metadata)
                """,
                {
                    "id": entry_id,
                    "owner_id": owner_id,
                    "namespace": namespace,
                    "entry_date": entry_date,
                    "topic": topic,
                    "content": content,
                    "metadata": json.dumps(metadata or {}),
                },
            )
            await _call(
                cursor.execute,
                """
                SELECT id, TO_CHAR(entry_date, 'YYYY-MM-DD') AS entry_date, topic, content,
                       metadata, TO_CHAR(created) AS created
                  FROM journal WHERE id = :id
                """,
                {"id": entry_id},
            )
            row = await _row_to_dict(cursor, await _call(cursor.fetchone))
            if row is None:
                raise RuntimeError("journal insert returned no row")
            return row
        finally:
            await _call(cursor.close)

    async def list_journal_entries(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        entry_date: Any | None,
        topic: str | None,
        search: str | None,
        limit: int,
    ) -> list[Row]:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            select = (
                "SELECT id, TO_CHAR(entry_date, 'YYYY-MM-DD') AS entry_date, topic, content, "
                "metadata, TO_CHAR(created) AS created FROM journal "
                "WHERE owner_id = :owner_id AND namespace = :namespace AND deleted_at IS NULL"
            )
            binds: dict[str, Any] = {"owner_id": owner_id, "namespace": namespace, "limit": limit}
            if entry_date is not None:
                select += " AND entry_date = CAST(:entry_date AS DATE)"
                binds["entry_date"] = entry_date
            elif topic:
                select += " AND topic = :topic"
                binds["topic"] = topic
            elif search:
                select += " AND (LOWER(content) LIKE LOWER(:search) OR LOWER(topic) LIKE LOWER(:search))"
                binds["search"] = f"%{search}%"
            select += " ORDER BY created DESC FETCH FIRST :limit ROWS ONLY"
            await _call(cursor.execute, select, binds)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def delete_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
    ) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "DELETE FROM journal WHERE id = :id AND owner_id = :owner_id "
                "AND namespace = :namespace AND deleted_at IS NULL",
                {"id": entry_id, "owner_id": owner_id, "namespace": namespace},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)


__all__ = [
    "OracleAuditChainRepository",
    "OracleAuditJournalMixin",
]
