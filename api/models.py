"""Pydantic models for MNEMOS API."""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel


class ConsultationRequest(BaseModel):
    prompt: str
    task_type: Optional[str] = "reasoning"
    context: Optional[str] = None
    mode: Optional[str] = "auto"
    limit_chars: Optional[int] = None
    format: Optional[str] = "full"


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


class MemoryItem(BaseModel):
    id: str
    content: str
    category: str
    subcategory: Optional[str] = None
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
    subcategory: Optional[str] = None
    include_compressed: Optional[bool] = False
    semantic: Optional[bool] = False   # True = pgvector cosine similarity; False = FTS


class MemoryCreateRequest(BaseModel):
    content: str
    category: str = "facts"
    subcategory: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    source: Optional[str] = "openclaw"


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


class MemoryUpdateRequest(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# ── Knowledge Graph models ────────────────────────────────────────────────────

class KGTripleCreate(BaseModel):
    subject: str
    predicate: str
    object: str
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    valid_from: Optional[str] = None     # ISO8601; defaults to NOW() if omitted
    valid_until: Optional[str] = None    # ISO8601; NULL means still valid
    memory_id: Optional[str] = None      # FK to memories.id
    confidence: float = 1.0


class KGTriple(BaseModel):
    id: str
    subject: str
    predicate: str
    object: str
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    valid_from: str
    valid_until: Optional[str] = None
    memory_id: Optional[str] = None
    confidence: float
    created: str


class KGTripleListResponse(BaseModel):
    count: int
    triples: List[KGTriple]
