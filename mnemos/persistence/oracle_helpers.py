"""Shared Oracle driver helpers; independent of repository import order."""

from __future__ import annotations

import inspect
from typing import Any


def _conn_from_tx(tx: Any) -> Any:
    """Resolve an oracledb connection from a backend-neutral tx handle."""
    if tx is None:
        return None
    return getattr(tx, "conn", tx)


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs) if callable(value) else value
    return await result if inspect.isawaitable(result) else result


async def _materialize_value(value: Any) -> Any:
    """Resolve oracledb async LOBs into plain strings/bytes.

    The python-oracledb async driver returns CLOB / BLOB columns as
    :class:`AsyncLOB` whose ``read()`` is a coroutine. The sync driver
    returns LOB objects whose ``read()`` is synchronous. This helper
    handles both shapes so callers always see a materialized value.
    """
    read = getattr(value, "read", None)
    if not callable(read):
        return value
    result = read()
    if inspect.isawaitable(result):
        return await result
    return result


async def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    names = [col[0].lower() for col in cursor.description]
    out: dict[str, Any] = {}
    for name, value in zip(names, row):
        out[name] = await _materialize_value(value)
    return out


async def _fetch_all_dicts(cursor: Any) -> list[dict[str, Any]]:
    rows = await _call(cursor.fetchall)
    out: list[dict[str, Any]] = []
    for raw in rows or []:
        d = await _row_to_dict(cursor, raw)
        if d is not None:
            out.append(d)
    return out
