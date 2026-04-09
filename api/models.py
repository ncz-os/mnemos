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
