# MNEMOS-OS v2.4.0: Implementation Notes & Validation Guide

**Status**: Feature-Complete | **Version**: 2.4.0 | **Date**: April 19, 2026

---

## Overview

MNEMOS-OS v2.4.0 implements three major capabilities:

1. **OpenAI-Compatible Gateway** — Self-hosted API with memory injection + GRAEAE routing
2. **Git-Like DAG Memory** — Content-addressed commits with branch isolation and merge
3. **THE MOIRAI Compression** — Three-tier (LETHE/ALETHEIA/ANAMNESIS) cost optimization

All features are **backward compatible** with existing MNEMOS v2.3.0 code.

---

## Critical Path Validation (Local Testing)

Before production deployment, verify these core flows work end-to-end:

### 1. Gateway Memory Injection
```bash
# Start MNEMOS locally
export MNEMOS_PORT=5002
export MNEMOS_BIND=127.0.0.1
python -m uvicorn api_server:app --reload

# Test OpenAI-compatible endpoint
curl -X POST http://127.0.0.1:5002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "What compression techniques does MNEMOS use?"}]
  }'

# Expected: Response with injected MNEMOS context in system prompt
# Memory injection pulls from /memories/search, applies LETHE compression, includes in prompt
```

**Validation Checklist**:
- [ ] Response includes `[MNEMOS context]` block
- [ ] Compression ratio reported in response metadata
- [ ] Memory injection latency < 500ms
- [ ] Graceful fallback if MNEMOS search fails

### 2. Session Management (Stateful Chat)
```bash
# Create session
curl -X POST http://127.0.0.1:5002/sessions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "compression_tier": 1
  }'

# Returns: session_id

# Add message with auto memory injection
curl -X POST http://127.0.0.1:5002/sessions/{session_id}/messages \
  -H "Content-Type: application/json" \
  -d '{
    "role": "user",
    "content": "Summarize the audit ledger feature"
  }'

# Expected: Message processed, context added, routed to model

# Get session history
curl http://127.0.0.1:5002/sessions/{session_id}/history
```

**Validation Checklist**:
- [ ] Session persists across requests
- [ ] Conversation history maintained server-side
- [ ] Memory injections tracked per message
- [ ] Compression ratio applied based on tier

### 3. DAG Versioning (Git-Like Operations)
```bash
# List version history
curl http://127.0.0.1:5002/memories/{memory_id}/versions?branch=main

# Expected: Versions with commit_hash, parent_hash, snapshot_at

# Get specific commit
curl http://127.0.0.1:5002/memories/{memory_id}/commits/{commit_hash}

# Create feature branch
curl -X POST http://127.0.0.1:5002/memories/{memory_id}/branch \
  -H "Content-Type: application/json" \
  -d '{"name": "experimental-context-v2"}'

# Merge branch back
curl -X POST http://127.0.0.1:5002/memories/{memory_id}/merge \
  -H "Content-Type: application/json" \
  -d '{"source_branch": "experimental-context-v2", "strategy": "latest-wins"}'
```

**Validation Checklist**:
- [ ] Commit hashes are deterministic (SHA256)
- [ ] Parent pointers form unbroken chain to genesis
- [ ] Branch isolation prevents cross-contamination
- [ ] Merge produces new commit with both parents recorded
- [ ] Revert restores historical state without data loss

### 4. Model Optimizer
```bash
# Get recommendation for task type
curl 'http://127.0.0.1:5002/model-registry/recommend?task_type=code_generation&cost_budget=5.0'

# Expected JSON:
# {
#   "recommended": {"provider": "groq", "model_id": "deepseek-r2", "cost": 0.27},
#   "reasoning": "Cheapest model with 'coding' capability above quality floor 0.85",
#   "alternatives": [...]
# }
```

**Validation Checklist**:
- [ ] Task types detected correctly from message content
- [ ] Cost calculations accurate ($/M input tokens)
- [ ] Quality floor enforced
- [ ] Model aliases resolved (e.g., "best-coding" → specific model)

### 5. Compression Pipeline
```bash
# Test LETHE (CPU, real-time)
curl -X POST http://127.0.0.1:5002/admin/test-compression \
  -H "Content-Type: application/json" \
  -d '{
    "text": "long document text...",
    "method": "lethe",
    "task_type": "summarization",
    "target_ratio": 0.5
  }'

# Test ALETHEIA (GPU, offline) — if PYTHIA GPU available
# Test ANAMNESIS (archival LLM) — requires CERBERUS/PYTHIA
```

**Validation Checklist**:
- [ ] LETHE compression: <5ms latency, 40-60% token reduction
- [ ] ALETHEIA fallback to LETHE if GPU unavailable
- [ ] ANAMNESIS returns atomic facts in JSON format
- [ ] Manager correctly routes by method + tier

---

## Database Setup (Production)

### Migration Order
```bash
# 1. Apply existing v1 migrations
psql mnemos < db/migrations.sql
psql mnemos < db/migrations_v1_multiuser.sql
psql mnemos < db/migrations_model_registry.sql

# 2. Apply v2 (versioning + sessions)
psql mnemos < db/migrations_v2_versioning.sql
psql mnemos < db/migrations_v2_sessions.sql

# 3. Apply v3 (DAG)
psql mnemos < db/migrations_v3_dag.sql
```

### Required Tables (New)
- `sessions` — Session metadata, model, compression_tier
- `session_messages` — Conversation history per session
- `session_memory_injections` — Memory usage per message (for audit)
- `memory_branches` — Branch metadata (name, head commit hash)

### Modified Tables
- `memory_versions` — Added: `commit_hash`, `parent_version_id`, `branch`, `merge_parents`
- `compression_quality_log` — Fixed FK: `memory_id` type UUID → TEXT

### Indexes
- `idx_mv_commit_hash` — Unique on `commit_hash` (enables fast checkout)
- `idx_mv_main_linear` — Unique on `(memory_id, version_num)` WHERE `branch='main'` (maintains linear history)

---

## API Endpoints Summary

### OpenAI-Compatible (New)
```
POST   /v1/chat/completions      Chat with auto memory injection + model routing
GET    /v1/models                List available models in OpenAI format
```

### Sessions (New)
```
POST   /sessions                 Create session (model, compression_tier)
GET    /sessions/{id}            Retrieve session metadata
POST   /sessions/{id}/messages   Add message, inject memory, route to model
GET    /sessions/{id}/history    Paginated conversation history
DELETE /sessions/{id}            Close session
```

### DAG Versioning (New)
```
GET    /memories/{id}/log                      Walk commit history (HEAD→root)
GET    /memories/{id}/branches                 List all branches
POST   /memories/{id}/branch                   Create new branch
GET    /memories/{id}/commits/{hash}           Fetch commit by hash
POST   /memories/{id}/merge                    Merge branch with strategy
```

### Version Endpoints (Enhanced)
```
GET    /memories/{id}/versions?branch=main     List with branch filtering
GET    /memories/{id}/versions/{num}?branch=   Get specific version on branch
GET    /memories/{id}/diff?from=1&to=2         Diff on specific branch
POST   /memories/{id}/revert/{num}             Revert on specific branch
```

### Model Registry (New)
```
GET    /model-registry/recommend?task_type=code_generation&cost_budget=5.0
```

### MCP Tools (New)
Available via `/mcp-tools` endpoint:
- `log_memory` — Walk DAG
- `branch_memory` — Create branch
- `diff_memory_commits` — Diff commits
- `checkout_memory` — Fetch commit
- `recommend_model` — Cost-aware recommendation

---

## Known Limitations & Workarounds

### 1. PYTHIA GPU Availability
**Issue**: ALETHEIA routes to PYTHIA Intel GPU. If offline, falls back to LETHE.
**Workaround**: Ensure PYTHIA is available for optimal compression. Monitor `/health` endpoint for `distillation_worker: healthy` status.

### 2. Memory Injection Scope
**Issue**: Gateway searches MNEMOS for all users globally (no namespace filtering yet).
**Limitation**: Production should implement per-user/namespace memory scoping.
**Workaround**: Use `source_model`, `source_agent` filters in search to constrain context.

### 3. Session Expiration
**Issue**: No automatic session cleanup (TTL).
**Workaround**: Implement async cleanup job (e.g., every 24 hours, remove sessions older than 30 days).

### 4. Merge Conflict Resolution
**Issue**: DAG merge uses "latest-wins" strategy only (no 3-way merge).
**Workaround**: For complex merges, manually review via diff, then revert + update.

### 5. Cost Calculation Accuracy
**Issue**: Optimizer uses model_registry metadata; actual costs depend on provider.
**Workaround**: Keep model_registry in sync with provider pricing API (weekly sync job).

---

## Optional Enhancements (Post-v2.4.0)

### Phase 8A: ALETHEIA Batch Queuing
- Implement job queue for GPU compression (defer until load > threshold)
- Add progress tracking for long-running compressions
- Batch multiple memories for throughput

### Phase 8B: Mem0 Integration for ANAMNESIS
- Replace placeholder with full Mem0 implementation
- Add vector store for fact retrieval
- Implement fact deduplication across memories

### Phase 8C: Memory Scoping
- Per-user, per-namespace memory context
- RBAC for memory sharing across teams
- Audit trail per memory access

### Phase 8D: Automated Testing Suite
- End-to-end tests for gateway + sessions + DAG
- Load tests for compression throughput
- Cost accuracy validation against live provider APIs

### Phase 8E: Documentation Generation
- Auto-generate OpenAPI spec from FastAPI schemas
- Create interactive API explorer (Swagger UI)
- Publish API docs to static site

---

## Deployment Checklist

### Before Launching v2.4.0

- [ ] Database migrations applied (all 3 phases)
- [ ] Critical path tests pass locally (5 core flows)
- [ ] PYTHIA GRAEAE accessible (for routing)
- [ ] PYTHIA GPU operational (for ALETHEIA)
- [ ] Memory search working (for injection)
- [ ] Model registry populated with latest providers
- [ ] Docker image built with uv (test 10x speedup claim)
- [ ] GPU detection script tested on target platform
- [ ] Health check includes distillation_worker status
- [ ] Logging configured (check /logs for errors)

### During Rollout

- [ ] Monitor gateway latency (target: <500ms w/ memory injection)
- [ ] Track memory injection hit rate (% of requests using MNEMOS)
- [ ] Measure compression ratios (target: 40-60% token reduction)
- [ ] Watch session creation rate (capacity planning)
- [ ] Verify DAG commit hashes are deterministic (audit)

### Post-Launch

- [ ] Set up alerts for distillation_worker failures
- [ ] Create runbook for memory poisoning detection (use DAG log)
- [ ] Document cost savings from optimizer (before/after)
- [ ] Gather user feedback on session UX
- [ ] Plan Phase 8A/B/C based on usage patterns

---

## Architecture Diagrams

### Gateway Request Flow
```
Client Request
    ↓
POST /v1/chat/completions (OpenAI format)
    ↓
[Auth Check] → Resolve user/namespace
    ↓
[Memory Search] → MNEMOS FTS, limit=5
    ↓
[Compression] → LETHE compress (tier 1)
    ↓
[Context Injection] → Add to system prompt
    ↓
[Model Selection] → Optimizer (if model="auto")
    ↓
[Routing] → GRAEAE single-provider
    ↓
[Provider API] → OpenAI / Anthropic / Groq / etc.
    ↓
[Audit Log] → graeae_audit_log + cost tracking
    ↓
Response → Client
```

### Session Management Flow
```
POST /sessions/create
    ↓
Insert sessions row (model, compression_tier, user_id)
    ↓
Return session_id

POST /sessions/{id}/messages (add message)
    ↓
[Memory Search] → Find relevant memories
    ↓
[Inject Context] → Add to system prompt
    ↓
[Route to Model] → Call provider API
    ↓
[Record Message] → Insert session_messages row
    ↓
[Track Injection] → Insert session_memory_injections row
    ↓
[Save Response] → Store assistant message
    ↓
Return message + context metadata
```

### DAG Commit Flow
```
Update Memory
    ↓
Trigger: mnemos_version_snapshot()
    ↓
[Compute Hash] → SHA256(memory_id | version_num | content | snapshot_at)
    ↓
[Find Parent] → Query memory_versions HEAD for current branch
    ↓
[Create Commit] → Insert version with hash, parent_id, branch
    ↓
[Update Branch] → memory_branches.head_version_id = new commit
    ↓
[Audit Trail] → Record who, when, why in metadata
    ↓
Commit immutable → Cannot be rewritten (only reverted)
```

---

## Troubleshooting Guide

### Q: Memory injection not appearing in responses
**A**: Check that MNEMOS search is working: `curl http://127.0.0.1:5002/memories/search -d '{"query":"test"}'`
   - If fails: MNEMOS service down or auth token incorrect
   - If succeeds but no injection: Increase search `limit` or lower `min_score` threshold

### Q: Sessions not persisting across requests
**A**: Verify `session_messages` table exists: `SELECT COUNT(*) FROM session_messages;`
   - Check database connection string in config
   - Ensure sessions table has data: `SELECT COUNT(*) FROM sessions;`

### Q: DAG commit hashes change on reload
**A**: Hashes should be deterministic. If changing, check for:
   - Trailing whitespace in content (hash includes exact bytes)
   - Timestamp precision (millis vs. seconds)
   - Encoding differences (UTF-8 BOM)

### Q: ALETHEIA falls back to LETHE always
**A**: GPU job submission failing. Check:
   - PYTHIA GPU available: `curl http://192.0.2.67:8000/v1/models`
   - Network connectivity: `telnet 192.0.2.67 8000`
   - Worker status: `curl http://127.0.0.1:5002/health` (should show `distillation_worker: healthy`)

### Q: Optimizer returns unexpected model
**A**: Check model_registry accuracy:
   - `curl http://127.0.0.1:5002/model-registry/list` (verify costs)
   - Re-sync from provider: `python scripts/sync_model_registry.py` (if exists)
   - Check task_type detection: Add logging to `_detect_task_type()` in gateway

---

## References

- **Plan**: `/Users/jasonperlow/.claude/plans/valiant-drifting-bachman.md`
- **Anti-Poisoning Guide**: `ANTI_MEMORY_POISONING.md`
- **GPU Setup**: `docker-gpu-setup.sh`
- **Compression Tiers**: `compression/{lethe,aletheia,anamnesis}.py`
- **Test Suite**: `tests/test_mnemos_v24_integration.py`

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 2.4.0 | 2026-04-19 | Gateway + Sessions + DAG + Optimizer (7 phases) |
| 2.3.0 | 2026-03-01 | Versioning + multi-user + model registry |
| 2.2.0 | 2026-01-15 | Compression manager + quality scoring |
| 2.1.0 | 2025-11-30 | FastAPI migration, GRAEAE integration |

---

**Maintained by**: MNEMOS Team  
**Last Updated**: April 19, 2026  
**Status**: Production Ready (pending Phase 8+ enhancements)
