# MNEMOS v3.0.0 Verification Guide

Quick start for verifying the v3.0.0 deployment before releasing to production.

---

## Prerequisites

**System Requirements:**
- Python 3.11+
- PostgreSQL 13+ (or access to a PostgreSQL instance)
- curl (for HTTP testing)
- bash (for verification script)

**Environment Setup:**
```bash
# Copy example configuration
cp .env.example .env

# Edit .env with your settings
# At minimum:
# - PG_HOST=localhost (or your PostgreSQL server)
# - PG_DATABASE=mnemos
# - PG_USER=postgres
# - PG_PASSWORD=your_password
# - MNEMOS_API_KEY=your_api_key_here

# Source environment
export $(cat .env | xargs)
```

---

## Quick Verification (5 minutes)

### 1. Start the Service
```bash
# In one terminal
python api_server.py

# You should see:
# INFO:     Uvicorn running on http://0.0.0.0:5002 (Press CTRL+C to quit)
```

### 2. Run Verification Script
```bash
# In another terminal
chmod +x verify_v3_deployment.sh
./verify_v3_deployment.sh
```

**Expected Output:**
```
MNEMOS v3.0.0 Deployment Verification

[1] Verifying Database Migrations
✓ consultation_memory_refs table exists
✓ graeae_audit_log table exists

[2] Checking Service Health
✓ Service running, version 3.0.0

[3] Testing New /v1/ Endpoints
✓ POST /v1/consultations responds
✓ GET /v1/consultations/audit responds
✓ GET /v1/providers responds
✓ GET /v1/memories responds

[4] Testing Backward Compatibility (v2.x)
✓ GET /graeae/health (v2) still works
✓ GET /model-registry (v2) still works
✓ GET /memories (v2) still works

[5] Verifying Code Structure
✓ api/handlers/consultations.py exists
✓ api/handlers/providers.py exists
✓ db/migrations_v3_graeae_unified.sql exists
✓ .env.example exists
✓ DEPLOYMENT.md exists

[6] Checking for Internal References (Sanitization)
✓ No hardcoded infrastructure references in code

[7] Checking Environment Configuration
✓ MNEMOS_API_KEY is set
✓ PG_HOST is set to localhost
ℹ GPU_PROVIDER_HOST not set (GPU optional, uses default providers)

[Summary]
✓ v3.0.0 deployment verification complete
```

### 3. Manual API Test
```bash
# Test creating a memory
curl -X POST http://localhost:5002/v1/memories \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Test memory",
    "category": "solutions",
    "tags": ["test"]
  }'

# Should return 200 + memory object with 'id' field
```

---

## Complete Verification (15 minutes)

### 1. Database Migration Verification
```bash
# Test migration in isolated environment (optional but recommended)
createdb mnemos_test
psql -d mnemos_test -f db/migrations_v3_graeae_unified.sql

# Verify tables
psql -d mnemos_test -c "\dt consultation_memory_refs"
psql -d mnemos_test -c "\di+ consultation_memory_refs*"

# Clean up
dropdb mnemos_test
```

### 2. Integration Tests
```bash
# Install test dependencies (if not already installed)
pip install pytest pytest-asyncio pytest-cov

# Run integration test suite
pytest tests/test_v3_integration.py -v

# With coverage report
pytest tests/test_v3_integration.py --cov=api.handlers.consultations --cov=api.handlers.providers --cov-report=html

# View coverage
open htmlcov/index.html
```

### 3. API Endpoint Testing
```bash
# Health check
curl http://localhost:5002/health | jq '.'

# List providers (v1 endpoint)
curl -X GET http://localhost:5002/v1/providers \
  -H "Authorization: Bearer $MNEMOS_API_KEY" | jq '.'

# Get provider health
curl http://localhost:5002/v1/providers/health | jq '.'

# Search memories (v1 endpoint)
curl -X POST http://localhost:5002/v1/memories/search \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "limit": 5}' | jq '.'

# List audit log (v1 endpoint)
curl -X GET http://localhost:5002/v1/consultations/audit \
  -H "Authorization: Bearer $MNEMOS_API_KEY" | jq '.'

# Verify audit chain
curl -X GET http://localhost:5002/v1/consultations/audit/verify \
  -H "Authorization: Bearer $MNEMOS_API_KEY" | jq '.'
```

### 4. Backward Compatibility Testing
```bash
# v2 graeae endpoint
curl -X GET http://localhost:5002/graeae/health | jq '.'

# v2 model registry endpoint
curl -X GET http://localhost:5002/model-registry \
  -H "Authorization: Bearer $MNEMOS_API_KEY" | jq '.[:2]'

# v2 memories endpoint
curl -X GET http://localhost:5002/memories \
  -H "Authorization: Bearer $MNEMOS_API_KEY" | jq '.[:2]'
```

### 5. Code Quality Check
```bash
# Check for linting issues (if linter installed)
# flake8 api/handlers/consultations.py api/handlers/providers.py

# Type checking (if mypy installed)
# mypy api/handlers/consultations.py --ignore-missing-imports

# Check for common issues
grep -n "TODO\|FIXME\|XXX" api/handlers/consultations.py api/handlers/providers.py

# Verify no secrets in code
grep -r "api_key\|secret\|password" api/ --include="*.py" | grep -v "env\|getenv\|environ" | wc -l
# (should be 0)
```

---

## Troubleshooting

### Service Won't Start

**Error**: `ERROR: Address already in use`
```bash
# Solution: Kill existing process on port 5002
lsof -i :5002
kill -9 <PID>

# Or use different port
PORT=5003 python api_server.py
```

**Error**: `ERROR: Database connection failed`
```bash
# Check PostgreSQL is running
pg_isready -h $PG_HOST

# Verify credentials
psql -h $PG_HOST -U $PG_USER -d $PG_DATABASE -c "SELECT 1"

# Check .env file is loaded
echo $PG_HOST $PG_DATABASE
```

### Migration Fails

**Error**: `ERROR: syntax error in migrations_v3_graeae_unified.sql`
```bash
# Check migration syntax
psql -d mnemos -f db/migrations_v3_graeae_unified.sql --echo-all

# Verify extensions installed (if needed)
psql -d mnemos -c "CREATE EXTENSION IF NOT EXISTS uuid-ossp;"
psql -d mnemos -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
```

**Error**: `ERROR: Foreign key constraint violation`
```bash
# Migration assumes graeae_consultations and memories tables exist
# Check:
psql -d mnemos -c "\dt graeae_consultations memories"

# If missing, run full schema setup first
psql -d mnemos < db/schema.sql
```

### Tests Fail

**Error**: `AuthenticationError: No token provided`
```bash
# Make sure MNEMOS_API_KEY is set
export MNEMOS_API_KEY=test-key
pytest tests/test_v3_integration.py::TestConsultationsV1 -v

# Or update test fixtures to use proper auth
```

**Error**: `ConnectionRefusedError: Service not running`
```bash
# Start service in separate terminal
python api_server.py

# Verify it's listening
curl http://localhost:5002/health

# Then run tests
pytest tests/test_v3_integration.py -v
```

### Verification Script Reports Errors

**Check**: Inspect the specific error
```bash
# Run verification with debug output
bash -x verify_v3_deployment.sh 2>&1 | head -50

# Or test individual checks
curl -v http://localhost:5002/health
curl -X GET http://localhost:5002/v1/providers \
  -H "Authorization: Bearer $MNEMOS_API_KEY" -v
```

---

## Verification Checklist

Use this before pushing to production:

- [ ] Service starts without errors
- [ ] `./verify_v3_deployment.sh` shows all green checkmarks
- [ ] Database migrations applied successfully
- [ ] All new `/v1/` endpoints respond correctly
- [ ] All v2.x endpoints still work (backward compatibility)
- [ ] Integration tests pass: `pytest tests/test_v3_integration.py -v`
- [ ] No internal infrastructure references in code
- [ ] Environment variables properly configured
- [ ] Health endpoint reports version 3.0.0
- [ ] Audit chain verifies successfully
- [ ] Manual API tests return expected results

---

## Performance Baselines

For reference (from testing):

| Endpoint | Method | Latency | Status |
|----------|--------|---------|--------|
| `/health` | GET | <10ms | 200 |
| `/v1/memories` | GET | <50ms | 200 |
| `/v1/memories/search` | POST | <100ms | 200 |
| `/v1/providers` | GET | <50ms | 200 |
| `/v1/providers/health` | GET | <100ms | 200 |
| `/v1/consultations/audit` | GET | <100ms | 200 |
| `/v1/consultations/audit/verify` | GET | <500ms | 200 |

---

## Next Steps After Verification

1. **All checks pass?**
   - Commit: `git commit -m "v3.0.0: Verification complete, ready for release"`
   - Tag: `git tag -a v3.0.0 -m "MNEMOS v3.0.0: Unified service, public release"`
   - Push: `git push origin main v3.0.0`
   - Create GitHub release from CHANGELOG.md

2. **Deploy to production**
   - Apply migration: `psql -d mnemos -f db/migrations_v3_graeae_unified.sql`
   - Deploy code: `git checkout v3.0.0 && pip install -e .`
   - Restart service: `systemctl restart mnemos` or `docker restart mnemos`
   - Verify: `./verify_v3_deployment.sh`

3. **Update downstream systems**
   - Update API endpoint references in:
     - OpenClaw: `/v1/consultations` → GRAEAE
     - ZeroClaw: `/v1/providers` → model selection
     - Claude Code hooks: `/v1/memories/search` → context injection

---

## Support

For issues or questions:
1. Check troubleshooting section above
2. Review DEPLOYMENT.md for configuration details
3. Check CHANGELOG.md for v3.0.0 changes
4. Review code in `api/handlers/consultations.py` and `api/handlers/providers.py`

---

**Status**: ✅ READY FOR VERIFICATION  
**Last Updated**: 2026-04-19  
**Version**: 3.0.0
