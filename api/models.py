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
    distillation_worker: Optional[str] = None  # idle, healthy, error, disabled, unavailable


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


# ── Session Ingestion (Claude Code integration) ────────────────────────────────

class SessionIngestRequest(BaseModel):
    """Ingest session data from Claude Code."""
    raw_data: Dict[str, Any]
    session_id: str
    source: Optional[str] = "claude-code"
    machine_id: Optional[str] = None
    agent_id: Optional[str] = None
    git_commit: Optional[str] = None


class SessionIngestResponse(BaseModel):
    """Response from session ingestion."""
    success: bool
    session_id: str
    stored_count: int
    memory_ids: List[str]


# ── Knowledge Graph Models ────────────────────────────────────────────────────

class KGTriple(BaseModel):
    """Knowledge graph triple (subject → predicate → object)."""
    id: str
    subject: str
    predicate: str
    object: str
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    valid_from: str
    valid_until: Optional[str] = None
    memory_id: Optional[str] = None
    confidence: float = 1.0
    created: str


class KGTripleCreate(BaseModel):
    """Create a knowledge graph triple."""
    subject: str
    predicate: str
    object: str
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    memory_id: Optional[str] = None
    confidence: float = 1.0


class KGTripleUpdate(BaseModel):
    """Update a knowledge graph triple."""
    object: Optional[str] = None
    confidence: Optional[float] = None
    valid_until: Optional[str] = None
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    predicate: Optional[str] = None
    subject: Optional[str] = None


class KGTripleListResponse(BaseModel):
    """List of knowledge graph triples."""
    count: int
    triples: List[KGTriple]


# ── Admin Models (User & API Key Management) ──────────────────────────────────

class UserCreateRequest(BaseModel):
    """Create a new user."""
    id: str
    display_name: str
    email: Optional[str] = None
    role: str = "user"  # "user" or "root"


class UserResponse(BaseModel):
    """User response."""
    id: str
    display_name: str
    email: Optional[str] = None
    role: str
    created_at: str


class ApiKeyCreateRequest(BaseModel):
    """Create an API key."""
    label: Optional[str] = None


class ApiKeyResponse(BaseModel):
    """API key response."""
    id: str
    user_id: str
    key_prefix: str
    label: Optional[str] = None
    created_at: str
    last_used: Optional[str] = None
    revoked: bool = False
    raw_key: Optional[str] = None  # only returned on creation


# ── v3.0.0 Consultations (GRAEAE Reasoning Domain) ────────────────────────────

class ConsultationResponse(BaseModel):
    """Response from GRAEAE consultation."""
    consultation_id: Optional[str] = None
    all_responses: Dict[str, Any]  # provider → response data
    consensus_response: Optional[str] = None
    consensus_score: Optional[float] = None
    winning_muse: Optional[str] = None
    cost: Optional[float] = None
    latency_ms: Optional[float] = None
    mode: str
    timestamp: str


class ConsultationArtifact(BaseModel):
    """Structured output from consultation."""
    consultation_id: str
    citations: List[str]  # memory IDs referenced
    memory_refs: List[Dict[str, Any]]  # {memory_id, relevance_score, content}
    audit_hash: Optional[str] = None  # SHA-256 of prompt+response chain
    created_at: str


class ProviderResponse(BaseModel):
    """Single provider's response in consensus."""
    provider: str
    model_id: str
    status: str
    response_text: str
    latency_ms: float
    final_score: float
    quality_score: Optional[float] = None


class AuditLogEntry(BaseModel):
    """Hash-chained audit log entry."""
    id: str
    sequence_num: int
    consultation_id: Optional[str] = None
    prompt_hash: str
    response_hash: str
    chain_hash: str
    prev_id: Optional[str] = None
    task_type: Optional[str] = None
    provider: Optional[str] = None
    quality_score: Optional[float] = None
    created_at: str


class AuditVerifyResponse(BaseModel):
    """Audit chain integrity verification."""
    valid: bool
    entries_checked: int
    first_broken_sequence: Optional[int] = None
    message: str


# ── v3.0.0 Providers (Model Registry & Routing) ───────────────────────────────

class ProviderListResponse(BaseModel):
    """List of available LLM providers."""
    providers: List[str]
    total_models: int
    last_sync: Optional[str] = None


class ModelRecommendation(BaseModel):
    """Model recommendation for a task type."""
    recommended: Dict[str, Any]  # {provider, model_id, cost_per_mtok}
    reasoning: str
    quality_score: Optional[float] = None
    context_window: Optional[int] = None
    alternatives: Optional[List[Dict[str, Any]]] = None
