"""Session ingestion endpoint."""
import json
import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from api.models import SessionIngestRequest, SessionIngestResponse

logger = logging.getLogger(__name__)
router = APIRouter()


def _extract_readable(items: list, max_items: int = 20) -> str:
    """Extract human-readable text from session items.

    Handles common message formats ({role, content}, {type, text}, plain strings).
    Caps at max_items to prevent unbounded memory growth.
    Never calls str() on arbitrary objects — only extracts validated string fields.
    """
    parts = []
    for item in items[:max_items]:
        if isinstance(item, dict):
            content = item.get("content") or item.get("text") or item.get("body") or ""
            if not isinstance(content, str):
                continue
            role = item.get("role") or item.get("type") or ""
            parts.append(f"[{role}] {content[:500]}" if role else content[:500])
        elif isinstance(item, str):
            parts.append(item[:500])
    return "\n".join(parts) if parts else "(no readable content)"




@router.post("/ingest/session", response_model=SessionIngestResponse)
async def ingest_session(request: SessionIngestRequest, user: UserContext = Depends(get_current_user)):
    """Ingest Claude Code session data into MNEMOS."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    stored_ids = []
    try:
        data = request.raw_data
        async with _lc._pool.acquire() as conn:
            if data.get("messages") or data.get("prompts"):
                items = data.get("messages", []) or data.get("prompts", [])
                if items:
                    content = f"Session {request.session_id} — {len(items)} messages\n{_extract_readable(items)}"
                    mem_id = f"mem_{uuid.uuid4().hex[:12]}"
                    meta = json.dumps({
                        "source": request.source, "session_id": request.session_id,
                        "machine_id": request.machine_id, "agent_id": request.agent_id,
                        "git_commit": request.git_commit, "item_count": len(items),
                        "item_type": "messages",
                    })
                    await conn.execute(
                        "INSERT INTO memories (id, content, category, metadata, quality_rating) "
                        "VALUES ($1, $2, $3, $4::jsonb, 75)",
                        mem_id, content, "session_activity", meta,
                    )
                    stored_ids.append(mem_id)

            if data.get("code_blocks"):
                items = data.get("code_blocks", [])
                if items:
                    content = f"Session {request.session_id} — {len(items)} code blocks\n{_extract_readable(items)}"
                    mem_id = f"mem_{uuid.uuid4().hex[:12]}"
                    meta = json.dumps({
                        "source": request.source, "session_id": request.session_id,
                        "machine_id": request.machine_id, "agent_id": request.agent_id,
                        "git_commit": request.git_commit, "item_count": len(items),
                        "item_type": "code",
                    })
                    await conn.execute(
                        "INSERT INTO memories (id, content, category, metadata, quality_rating) "
                        "VALUES ($1, $2, $3, $4::jsonb, 75)",
                        mem_id, content, "session_code", meta,
                    )
                    stored_ids.append(mem_id)

            if data.get("tool_operations") or data.get("tools"):
                items = data.get("tool_operations", []) or data.get("tools", [])
                if items:
                    content = f"Session {request.session_id} — {len(items)} tool operations\n{_extract_readable(items)}"
                    mem_id = f"mem_{uuid.uuid4().hex[:12]}"
                    meta = json.dumps({
                        "source": request.source, "session_id": request.session_id,
                        "machine_id": request.machine_id, "agent_id": request.agent_id,
                        "git_commit": request.git_commit, "item_count": len(items),
                        "item_type": "tools",
                    })
                    await conn.execute(
                        "INSERT INTO memories (id, content, category, metadata, quality_rating) "
                        "VALUES ($1, $2, $3, $4::jsonb, 75)",
                        mem_id, content, "session_tools", meta,
                    )
                    stored_ids.append(mem_id)

        if _lc._cache:
            try:
                await _lc._cache.delete("stats:global")
            except Exception:
                pass

        logger.info(f"Session {request.session_id} ingested: {len(stored_ids)} records")
        return SessionIngestResponse(
            success=True, session_id=request.session_id,
            stored_count=len(stored_ids), memory_ids=stored_ids,
        )
    except asyncpg.PostgresError as e:
        logger.error(f"Session ingestion DB error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    except Exception as e:
        logger.error(f"Session ingestion failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
