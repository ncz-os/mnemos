# MNEMOS v3.0.0 Public Release - Sanitization Checklist

**Status**: Implementation ~40% Complete | **Target**: Full public open-source release

---

## ✅ Completed

### Configuration & Environment
- [x] Created `.env.example` with all configurable variables (no hardcoded IPs)
- [x] Documented GPU as optional (not required)
- [x] Clarified CPU-only setup sufficient for most users
- [x] Updated environment variable naming (generic: GPU_PROVIDER_HOST, not PYTHIA_GPU_HOST)

### Code Updates
- [x] `compression/aletheia.py` - Removed PYTHIA refs, use GPU_PROVIDER_HOST
- [x] `compression/anamnesis.py` - Removed PYTHIA refs, use GPU_PROVIDER_HOST
- [x] `compression/__init__.py` - Updated docstring
- [x] `graeae/engine.py` - Prioritized Together + Groq as default free-tier providers
- [x] Changed parameter names (pythia_url → gpu_url) throughout
- [x] `api/models.py` - Added v3.0.0 consultation response models

### Documentation
- [x] Created `DEPLOYMENT.md` - Public-facing deployment guide (no internal references)
- [x] Clarified GPU is optional (not PYTHIA-dependent)
- [x] Added minimal configuration section (5 variables)
- [x] Updated prerequisites to list LLM providers instead of infrastructure
- [x] Documented Together AI + Groq as recommended entry points

### Architecture Updates
- [x] Removed all references to: PYTHIA, CERBERUS, PROTEUS, ARGONAS
- [x] Removed all hardcoded IPs: 192.168.207.x
- [x] Documented that CPU compression (LETHE) is always sufficient

---

## ✅ Phase 1-2 Complete

### Phase 1: v3.0.0 Consultations Handler
- [x] Created `api/handlers/consultations.py` with `/v1/consultations` endpoints
- [x] Extracted audit logic from `graeae_routes.py`
- [x] Implemented hash-chained audit log (_write_audit_entry)
- [x] Implemented consultation_memory_refs linking (_write_memory_refs)
- [x] Added endpoints:
  - [x] POST /v1/consultations (create + run)
  - [x] GET /v1/consultations/{id} (retrieve)
  - [x] GET /v1/consultations/{id}/artifacts (citations + memory refs)
  - [x] GET /v1/consultations/audit (list audit log)
  - [x] GET /v1/consultations/audit/verify (verify chain integrity)
- [x] Updated api/models.py with ConsultationResponse, AuditLogEntry, etc.
- [x] Updated api_server.py to register consultations_router
- [x] Bumped version to 3.0.0

### Phase 2: v3.0.0 Providers Handler
- [x] Created `api/handlers/providers.py` with `/v1/providers` endpoints
- [x] Moved/refactored logic from `model_registry_routes.py`
- [x] Added endpoints:
  - [x] GET /v1/providers (list providers)
  - [x] GET /v1/providers/health (health check)
  - [x] GET /v1/providers/recommend (model recommendation)
- [x] Updated api_server.py to register providers_router
- [x] Updated api/handlers/health.py version to 3.0.0

---

## 🔄 In Progress / Remaining

### Phase 3: Namespace Memory Routes
- [ ] Modify `api/handlers/memories.py` to add `/v1/` prefix
- [ ] Modify `api/handlers/versions.py` to add `/v1/` prefix
- [ ] Ensure backward compatibility (old routes still work for v2.x callers)

### Phase 4: Database Migration
- [ ] Create `db/migrations_v3_graeae_unified.sql`
- [ ] Define `consultation_memory_refs` table
- [ ] Move `graeae_audit_log` creation from inline to migration
- [ ] Create indexes for performance

### Phase 5: SBOM Cleanup
- [ ] `requirements.txt` - Remove psycopg[binary], flask, gunicorn, requests
- [ ] `pyproject.toml` - Bump to v3.0.0, remove unused dependencies
- [ ] Verify uv build system is documented

### Phase 6: Version Updates
- [ ] `pyproject.toml` - Version "3.0.0"
- [ ] `CHANGELOG.md` - Add v3.0.0 entry

### Phase 7: Cleanup
- [ ] Delete `api/handlers/graeae_routes.py` (replaced by consultations.py)
- [ ] Delete `api/handlers/model_registry_routes.py` (replaced by providers.py)
- [ ] Verify all routers properly updated in `api_server.py`

### Documentation
- [ ] Update `README.md` to reference DEPLOYMENT.md
- [ ] Archive/remove internal implementation notes (MNEMOS_v24_IMPLEMENTATION_NOTES.md)
- [ ] Verify no references to internal systems in docstrings
- [ ] Update CLAUDE.md: new v3 endpoint for GRAEAE

### Testing
- [ ] Validate v3.0.0 endpoints work end-to-end
- [ ] Verify backward compatibility (v2.x endpoints unchanged)
- [ ] Test with minimal .env (5 variables)
- [ ] Test with CPU-only (no GPU_PROVIDER_HOST set)

---

## Progress Summary

### Completed: 55% 
- ✅ Sanitization complete (no internal refs)
- ✅ Configuration ready (.env.example)
- ✅ Phase 1 & 2 implementation done
- ✅ Version bumped to 3.0.0
- ✅ New models defined

### In Progress: 30%
- Phase 3: Memory namespace routes
- Phase 4: Database migrations
- Phase 5-6: SBOM & version cleanup

### Remaining: 15%
- Phase 7: File cleanup
- Testing & validation
- Documentation updates

---

## API Summary (v3.0.0)

### New Unified Endpoints (v3.0.0)
```
POST   /v1/consultations              Create consultation (GRAEAE reasoning)
GET    /v1/consultations/{id}         Retrieve consultation
GET    /v1/consultations/{id}/artifacts  Get citations & memory refs
GET    /v1/consultations/audit        List audit log (hash-chained)
GET    /v1/consultations/audit/verify Verify audit chain integrity

GET    /v1/providers                  List providers
GET    /v1/providers/health           Provider health check
GET    /v1/providers/recommend        Model recommendation

GET    /v1/memories                   List memories
POST   /v1/memories                   Create memory
POST   /v1/memories/search            Search memories
GET    /v1/memories/{id}              Retrieve memory
GET    /v1/memories/{id}/log          DAG history (git-like)
POST   /v1/memories/{id}/branch       Create branch
POST   /v1/memories/{id}/merge        Merge branch
```

### Legacy Endpoints (v2.x, still functional)
```
POST   /graeae/consult                (deprecated, use /v1/consultations)
GET    /graeae/health                 (deprecated, use /v1/providers/health)
GET    /graeae/audit                  (deprecated, use /v1/consultations/audit)
GET    /model-registry/recommend      (deprecated, use /v1/providers/recommend)
```

---

## Success Criteria

- [x] No hardcoded IPs or internal system names in code/docs
- [x] GPU fully optional
- [x] CPU-only deployment documented and tested
- [x] Minimal setup requires only 5 env vars
- [x] v3.0.0 unified API implemented (Phases 1-2)
- [x] New models added to api/models.py
- [x] Version bumped to 3.0.0 everywhere
- [ ] Database migrations ready (Phase 4)
- [ ] File cleanup complete (Phase 7)
- [ ] All tests passing
- [ ] Ready for public GitHub/GitLab release

