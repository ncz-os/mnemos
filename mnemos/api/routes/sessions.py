"""Session management endpoints for stateful multi-turn conversations.

Sessions store conversation history server-side, accumulate memory context across turns,
and track compression/cost metrics per session. Integrates with gateway memory injection
and context routing.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import backend_or_503
from mnemos.core.security import scope_namespace
from mnemos.domain.openai_compat.router import search_memory_context as _search_mnemos_context
from mnemos.domain.openai_compat.providers import _route_to_provider
from mnemos.domain.models import (
    ChatMessage,
    SessionContext,
    SessionHistoryResponse,
    SessionMessage,
    SessionMessageResponse,
    SessionRequest,
    SessionResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse)
async def create_session(
    request: SessionRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create a new session for multi-turn conversation."""
    backend = backend_or_503()

    try:
        async with backend.transactional() as tx:
            row = await backend.sessions.create_session(
                tx,
                user_id=user.user_id,
                namespace=user.namespace,
                model=request.model or "gpt-4o",
                initial_context=request.initial_context,
            )

        logger.info(f"[SESSIONS] Created session {row['id']} for user {user.user_id}")

        return SessionResponse(
            session_id=row["id"],
            created_at=row["created_at"].isoformat()
            if hasattr(row["created_at"], "isoformat")
            else str(row["created_at"]),
            model=row["model"],
        )

    except Exception as e:
        logger.error(f"[SESSIONS] Failed to create session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Session creation failed: {str(e)}")


@router.get("/{session_id}", response_model=SessionContext)
async def get_session(
    session_id: str,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Get session context and metadata."""
    backend = backend_or_503()
    target_ns = scope_namespace(user, namespace)

    async with backend.transactional() as tx:
        session = await backend.sessions.get_session(tx, session_id, user.user_id, target_ns)
        injections = await backend.sessions.list_injected_memory_ids(tx, session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return SessionContext(
        session_id=session["id"],
        user_id=session["user_id"],
        created_at=session["created_at"].isoformat()
        if hasattr(session["created_at"], "isoformat")
        else str(session["created_at"]),
        last_activity=session["last_activity"].isoformat()
        if hasattr(session["last_activity"], "isoformat")
        else str(session["last_activity"]),
        message_count=session["message_count"],
        total_tokens=session["total_tokens"],
        model=session["model"],
        injected_memories=injections,
    )


@router.post("/{session_id}/messages", response_model=SessionMessageResponse)
async def add_session_message(
    session_id: str,
    request: SessionMessage,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Add message to session, search memory, inject context, call provider, return response.

    This is the main stateful chat endpoint. It:
    1. Stores user message in history
    2. Searches MNEMOS for relevant context
    3. Injects bounded memory snippets into system prompt
    4. Routes to provider with accumulated context
    5. Stores assistant response in history
    6. Updates session metrics
    """
    backend = backend_or_503()
    target_ns = scope_namespace(user, namespace)

    # Verify session ownership
    async with backend.transactional() as tx:
        session = await backend.sessions.get_session(tx, session_id, user.user_id, target_ns)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Store user message
    async with backend.transactional() as tx:
        message_id = await backend.sessions.add_message(
            tx,
            session_id=session_id,
            role=request.role or "user",
            content=request.content,
            model=request.model or session["model"],
        )

    # Get conversation history for the provider:
    #   1. The earliest role='system' row (the initial_context written
    #      at create_session time) — ALWAYS included, never evictable.
    #   2. Up to 4 most recent later system rows (subsequent policy
    #      updates posted via add_session_message). Bounded to prevent
    #      adversarial role='system' spam from blowing the prompt
    #      budget.
    #   3. The 10 most recent non-system messages, chronological.
    #
    # Iteration history under Codex review:
    #   - ASC LIMIT 10  → returned 10 OLDEST messages (wrong dir).
    #   - DESC LIMIT 10 → recent works, but loses initial_context.
    #   - LIMIT 1 pinned earliest → drops later system updates.
    #   - No LIMIT pinned → unbounded pinned context.
    #   - LIMIT 5 pinned (most recent) → 5 later system writes
    #     evict the foundational initial_context (adversarial path).
    #   - This shape: earliest pinned + 4 most recent later +
    #     10 recent non-system. Initial context never evictable;
    #     later updates capped; bounded total surface.
    #
    # Token-aware truncation and privilege-gating role='system' on
    # add_session_message remain the structurally correct redesign;
    # tracked separately, out of scope here.
    # Ordering key is (timestamp, id) — pure timestamp is not unique
    # in session_messages, so an exclusion that uses `timestamp >`
    # alone could either double-count the initial row or skip it on
    # a tie. Using the row id as a tie-breaker is deterministic.
    async with backend.transactional() as tx:
        history = await backend.sessions.fetch_provider_history(tx, session_id)

    # Search MNEMOS for context
    memories_injected = 0
    mnemos_context = ""

    try:
        mnemos_docs = await _search_mnemos_context(request.content, user, limit=3)

        if mnemos_docs:
            # Store injection record for each memory.
            memory_ids = [doc.get("id", f"doc_{i}") for i, doc in enumerate(mnemos_docs)]
            async with backend.transactional() as tx:
                await backend.sessions.add_memory_injections(
                    tx,
                    session_id=session_id,
                    message_id=message_id,
                    memory_ids=memory_ids,
                )

            mnemos_context = "\n\n".join([f"[Memory]\n{doc['content'][:500]}" for doc in mnemos_docs])
            memories_injected = len(mnemos_docs)
            [doc.get("id") for doc in mnemos_docs]

            logger.info(f"[SESSIONS] Injected {memories_injected} memories into session {session_id}")

    except Exception as e:
        logger.warning(f"[SESSIONS] Memory search failed: {e}, continuing without context")

    # Build messages for provider (include session history + injected context)
    messages = [{"role": msg["role"], "content": msg["content"]} for msg in history]

    # Add system prompt with MNEMOS context if available
    system_prompt = ""
    has_system = any(m["role"] == "system" for m in messages)

    if mnemos_context:
        system_prompt = f"[MNEMOS Context - {memories_injected} memories]\n{mnemos_context}"
        if has_system:
            # Append to existing system prompt
            messages[0]["content"] += f"\n\n{system_prompt}"
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})

    # Route to provider
    model = request.model or session["model"]
    response_text = ""
    tokens_used = 0

    try:
        response_text = await _route_to_provider(
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=None,
            user=user,
        )

        # Estimate tokens (rough approximation: ~4 chars per token)
        tokens_used = len(response_text) // 4

    except Exception as e:
        logger.error(f"[SESSIONS] Provider routing failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"Provider unavailable: {str(e)}")

    # Store assistant response
    async with backend.transactional() as tx:
        assistant_message_id = await backend.sessions.add_message(
            tx,
            session_id=session_id,
            role="assistant",
            content=response_text,
            model=model,
            tokens_used=tokens_used,
            memories_injected=memories_injected,
        )
        await backend.sessions.update_metrics(
            tx,
            session_id=session_id,
            user_id=user.user_id,
            namespace=target_ns,
            tokens_used=tokens_used,
        )

    logger.info(
        f"[SESSIONS] Added message to session {session_id}: "
        f"user→assistant, {tokens_used} tokens, {memories_injected} memories"
    )

    return SessionMessageResponse(
        session_id=session_id,
        message_id=assistant_message_id,
        role="assistant",
        content=response_text,
        model=model,
        timestamp=datetime.now(timezone.utc).isoformat(),
        tokens_used=tokens_used,
        memories_injected=memories_injected,
    )


@router.get("/{session_id}/history", response_model=SessionHistoryResponse)
async def get_session_history(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Get conversation history for session."""
    backend = backend_or_503()
    target_ns = scope_namespace(user, namespace)

    async with backend.transactional() as tx:
        session = await backend.sessions.get_session(tx, session_id, user.user_id, target_ns)
        if session:
            messages, total = await backend.sessions.fetch_history(tx, session_id, limit, offset)
        else:
            messages, total = [], 0

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return SessionHistoryResponse(
        session_id=session_id,
        messages=[
            ChatMessage(
                role=m["role"],
                content=m["content"],
                timestamp=m["timestamp"].isoformat()
                if hasattr(m["timestamp"], "isoformat")
                else (str(m["timestamp"]) if m["timestamp"] else None),
                model=m["model"],
            )
            for m in messages
        ],
        total_messages=total,
        total_tokens=session["total_tokens"],
        created_at=session["created_at"].isoformat()
        if hasattr(session["created_at"], "isoformat")
        else str(session["created_at"]),
    )


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Close and delete session."""
    backend = backend_or_503()
    target_ns = scope_namespace(user, namespace)

    async with backend.transactional() as tx:
        session = await backend.sessions.get_session(tx, session_id, user.user_id, target_ns)
        if session:
            await backend.sessions.delete_session(tx, session_id, user.user_id, target_ns)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    logger.info(f"[SESSIONS] Deleted session {session_id}")

    return {"status": "deleted", "session_id": session_id}
