"""Pydantic models for MNEMOS API."""
from typing import Optional, Dict, Any, List
from datetime import datetime
from pydantic import BaseModel


class ConsultationRequest(BaseModel):
    prompt: str
    task_type: Optional[str] = "reasoning"
    context: Optional[str] = None
    mode: Optional[str] = "auto"
    limit_chars: Optional[int] = None
    format: Optional[str] = "full"


class StatsResponse(BaseModel):
    total_memories: int
    total_compressions: int
    average_compression_ratio: float
    average_quality_rating: int
    memories_by_category: Dict[str, int]
    memories_by_subcategory: Dict[str, Dict[str, int]] = {}
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
    verbatim_content: Optional[str] = None
    # v1 provenance + ownership
    owner_id: Optional[str] = None
    group_id: Optional[str] = None
    namespace: Optional[str] = None
    permission_mode: Optional[int] = None
    source_model: Optional[str] = None
    source_provider: Optional[str] = None
    source_session: Optional[str] = None
    source_agent: Optional[str] = None


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
    # Provenance filters (v2.3.0+) — all optional, ANDed together when set
    source_provider: Optional[str] = None
    source_model: Optional[str] = None
    source_agent: Optional[str] = None
    namespace: Optional[str] = None


class MemoryCreateRequest(BaseModel):
    content: str
    category: str = "facts"
    subcategory: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    verbatim_content: Optional[str] = None


class MemoryUpdateRequest(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    quality_rating: Optional[int] = None


class BulkCreateRequest(BaseModel):
    memories: List[MemoryCreateRequest]


class BulkCreateResponse(BaseModel):
    created: int
    failed: int
    details: List[Dict[str, Any]] = []


class RehydrationRequest(BaseModel):
    query: str
    limit: int = 5
    budget_tokens: Optional[int] = None


class RehydrationResponse(BaseModel):
    context: str
    tokens_used: int
    original_tokens: int
    compression_ratio: float
    quality_score: int
    memories_included: int
    compression_applied: bool


# ── Session Management Models (NEW) ────────────────────────────────────────

class ChatMessage(BaseModel):
    """Message in conversation history."""
    role: str  # "user", "assistant", "system"
    content: str
    timestamp: Optional[str] = None
    model: Optional[str] = None


class SessionContext(BaseModel):
    """Server-side context for a session."""
    session_id: str
    user_id: str
    created_at: str
    last_activity: str
    message_count: int
    total_tokens: int
    model: str
    compression_tier: int = 1  # 1=LETHE, 2=ALETHEIA, 3=ANAMNESIS
    injected_memories: Optional[List[str]] = None


class SessionRequest(BaseModel):
    """Create a new session."""
    model: Optional[str] = "gpt-4o"
    compression_tier: Optional[int] = 1
    initial_context: Optional[str] = None


class SessionResponse(BaseModel):
    """Session created successfully."""
    session_id: str
    created_at: str
    model: str
    compression_tier: int


class SessionMessage(BaseModel):
    """Add a message to a session (stateful chat)."""
    role: str  # "user" or "assistant"
    content: str
    model: Optional[str] = None  # Override session model


class SessionMessageResponse(BaseModel):
    """Response to a session message."""
    session_id: str
    message_id: str
    role: str
    content: str
    model: str
    timestamp: str
    tokens_used: int
    memories_injected: int
    compression_ratio: Optional[float] = None


class SessionHistoryRequest(BaseModel):
    """Get session conversation history."""
    limit: int = 50
    offset: int = 0


class SessionHistoryResponse(BaseModel):
    """Session conversation history."""
    session_id: str
    messages: List[ChatMessage]
    total_messages: int
    total_tokens: int
    created_at: str
