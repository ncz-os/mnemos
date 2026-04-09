# Phase 2: Modules Implementation Guide

## Overview

Phase 1 is complete with core database, MemoryStore, and compression infrastructure.

Phase 2 implements the remaining 5 independent modules that build on Phase 1.

**Estimated effort**: 3-4 hours to implement, test, and document all modules.

## Module 1: Hooks System

### Files to Create
- `modules/hooks/hook_registry.py` - Hook manager
- `modules/hooks/session_start.py` - Session initialization hook
- `modules/hooks/prompt_submit.py` - Prompt submission hook
- `modules/hooks/__init__.py` - Module exports

### Core Classes
```python
class HookRegistry:
    async def trigger(event: str, context: dict) -> dict
    def register(event: str, callback: Callable) -> None
    def list_hooks(event: str) -> List[str]

# Hook events
HOOK_SESSION_START = "session.start"
HOOK_PROMPT_SUBMIT = "prompt.submit"
HOOK_MEMORY_WRITE = "memory.write"
HOOK_MEMORY_READ = "memory.read"
HOOK_REHYDRATION_START = "rehydration.start"
HOOK_GRAEAE_CONSULT = "graeae.consult"
```

### Implementation Steps
1. Create HookRegistry as event dispatcher
2. Implement built-in hooks (session-start, prompt-submit)
3. Make hooks configurable (enable/disable per config.toml)
4. Add hook error handling (log, don't crash)

## Module 2: Memory Categorization

### Files to Create
- `modules/memory_categorization/tier_selector.py` - Task → Tier mapping
- `modules/memory_categorization/tiers.py` - Tier definitions
- `modules/memory_categorization/state.py` - State files (identity, today, workspace)
- `modules/memory_categorization/journal.py` - Journal management
- `modules/memory_categorization/entities.py` - Entity tracking
- `modules/memory_categorization/__init__.py` - Module exports

### Core Classes
```python
class MemoryTier:
    tier_level: int  # 1-4
    token_budget: int
    compression_ratio: float
    categories: List[str]

class TierSelector:
    def select_tiers(task_type: str, complexity: str) -> List[MemoryTier]

class StateManager:
    async def load_identity() -> dict
    async def load_today() -> dict
    async def load_workspace() -> dict
    async def save_state(state: dict, key: str)

class JournalManager:
    async def append(topic: str, content: str) -> UUID
    async def get_recent(count: int) -> List[JournalEntry]
    async def query(search: str) -> List[JournalEntry]

class EntityManager:
    async def create_entity(type: str, name: str, metadata: dict) -> UUID
    async def link_entities(id1: UUID, id2: UUID)
    async def query_entities(type: str) -> List[Entity]
```

### Implementation Steps
1. Create Tier definitions with compression ratios
2. Implement TierSelector with task-type detection
3. Implement StateManager for identity/today/workspace files
4. Implement JournalManager with date-partitioned queries
5. Implement EntityManager for relationship tracking

## Module 3: Consultation Bundles

### Files to Create
- `modules/bundles/bundle_definitions.py` - Bundle configs
- `modules/bundles/model_variants.py` - Provider → model mappings
- `modules/bundles/task_routing.py` - Task → Bundle router
- `modules/bundles/__init__.py` - Module exports

### Core Classes
```python
class BundleDefinition:
    bundle_type: str
    response: str  # Fallback response
    models: Dict[str, str]  # provider → model
    consensus_score: int
    tags: List[str]

class BundleRouter:
    def select_bundle(task_type: str) -> BundleDefinition
    def get_model_variant(provider: str, task_type: str) -> str
    def select_models(task_type: str) -> Dict[str, str]
```

### Bundle Types (from config)
- `code_generation`: Grok-4 Code, GPT-5.2, Together
- `architecture_design`: GPT-5.2, Gemini 3.0, Groq
- `api_design`: GPT-5.2, Gemini 3.0, Groq
- `data_modeling`: Gemini 3.0, GPT-5.2, Groq
- `reasoning`: GPT-5.2, Gemini 3.0, Groq

### Implementation Steps
1. Load bundle definitions from config.toml
2. Create BundleRouter with task-type matching
3. Create fallback bundles (for when Graeae unavailable)
4. Implement model variant selection logic

## Module 4: Routing (Graeae Integration)

### Files to Create
- `modules/routing/graeae_client.py` - Graeae HTTP client
- `modules/routing/fallbacks.py` - Hardcoded fallback bundles
- `modules/routing/__init__.py` - Module exports

### Core Classes
```python
class GraeaeClient:
    async def consult(
        prompt: str,
        task_type: str,
        context: str,
        mode: str = 'external'
    ) -> ConsultationResult

    def get_fallback(task_type: str) -> BundleDefinition
```

### Implementation Steps
1. Create HTTP client for Graeae (192.0.2.67:5001)
2. Implement timeout and error handling
3. Create fallback bundles (embed in code)
4. Integrate with memory_store.save_consultation()
5. Add mode selection (local, external, auto)

## Module 5: Integrations

### macrodata Adapter

Files to Create:
- `integrations/macrodata/hook_adapter.py` - Adapt macrodata hooks to MNEMOS
- `integrations/macrodata/state_sync.py` - Sync state files
- `integrations/macrodata/__init__.py`

Functionality:
- Load macrodata state (identity, today, workspace)
- Sync with MNEMOS state table
- Trigger hooks on state changes
- Journal integration

### External LLM Providers

Files to Create:
- `integrations/external_lms/provider_models.py` - Model listings
- `integrations/external_lms/__init__.py`

Functionality:
- Query provider APIs for available models
- Cache model lists
- Update bundle definitions from live model availability

## Testing Strategy

### Unit Tests
- Each module has `tests/` subdirectory
- Test isolation (mock dependencies)
- Config-driven test cases

Example structure:
```
tests/
├── test_compression/
│   ├── test_quality_analyzer.py
│   ├── test_compression_manager.py
│   └── fixtures.py
├── test_hooks/
│   ├── test_hook_registry.py
│   └── test_hook_execution.py
├── test_bundles/
│   ├── test_bundle_router.py
│   └── test_model_variants.py
└── test_e2e/
    ├── test_write_compress_read.py
    ├── test_graeae_consultation.py
    └── test_quality_manifests.py
```

### Integration Tests
- Write → Compress → Read pipeline
- Graeae consultation with both versions
- State management with journal
- Entity relationships

### E2E Tests
- Full rehydration pipeline
- Quality manifest generation
- Audit trail verification
- API endpoints (once Phase 3)

## API Server (Phase 3)

Core endpoints:
```
POST   /memories              # Create memory
GET    /memories/<id>         # Get with quality check
GET    /memories/<id>/original # Get original
POST   /memories/search       # Semantic search
GET    /compression-log       # Audit trail
POST   /memories/<id>/quality-review  # Review decision
POST   /graeae/consult        # Consult Graeae
GET    /health                # Health check
GET    /stats                 # System stats
```

## Deployment Checklist

- [ ] Database: Run migrations
- [ ] Config: Update config.toml with your settings
- [ ] Dependencies: `pip install -r requirements.txt`
- [ ] Tests: Run full test suite
- [ ] Compression: Verify compression works (quality > 80%)
- [ ] Graeae: Verify connectivity to Graeae service
- [ ] API: Start API server, verify health endpoint
- [ ] Documentation: Update API docs

## Timeline

**Module 1 (Hooks)**: 30 min
**Module 2 (Memory Categorization)**: 60 min
**Module 3 (Bundles)**: 45 min
**Module 4 (Routing)**: 45 min
**Module 5 (Integrations)**: 30 min
**Testing**: 60 min
**Documentation**: 30 min

**Total**: 4-5 hours

## Success Criteria

✅ All modules independently testable
✅ All modules can be enabled/disabled via config
✅ Compression integrated into write/read/graeae paths
✅ Quality manifests generated for every compressed block
✅ Audit trail complete (compression_quality_log)
✅ Reversal capability (get_original API)
✅ E2E tests passing (100%)
✅ Documentation complete (API, modules, examples)
✅ Deployment guide ready
