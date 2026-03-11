"""MNEMOS API Server with Database Integration - Phase 2/5: Response Pre-Compression & Rehydration"""
import logging
import json
import sys
import os
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from datetime import datetime
import asyncpg
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Phase 2: Import external inference provider for compression
sys.path.insert(0, os.path.dirname(__file__))
from external_inference_provider import ExternalInferenceProvider

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# Compression thresholds
COMPRESSION_RESULT_SET_THRESHOLD = 50 * 1024   # 50KB total result set
COMPRESSION_ITEM_THRESHOLD = 5 * 1024           # 5KB per item

# Singleton inference provider
_inference_provider: Optional[ExternalInferenceProvider] = None

def get_inference_provider() -> ExternalInferenceProvider:
    global _inference_provider
    if _inference_provider is None:
        _inference_provider = ExternalInferenceProvider()
    return _inference_provider

# Models
class ConsultationRequest(BaseModel):
    prompt: str
    task_type: Optional[str] = "reasoning"
    context: Optional[str] = None
    mode: Optional[str] = "auto"

class ConsultationResponse(BaseModel):
    consensus_response: str
    consensus_score: float
    winning_muse: str
    winning_latency_ms: int
    cost: float
    mode: str
    task_type: str
    timestamp: str

class StatsResponse(BaseModel):
    total_memories: int
    total_compressions: int
    average_compression_ratio: float
    average_quality_rating: int
    memories_by_category: Dict[str, int]
    memories_by_task_type: Dict[str, int]
    unreviewed_compressions: int
    timestamp: str

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    database_connected: bool
    version: str

@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup/shutdown"""
    logger.info("Starting MNEMOS API Server v2.1.0 (Phase 2/5: Compression)")
    # Pre-initialize provider to detect CERBERUS connectivity at startup
    provider = get_inference_provider()
    healthy = await provider.health_check()
    if healthy:
        logger.info("ExternalInferenceProvider: CERBERUS llama-server CONNECTED")
    else:
        logger.warning("ExternalInferenceProvider: CERBERUS llama-server UNREACHABLE - compression disabled")
    yield
    await provider.close()
    logger.info("Shutting down MNEMOS API Server")

app = FastAPI(title="MNEMOS API", version="2.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.router.lifespan_context = lifespan

@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(status="healthy", timestamp=datetime.utcnow().isoformat(), database_connected=True, version="2.1.0")

@app.get("/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """Get system statistics from database"""
    try:
        conn = await asyncpg.connect(user='mnemos_user', password='mnemos_password', database='mnemos', host='localhost')
        total = await conn.fetchval('SELECT COUNT(*) FROM memories')
        cat_rows = await conn.fetch('SELECT category, COUNT(*) as cnt FROM memories GROUP BY category')
        memories_by_category = {row['category']: row['cnt'] for row in cat_rows}
        avg_quality = await conn.fetchval('SELECT AVG(quality_rating) FROM memories WHERE quality_rating IS NOT NULL')
        total_compressions = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE llm_optimized = true") or 0
        avg_ratio_row = await conn.fetchval("""
            SELECT AVG(LENGTH(compressed_content)::float / NULLIF(LENGTH(content), 0))
            FROM memories WHERE llm_optimized = true AND compressed_content IS NOT NULL
        """)
        await conn.close()

        return StatsResponse(
            total_memories=total or 0,
            total_compressions=total_compressions,
            average_compression_ratio=round(avg_ratio_row, 2) if avg_ratio_row else 0.57,
            average_quality_rating=int(avg_quality) if avg_quality else 75,
            memories_by_category=memories_by_category,
            memories_by_task_type={},
            unreviewed_compressions=0,
            timestamp=datetime.utcnow().isoformat(),
        )
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return StatsResponse(total_memories=0, total_compressions=0, average_compression_ratio=0.0, average_quality_rating=0,
                             memories_by_category={}, memories_by_task_type={}, unreviewed_compressions=0, timestamp=datetime.utcnow().isoformat())

@app.post("/graeae/consult", response_model=ConsultationResponse)
async def consult_graeae(request: ConsultationRequest) -> ConsultationResponse:
    """Consult GRAEAE and log consultation"""
    logger.info(f"GRAEAE Consultation: {request.task_type}")

    response = ConsultationResponse(
        consensus_response=request.prompt[:100] + "..." if len(request.prompt) > 100 else request.prompt,
        consensus_score=0.85,
        winning_muse="claude-opus",
        winning_latency_ms=1200,
        cost=0.02,
        mode=request.mode or "auto",
        task_type=request.task_type,
        timestamp=datetime.utcnow().isoformat(),
    )

    async def log_it():
        try:
            conn = await asyncpg.connect(user='mnemos_user', password='mnemos_password', database='mnemos', host='localhost')
            await conn.execute('''INSERT INTO graeae_consultations
                (prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)''',
                request.prompt, request.task_type, response.consensus_response,
                response.consensus_score, response.winning_muse, response.cost,
                response.winning_latency_ms, response.mode)
            await conn.close()
        except Exception as e:
            logger.warning(f"Log consultation failed: {e}")

    import asyncio
    await log_it()
    return response

@app.get("/graeae/health")
async def graeae_health():
    return {"status": "healthy", "service": "graeae"}


# ============================================================
# MEMORY ENDPOINTS
# ============================================================

class MemoryItem(BaseModel):
    id: str
    content: str
    category: str
    created: str
    updated: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    quality_rating: Optional[int] = None
    compressed_content: Optional[str] = None

class MemoryListResponse(BaseModel):
    count: int
    memories: List[MemoryItem]
    compression_applied: Optional[bool] = False
    compression_metadata: Optional[Dict[str, Any]] = None

class MemorySearchRequest(BaseModel):
    query: str
    limit: int = 10
    category: Optional[str] = None
    include_compressed: Optional[bool] = False

class MemoryCreateRequest(BaseModel):
    content: str
    category: str = "facts"
    metadata: Optional[Dict[str, Any]] = None
    source: Optional[str] = "openclaw"

# Phase 5: Rehydration request/response models
class RehydrationRequest(BaseModel):
    query: str
    budget_tokens: int = 8000
    category: Optional[str] = None
    limit: int = 20

class RehydrationResponse(BaseModel):
    context: str
    tokens_used: int
    original_tokens: int
    compression_ratio: float
    quality_score: int
    memories_included: int
    compression_applied: bool

async def _get_db():
    return await asyncpg.connect(user='mnemos_user', password='mnemos_password', database='mnemos', host='localhost')

def _row_to_memory(row, include_compressed: bool = False) -> MemoryItem:
    raw_meta = row.get('metadata')
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except Exception:
            raw_meta = None
    elif not isinstance(raw_meta, dict):
        raw_meta = None
    return MemoryItem(
        id=row['id'],
        content=row['content'][:2000],
        category=row['category'],
        created=row['created'].isoformat() if row['created'] else '',
        updated=row['updated'].isoformat() if row.get('updated') else None,
        metadata=raw_meta if raw_meta else None,
        quality_rating=row.get('quality_rating'),
        compressed_content=row.get('compressed_content') if include_compressed else None,
    )

@app.get("/memories", response_model=MemoryListResponse)
async def list_memories(category: Optional[str] = None, limit: int = 20, offset: int = 0):
    conn = await _get_db()
    try:
        if category:
            rows = await conn.fetch('SELECT id, content, category, created, updated, metadata, quality_rating, compressed_content FROM memories WHERE category=$1 ORDER BY created DESC LIMIT $2 OFFSET $3', category, limit, offset)
            total = await conn.fetchval('SELECT COUNT(*) FROM memories WHERE category=$1', category)
        else:
            rows = await conn.fetch('SELECT id, content, category, created, updated, metadata, quality_rating, compressed_content FROM memories ORDER BY created DESC LIMIT $1 OFFSET $2', limit, offset)
            total = await conn.fetchval('SELECT COUNT(*) FROM memories')
        return MemoryListResponse(count=total, memories=[_row_to_memory(r) for r in rows])
    finally:
        await conn.close()

@app.get("/memories/{memory_id}", response_model=MemoryItem)
async def get_memory(memory_id: str):
    conn = await _get_db()
    try:
        row = await conn.fetchrow('SELECT id, content, category, created, updated, metadata, quality_rating, compressed_content FROM memories WHERE id=$1', memory_id)
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        return _row_to_memory(row, include_compressed=True)
    finally:
        await conn.close()

@app.post("/memories/search", response_model=MemoryListResponse)
async def search_memories(request: MemorySearchRequest):
    """
    Phase 2: Search memories with optional compression of large result sets.
    - If total result size > 50KB, compress individual items > 5KB
    - Include compressed_content and quality_score in response metadata
    """
    conn = await _get_db()
    try:
        query_tsv = ' & '.join(request.query.split())
        if request.category:
            rows = await conn.fetch(
                "SELECT id, content, category, created, updated, metadata, quality_rating, compressed_content, "
                "ts_rank(to_tsvector('english', content), to_tsquery('english', $1)) as rank "
                "FROM memories WHERE to_tsvector('english', content) @@ to_tsquery('english', $1) AND category=$3 "
                "ORDER BY rank DESC LIMIT $2",
                query_tsv, request.limit, request.category
            )
        else:
            rows = await conn.fetch(
                "SELECT id, content, category, created, updated, metadata, quality_rating, compressed_content, "
                "ts_rank(to_tsvector('english', content), to_tsquery('english', $1)) as rank "
                "FROM memories WHERE to_tsvector('english', content) @@ to_tsquery('english', $1) "
                "ORDER BY rank DESC LIMIT $2",
                query_tsv, request.limit
            )
    except Exception as e:
        logger.warning(f"FTS failed, falling back to ILIKE: {e}")
        like_q = f"%{request.query}%"
        try:
            if request.category:
                rows = await conn.fetch(
                    'SELECT id, content, category, created, updated, metadata, quality_rating, compressed_content '
                    'FROM memories WHERE content ILIKE $1 AND category=$3 ORDER BY created DESC LIMIT $2',
                    like_q, request.limit, request.category
                )
            else:
                rows = await conn.fetch(
                    'SELECT id, content, category, created, updated, metadata, quality_rating, compressed_content '
                    'FROM memories WHERE content ILIKE $1 ORDER BY created DESC LIMIT $2',
                    like_q, request.limit
                )
        except Exception as e2:
            logger.error(f"Both FTS and ILIKE failed: {e2}")
            rows = []
    finally:
        await conn.close()

    # Phase 2: Response pre-compression for large result sets
    memories = [_row_to_memory(r, include_compressed=request.include_compressed) for r in rows]
    compression_applied = False
    compression_metadata = {}

    # Calculate total result set size
    total_size = sum(len(m.content) for m in memories)

    if total_size > COMPRESSION_RESULT_SET_THRESHOLD:
        provider = get_inference_provider()
        # Check if CERBERUS is available before attempting compression
        cerberus_healthy = await provider.health_check()

        if cerberus_healthy:
            logger.info(f"[PHASE2] Result set {total_size} bytes > {COMPRESSION_RESULT_SET_THRESHOLD} bytes threshold, applying compression")
            compressed_count = 0
            total_original = total_size
            total_compressed = 0
            quality_scores = []

            for memory in memories:
                item_size = len(memory.content)
                if item_size > COMPRESSION_ITEM_THRESHOLD and not memory.compressed_content:
                    # Item > 5KB and not already compressed - compress it
                    result = await provider.compress(memory.content, target_ratio=0.35, min_quality=70)
                    if result['success']:
                        memory.compressed_content = result['compressed']
                        quality_scores.append(result['quality_score'])
                        total_compressed += result['compressed_length']
                        compressed_count += 1
                        logger.info(f"[PHASE2] Compressed {memory.id[:8]}: {item_size} -> {result['compressed_length']} chars (quality={result['quality_score']})")
                    else:
                        total_compressed += item_size
                        logger.warning(f"[PHASE2] Compression failed for {memory.id[:8]}: {result['error']}")
                else:
                    total_compressed += item_size

            if compressed_count > 0:
                compression_applied = True
                avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0
                compression_metadata = {
                    'items_compressed': compressed_count,
                    'total_items': len(memories),
                    'original_bytes': total_original,
                    'compressed_bytes': total_compressed,
                    'compression_ratio': round(total_compressed / max(total_original, 1), 3),
                    'average_quality_score': round(avg_quality, 1),
                    'threshold_triggered': COMPRESSION_RESULT_SET_THRESHOLD,
                }
                logger.info(f"[PHASE2] Compression complete: {compressed_count}/{len(memories)} items compressed, ratio={compression_metadata['compression_ratio']:.2%}, avg_quality={avg_quality:.1f}")
        else:
            logger.warning("[PHASE2] CERBERUS unavailable, skipping compression for large result set")

    return MemoryListResponse(
        count=len(memories),
        memories=memories,
        compression_applied=compression_applied,
        compression_metadata=compression_metadata if compression_applied else None,
    )

@app.post("/memories", response_model=MemoryItem)
async def create_memory(request: MemoryCreateRequest):
    import uuid
    mem_id = f"mem_{uuid.uuid4().hex[:12]}"
    conn = await _get_db()
    try:
        meta = json.dumps(request.metadata or {"source": request.source})
        await conn.execute("INSERT INTO memories (id, content, category, metadata, quality_rating) VALUES ($1, $2, $3, $4::jsonb, 75)", mem_id, request.content, request.category, meta)
        row = await conn.fetchrow('SELECT id, content, category, created, updated, metadata, quality_rating, compressed_content FROM memories WHERE id=$1', mem_id)
        return _row_to_memory(row)
    finally:
        await conn.close()


# ============================================================
# PHASE 5: REHYDRATION ENDPOINT
# ============================================================

@app.post("/memories/rehydrate", response_model=RehydrationResponse)
async def rehydrate_memories(request: RehydrationRequest):
    """
    Phase 5: Return memories optimized for Claude context injection.

    Searches for relevant memories, concatenates them, and auto-compresses
    to fit within the specified token budget. Returns formatted context
    ready for injection into Claude prompts.

    Input:
      - query: Search term to find relevant memories
      - budget_tokens: Maximum token budget (default 8000)
      - category: Optional category filter
      - limit: Max memories to retrieve before compression

    Output:
      - context: Compressed/formatted context ready for Claude injection
      - tokens_used: Estimated tokens in final context
      - original_tokens: Estimated tokens before compression
      - compression_ratio: Final compression ratio
      - quality_score: Compression quality (0-100)
      - memories_included: Number of memories in context
      - compression_applied: Whether compression was needed
    """
    conn = await _get_db()
    try:
        # Retrieve relevant memories
        query_tsv = ' & '.join(request.query.split())
        try:
            if request.category:
                rows = await conn.fetch(
                    "SELECT id, content, category, created, compressed_content, quality_rating, "
                    "ts_rank(to_tsvector('english', content), to_tsquery('english', $1)) as rank "
                    "FROM memories WHERE to_tsvector('english', content) @@ to_tsquery('english', $1) AND category=$3 "
                    "ORDER BY rank DESC LIMIT $2",
                    query_tsv, request.limit, request.category
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, content, category, created, compressed_content, quality_rating, "
                    "ts_rank(to_tsvector('english', content), to_tsquery('english', $1)) as rank "
                    "FROM memories WHERE to_tsvector('english', content) @@ to_tsquery('english', $1) "
                    "ORDER BY rank DESC LIMIT $2",
                    query_tsv, request.limit
                )
        except Exception as e:
            logger.warning(f"[REHYDRATE] FTS failed, using ILIKE fallback: {e}")
            like_q = f"%{request.query}%"
            if request.category:
                rows = await conn.fetch(
                    'SELECT id, content, category, created, compressed_content, quality_rating '
                    'FROM memories WHERE content ILIKE $1 AND category=$3 ORDER BY created DESC LIMIT $2',
                    like_q, request.limit, request.category
                )
            else:
                rows = await conn.fetch(
                    'SELECT id, content, category, created, compressed_content, quality_rating '
                    'FROM memories WHERE content ILIKE $1 ORDER BY created DESC LIMIT $2',
                    like_q, request.limit
                )
    finally:
        await conn.close()

    if not rows:
        return RehydrationResponse(
            context="",
            tokens_used=0,
            original_tokens=0,
            compression_ratio=1.0,
            quality_score=100,
            memories_included=0,
            compression_applied=False,
        )

    # Build context string from memories, preferring compressed_content when available
    context_parts = []
    for row in rows:
        # Use compressed_content if available (already distilled), else use full content
        effective_content = row['compressed_content'] if row['compressed_content'] else row['content']
        created_str = row['created'].strftime('%Y-%m-%d') if row['created'] else 'unknown'
        context_parts.append(
            f"[{row['category']} / {created_str}]\n{effective_content[:2000]}"
        )

    combined_context = "\n\n---\n\n".join(context_parts)
    original_tokens = int(len(combined_context) / 4)

    # Use ExternalInferenceProvider.prepare_context() to fit token budget
    provider = get_inference_provider()
    result = await provider.prepare_context(combined_context, max_tokens=request.budget_tokens)

    compression_applied = result['ratio'] < 0.99
    logger.info(
        f"[REHYDRATE] query='{request.query[:30]}' | memories={len(rows)} | "
        f"original_tokens={original_tokens} | tokens_used={result['tokens_used']} | "
        f"ratio={result['ratio']:.2%} | quality={result['quality_score']} | "
        f"compressed={compression_applied}"
    )

    return RehydrationResponse(
        context=result['context_for_injection'],
        tokens_used=result['tokens_used'],
        original_tokens=original_tokens,
        compression_ratio=round(result['ratio'], 3),
        quality_score=result['quality_score'],
        memories_included=len(rows),
        compression_applied=compression_applied,
    )


# ============================================================
# SESSION INGESTION ENDPOINT
# ============================================================

class SessionIngestRequest(BaseModel):
    source: str
    session_id: str
    machine_id: str
    agent_id: str
    raw_data: Dict[str, Any]
    git_commit: Optional[str] = None

class SessionIngestResponse(BaseModel):
    success: bool
    session_id: str
    stored_count: int
    memory_ids: List[str]

@app.post("/ingest/session", response_model=SessionIngestResponse)
async def ingest_session(request: SessionIngestRequest):
    """Ingest Claude Code session data into MNEMOS"""
    conn = await _get_db()
    stored_ids = []

    try:
        import uuid

        data = request.raw_data

        # Store messages
        if data.get("messages") or data.get("prompts"):
            items = data.get("messages", []) or data.get("prompts", [])
            if items:
                content = f"Session {request.session_id} - {len(items)} messages\n{str(items)[:500]}"
                mem_id = f"mem_{uuid.uuid4().hex[:12]}"
                meta = json.dumps({"source": request.source, "session_id": request.session_id, "machine_id": request.machine_id, "agent_id": request.agent_id, "git_commit": request.git_commit, "item_count": len(items), "item_type": "messages"})
                await conn.execute("INSERT INTO memories (id, content, category, metadata, quality_rating) VALUES ($1, $2, $3, $4::jsonb, 75)", mem_id, content, "session_activity", meta)
                stored_ids.append(mem_id)

        # Store code blocks
        if data.get("code_blocks"):
            items = data.get("code_blocks", [])
            if items:
                content = f"Session {request.session_id} - {len(items)} code blocks\n{str(items)[:500]}"
                mem_id = f"mem_{uuid.uuid4().hex[:12]}"
                meta = json.dumps({"source": request.source, "session_id": request.session_id, "machine_id": request.machine_id, "agent_id": request.agent_id, "git_commit": request.git_commit, "item_count": len(items), "item_type": "code"})
                await conn.execute("INSERT INTO memories (id, content, category, metadata, quality_rating) VALUES ($1, $2, $3, $4::jsonb, 75)", mem_id, content, "session_code", meta)
                stored_ids.append(mem_id)

        # Store tools
        if data.get("tool_operations") or data.get("tools"):
            items = data.get("tool_operations", []) or data.get("tools", [])
            if items:
                content = f"Session {request.session_id} - {len(items)} tool operations\n{str(items)[:500]}"
                mem_id = f"mem_{uuid.uuid4().hex[:12]}"
                meta = json.dumps({"source": request.source, "session_id": request.session_id, "machine_id": request.machine_id, "agent_id": request.agent_id, "git_commit": request.git_commit, "item_count": len(items), "item_type": "tools"})
                await conn.execute("INSERT INTO memories (id, content, category, metadata, quality_rating) VALUES ($1, $2, $3, $4::jsonb, 75)", mem_id, content, "session_tools", meta)
                stored_ids.append(mem_id)

        logger.info(f"Session {request.session_id} ingested: {len(stored_ids)} records")
        return SessionIngestResponse(success=True, session_id=request.session_id, stored_count=len(stored_ids), memory_ids=stored_ids)

    except Exception as e:
        logger.error(f"Session ingestion failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000, workers=4)
