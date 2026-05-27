"""mnemos/ledger — knemon invocation audit log (writes to Postgres)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def record(model: str, task: str, result: str | None) -> None:
    """Persist a knemon invocation record to the ``memories`` table.

    Category is ``knemon_invocation``.  This function wraps the async
    write internally and **never raises** — errors are logged silently.
    """
    try:
        asyncio.run(_record_async(model, task, result))
    except Exception:
        logger.warning("[knemon] ledger record failed", exc_info=True)


async def _record_async(model: str, task: str, result: str | None) -> None:
    from mnemos.core import lifecycle as _lc

    pool = _lc._pool
    if pool is None:
        logger.warning("[knemon] ledger skipped: no database pool available")
        return

    now = datetime.now(timezone.utc)
    memory_id = str(uuid.uuid4())
    metadata = json.dumps({
        "model": model,
        "task": task,
        "result": result,
        "recorded_at": now.isoformat(),
    })

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (
                id, content, category, metadata, quality_rating,
                owner_id, namespace, permission_mode,
                source_model, source_agent, created, updated
            )
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            memory_id,
            task,
            "knemon_invocation",
            metadata,
            0,
            "knemon",
            "default",
            644,
            model,
            "knemon",
            now,
            now,
        )
