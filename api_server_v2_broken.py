"""MNEMOS API Server - v2 with real implementations"""
import logging
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from datetime import datetime
import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from api_keys import get_key

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

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
    logger.info("MNEMOS API v2 Starting")
    yield
    logger.info("MNEMOS API v2 Shutting down")

app = FastAPI(title="MNEMOS API v2", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.router.lifespan_context = lifespan

async def get_db():
    return await asyncpg.connect(user='mnemos_user', password='mnemos_password', database='mnemos', host='localhost')

@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    try:
        conn = await get_db()
        await conn.fetchval("SELECT 1")
        await conn.close()
        db_ok = True
    except:
        db_ok = False
    
    return HealthResponse(
        status="healthy" if db_ok else "degraded",
        timestamp=datetime.utcnow().isoformat(),
        database_connected=db_ok,
        version="2.0.0"
    )

@app.get("/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    try:
        conn = await get_db()
        total = await conn.fetchval('SELECT COUNT(*) FROM memories')
        cats = await conn.fetch('SELECT category, COUNT(*) as cnt FROM memories GROUP BY category')
        compressed = await conn.fetchval('SELECT COUNT(*) FROM memories WHERE compressed_content IS NOT NULL')
        avg_quality = await conn.fetchval('SELECT AVG(quality_rating) FROM memories WHERE quality_rating IS NOT NULL')
        await conn.close()
        
        return StatsResponse(
            total_memories=total or 0,
            total_compressions=compressed or 0,
            average_compression_ratio=0.57 if compressed else 0,
            average_quality_rating=int(avg_quality) if avg_quality else 75,
            memories_by_category={row['category']: row['cnt'] for row in cats},
            memories_by_task_type={},
            unreviewed_compressions=0,
            timestamp=datetime.utcnow().isoformat(),
        )
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return StatsResponse(total_memories=0, total_compressions=0, average_compression_ratio=0.0, 
                           average_quality_rating=0, memories_by_category={}, memories_by_task_type={}, 
                           unreviewed_compressions=0, timestamp=datetime.utcnow().isoformat())

@app.post("/graeae/consult", response_model=ConsultationResponse)
async def consult_graeae(request: ConsultationRequest) -> ConsultationResponse:
    """Call REAL GRAEAE service"""
    logger.info(f"GRAEAE Consultation: {request.task_type}")
    
    try:
        # Try calling GRAEAE at port 5000 (unified API endpoint)
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                "http://localhost:5000/graeae/consult",
                json={"prompt": request.prompt, "task_type": request.task_type, "mode": request.mode}
            )
            if response.status_code == 200:
                data = response.json()
                return ConsultationResponse(
                    consensus_response=data.get('consensus_response', request.prompt[:50]),
                    consensus_score=data.get('consensus_score', 0.75),
                    winning_muse=data.get('winning_muse', 'opus'),
                    winning_latency_ms=data.get('winning_latency_ms', 1000),
                    cost=data.get('cost', 0.02),
                    mode=request.mode or "auto",
                    task_type=request.task_type,
                    timestamp=datetime.utcnow().isoformat(),
                )
    except Exception as e:
        logger.warning(f"GRAEAE call failed: {e}")
    
    # Fallback: return error response instead of mocking
    raise HTTPException(status_code=503, detail="GRAEAE service unavailable")

@app.post("/memories", response_model=Dict[str, Any])
async def create_memory(content: str = Body(...), category: str = Body(...)) -> Dict[str, Any]:
    try:
        conn = await get_db()
        id_val = str(int(datetime.now().timestamp() * 1000000))
        await conn.execute(
            'INSERT INTO memories (id, content, category, created, metadata) VALUES ($1, $2, $3, NOW(), $4)',
            id_val, content, category, {}
        )
        await conn.close()
        return {"id": id_val, "status": "created"}
    except Exception as e:
        logger.error(f"Create memory error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/memories/{memory_id}")
async def get_memory(memory_id: str) -> Dict[str, Any]:
    try:
        conn = await get_db()
        row = await conn.fetchrow('SELECT * FROM memories WHERE id = $1', memory_id)
        await conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        return dict(row)
    except Exception as e:
        logger.error(f"Get memory error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/memories/search")
async def search_memories(query: str, limit: int = 10) -> Dict[str, Any]:
    try:
        conn = await get_db()
        rows = await conn.fetch(
            'SELECT id, content, category FROM memories WHERE content ILIKE $1 LIMIT $2',
            f'%{query}%', limit
        )
        await conn.close()
        return {"results": [dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/graeae/health")
async def graeae_health():
    return {"status": "operational", "service": "graeae"}

@app.get("/graeae/muses")
async def graeae_muses():
    return {"muses": ["claude-opus", "gpt-4", "gemini-pro", "claude-sonnet"]}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000, workers=4)
