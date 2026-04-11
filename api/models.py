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


class MemoryCreateRequest(BaseModel):
    content: str
    category: str = "facts"
    subcategory: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    verbatim_content: Optional[str] = None   # explicit override; defaults to content if omitted
    # v1 provenance + ownership
    owner_id: Optional[str] = None
    namespace: Optional[str] = None
    source_model: Optional[str] = None
    source_provider: Optional[str] = None
    source_session: Optional[str] = None
    source_agent: Optional[str] = None


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
    verbatim_content: Optional[str] = None


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


class KGTripleUpdate(BaseModel):
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[str] = None
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    valid_until: Optional[str] = None    # ISO8601; set to mark expired
    confidence: Optional[float] = None


class BulkCreateRequest(BaseModel):
    memories: List[MemoryCreateRequest]


class BulkCreateResponse(BaseModel):
    created: int
    memory_ids: List[str]
    errors: List[str] = []


# ── Admin / auth models ───────────────────────────────────────────────────────

class ApiKeyCreateRequest(BaseModel):
    label: Optional[str] = None


class ApiKeyResponse(BaseModel):
    id: str
    user_id: str
    key_prefix: str
    label: Optional[str] = None
    created_at: str
    last_used: Optional[str] = None
    revoked: bool
    raw_key: Optional[str] = None   # only present on creation; never returned again


class UserCreateRequest(BaseModel):
    id: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str = "user"


class UserResponse(BaseModel):
    id: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    created_at: str
