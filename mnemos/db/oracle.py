"""Oracle repository helpers for the initial MNEMOS Oracle port."""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from typing import Any


_MEMORY_COLUMNS = (
    "id",
    "content",
    "category",
    "subcategory",
    "metadata",
    "quality_rating",
    "compressed_content",
    "verbatim_content",
    "owner_id",
    "namespace",
    "created_at",
    "updated_at",
    "external_id",
)
_MEMORY_SELECT = ", ".join(_MEMORY_COLUMNS)


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs) if callable(value) else value
    return await result if inspect.isawaitable(result) else result


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _json_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, default=str, separators=(",", ":"))


def _current_timestamp() -> str:
    """Return the SQL fragment for current timestamp. Override in Db2 subclass."""
    return "SYSTIMESTAMP"


def _bind_style() -> str:
    """Return bind style hint. 'named' for Oracle, 'positional' for Db2."""
    return "named"


def _convert_binds(sql: str, style: str = "named") -> str:
    """Convert :name binds to positional if needed (Db2 override)."""
    if style == "positional":
        # Simple conversion for common cases
        import re

        return re.sub(r":\w+", "?", sql)
    return sql


def _read_lob(value: Any) -> Any:
    read = getattr(value, "read", None)
    return read() if callable(read) else value


def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    names = [col[0].lower() for col in cursor.description]
    data = {name: _read_lob(value) for name, value in zip(names, row)}
    data["created"] = data.get("created_at")
    data["updated"] = data.get("updated_at")
    return data


class OracleMemoryRepository:
    """Surgical Oracle repository matching Postgres parity needs."""

    def __init__(self, connectable: Any):
        self._connectable = connectable

    @asynccontextmanager
    async def _connection(self):
        acquire = getattr(self._connectable, "acquire", None)
        if callable(acquire):
            async with acquire() as conn:
                yield conn
            return
        yield self._connectable

    async def _cursor(self, conn: Any) -> Any:
        return await _call(conn.cursor)

    async def _commit(self, conn: Any) -> None:
        await _call(conn.commit)

    async def insert_memory(
        self,
        tx: Any = None,
        *,
        memory_id: str,
        content: str,
        category: str = "imported",
        subcategory: str | None = None,
        metadata_json: str = "{}",
        quality_rating: int = 75,
        owner_id: str = "default",
        namespace: str = "default",
        permission_mode: int = 600,
        source_model: str | None = None,
        source_provider: str | None = None,
        source_session: str | None = None,
        source_agent: str | None = None,
        verbatim_content: str | None = None,
        created: Any = None,
        updated: Any = None,
        commit: bool = False,
        **_: Any,
    ) -> str:
        conn_cm = self._connection() if tx is None else asynccontextmanager(lambda: (yield tx))()
        async with conn_cm as conn:
            cursor = await self._cursor(conn)
            try:
                await _call(
                    cursor.execute,
                    """
                    INSERT INTO memories (
                        id, content, category, subcategory, metadata,
                        quality_rating, verbatim_content, owner_id, namespace,
                        permission_mode, source_model, source_provider,
                        source_session, source_agent, created, updated
                    )
                    SELECT :id, :content, :category, :subcategory, :metadata,
                           :quality_rating, :verbatim_content, :owner_id, :namespace,
                           :permission_mode, :source_model, :source_provider,
                           :source_session, :source_agent,
                           COALESCE(:created, :ts),
                           COALESCE(:updated, :ts)
                    FROM dual
                    WHERE NOT EXISTS (SELECT 1 FROM memories WHERE id = :id)
                """,
                    locals() | {"metadata": metadata_json},
                )
                if commit:
                    await self._commit(conn)
                return "INSERT 0 1" if int(getattr(cursor, "rowcount", 0) or 0) else "INSERT 0 0"
            finally:
                await _call(cursor.close)

    async def fetch_memory_by_id(self, tx: Any, memory_id: str) -> dict[str, Any] | None:
        async with asynccontextmanager(lambda: (yield tx))() as conn:
            cursor = await self._cursor(conn)
            try:
                await _call(
                    cursor.execute,
                    f"""
                    SELECT {_MEMORY_SELECT}, created, updated, permission_mode,
                           source_model, source_provider, source_session, source_agent
                    FROM memories
                    WHERE id = :id AND deleted_at IS NULL
                """,
                    {"id": memory_id},
                )
                return _row_to_dict(cursor, await _call(cursor.fetchone))
            finally:
                await _call(cursor.close)


__all__ = ["OracleMemoryRepository"]
