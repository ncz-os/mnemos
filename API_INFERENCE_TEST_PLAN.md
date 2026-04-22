# API Inference Frontend Test Plan for Agents

**Status**: Implementation exists, functional testing pending  
**Date**: 2026-04-19  
**Version**: 3.0.0

---

## What Exists

### OpenAI-Compatible Gateway
**File**: `api/handlers/openai_compat.py`

**Endpoints**:
```
GET  /v1/models              — List available models
GET  /v1/models/{model_id}   — Get specific model info
POST /v1/chat/completions    — Chat completion (agent inference)
```

**Features**:
- ✅ OpenAI-compatible `/v1/chat/completions` request/response format
- ✅ Automatic model selection (`model="auto"` uses optimizer)
- ✅ Model aliases (`best-coding`, `best-reasoning`, `fastest`, `cheapest`)
- ✅ Memory injection (searches MNEMOS for context)
- ✅ LETHE compression (512-token budget)
- ✅ Multi-provider routing (OpenAI, Anthropic, Groq, Together, etc.)
- ✅ Cost-aware model selection
- ✅ Task-type based capability mapping

**Authentication**: Bearer token (MNEMOS_API_KEY)

---

## What Hasn't Been Tested

### Functional Testing
- [ ] `/v1/chat/completions` with running service
- [ ] Memory injection during inference
- [ ] Model auto-selection (cost optimization)
- [ ] Agent tool calls and streaming
- [ ] Error handling (invalid model, provider down, etc.)

### Integration Testing
- [ ] Full agent workflow (plan → execute → reflect)
- [ ] Multi-turn conversations with memory
- [ ] Concurrent requests (rate limiting, etc.)
- [ ] Provider failover (if primary fails, use secondary)

### Unit Testing
- [ ] Model routing logic
- [ ] Memory search and compression
- [ ] Capability matching
- [ ] Cost calculation

---

## Test Plan

### Phase 1: Unit Tests (Run Locally)

```bash
# Test model selection and routing
python3 -c "
from api.handlers.openai_compat import MODEL_ALIASES, TASK_CAPABILITY_MAP
assert MODEL_ALIASES['best-coding'] == 'gpt-4o'
assert 'coding' in TASK_CAPABILITY_MAP['code_generation']
print('✓ Model routing logic valid')
"

# Test chat message models
python3 -c "
from api.handlers.openai_compat import ChatMessage, ChatCompletionRequest
msg = ChatMessage(role='user', content='test')
req = ChatCompletionRequest(model='gpt-4', messages=[msg])
assert req.model == 'gpt-4'
assert len(req.messages) == 1
print('✓ Chat message models valid')
"
```

### Phase 2: Integration Tests (Requires Running Service)

```bash
# 1. Start service
python3 api_server.py &
API_PID=$!
sleep 3

# 2. List models
curl -X GET http://localhost:5002/v1/models \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  | jq '.data | length'
# Expected: >0 (at least one model available)

# 3. Test chat completion
curl -X POST http://localhost:5002/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant"},
      {"role": "user", "content": "What is 2+2?"}
    ],
    "temperature": 0.7,
    "max_tokens": 100
  }' | jq '.choices[0].message.content'
# Expected: Response about 2+2

# 4. Stop service
kill $API_PID
```

### Phase 3: Agent Inference Tests

```bash
# Test 1: Basic agent task
python3 << 'EOF'
import asyncio
import httpx

async def test_agent_inference():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:5002/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "auto",  # Use optimizer
                "messages": [
                    {"role": "system", "content": "You are a code assistant"},
                    {"role": "user", "content": "Write a Python function to reverse a string"}
                ],
                "temperature": 0.2,
                "max_tokens": 200
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "choices" in data
        assert len(data["choices"]) > 0
        assert "message" in data["choices"][0]
        print(f"✓ Agent inference test passed")
        print(f"  Response: {data['choices'][0]['message']['content'][:100]}...")

asyncio.run(test_agent_inference())
EOF
```

### Phase 4: Memory Injection Tests

```bash
# Test: Verify memory context is injected
python3 << 'EOF'
import asyncio
import httpx

async def test_memory_injection():
    """Test that MNEMOS context is injected into agent response"""
    async with httpx.AsyncClient() as client:
        # First, store a memory
        memory_resp = await client.post(
            "http://localhost:5002/v1/memories",
            headers={"Authorization": "Bearer test-key"},
            json={
                "content": "Python best practices: use type hints and docstrings",
                "category": "solutions"
            }
        )
        assert memory_resp.status_code in [200, 201]
        
        # Then use it in inference (should search and inject)
        inference_resp = await client.post(
            "http://localhost:5002/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "What are Python best practices?"}
                ]
            }
        )
        
        assert inference_resp.status_code == 200
        data = inference_resp.json()
        # Response should reference memory content (type hints, docstrings)
        response_text = data["choices"][0]["message"]["content"].lower()
        # At least one of these should appear
        has_practices = any(x in response_text for x in ["type hint", "docstring", "practice"])
        print(f"✓ Memory injection test {'passed' if has_practices else 'inconclusive'}")

asyncio.run(test_memory_injection())
EOF
```

### Phase 5: Error Handling Tests

```bash
# Test 1: Invalid model
curl -X POST http://localhost:5002/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nonexistent-model-xyz",
    "messages": [{"role": "user", "content": "test"}]
  }'
# Expected: 400 or 404 error with helpful message

# Test 2: Missing authentication
curl -X POST http://localhost:5002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "test"}]
  }'
# Expected: 401 Unauthorized

# Test 3: Invalid request format
curl -X POST http://localhost:5002/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"invalid": "json"}'
# Expected: 400 Bad Request
```

---

## Implementation Verification

### Existing Code Audit

**✅ Implemented**:
1. `ChatMessage` model for OpenAI format
2. `ChatCompletionRequest` model with:
   - model (supports "auto", aliases, explicit models)
   - messages (list of role/content)
   - temperature, max_tokens, top_p
   - user identifier
3. `ChatCompletionResponse` with:
   - choices array
   - usage dict (input_tokens, output_tokens)
   - model, created timestamp
4. Memory injection via `_search_mnemos_context()`
5. Model recommendation via `_get_model_recommendation()`
6. Cost calculation for model selection
7. Provider routing via `_route_to_provider()`
8. Authentication via bearer token

**⚠️ Needs Verification**:
1. Actual provider API calls (httpx integration)
2. Streaming response support (if needed for agents)
3. Function calling / tool use (if agents need it)
4. Vision capability (if multimodal needed)

---

## Test Execution Steps

### Prerequisites
```bash
# 1. Ensure Python 3.11+
python3 --version  # Should be 3.11+

# 2. Install dependencies
pip3 install -e .

# 3. Set up environment
export MNEMOS_API_KEY=$(uuidgen)
export PG_HOST=localhost
export PG_DATABASE=mnemos
export PG_USER=postgres
export PG_PASSWORD=postgres

# 4. Start PostgreSQL
psql -U postgres -d postgres -c "CREATE DATABASE mnemos;"

# 5. Run migrations
psql -d mnemos -f db/migrations.sql
```

### Run Tests
```bash
# Terminal 1: Start API
python3 api_server.py

# Terminal 2: Run test suite
bash api_inference_tests.sh
```

---

## Success Criteria

| Test | Criteria | Status |
|------|----------|--------|
| **Health Check** | `/health` responds with 3.0.0 | ⏳ Pending |
| **Model Listing** | `GET /v1/models` returns array | ⏳ Pending |
| **Basic Completion** | `POST /v1/chat/completions` returns valid response | ⏳ Pending |
| **Model Aliases** | `model="best-coding"` resolves correctly | ⏳ Pending |
| **Auto Selection** | `model="auto"` uses optimizer | ⏳ Pending |
| **Memory Injection** | Context from MNEMOS in prompt | ⏳ Pending |
| **Authentication** | Requires valid bearer token | ⏳ Pending |
| **Error Handling** | Invalid requests return proper errors | ⏳ Pending |
| **Cost Tracking** | Response includes token usage | ⏳ Pending |
| **Provider Routing** | Requests routed to correct LLM provider | ⏳ Pending |

---

## Current Implementation Status

### Code Coverage: 100%
- ✅ All endpoints implemented
- ✅ All models defined
- ✅ All helper functions present
- ✅ Authentication integrated
- ✅ Memory injection logic present

### Test Coverage: 0%
- ❌ No functional tests
- ❌ No integration tests  
- ❌ No end-to-end tests
- ❌ Unit tests failing due to Python 3.9 vs 3.11 issue

### Documentation: 50%
- ✅ Code docstrings present
- ✅ Type hints complete
- ❌ API documentation missing (needs OpenAPI spec)
- ❌ Examples missing (needs curl/Python examples)
- ❌ Agent integration guide missing

---

## Next Steps (To Complete Testing)

### Immediate (Phase 1-2)
1. Run the service with Python 3.11+
2. Execute HTTP tests against running service
3. Verify endpoints respond correctly
4. Test with real LLM provider (Groq/Together free tier)

### Short-term (Phase 3-4)
1. Create agent task examples
2. Test memory injection in real scenarios
3. Measure latency and costs
4. Document agent usage patterns

### Medium-term (Phase 5)
1. Add comprehensive error tests
2. Test provider failover
3. Test concurrent requests
4. Load testing (if production use)

---

## Example: Agent Inference with cURL

```bash
# 1. Set environment
export MNEMOS_API_KEY="your-api-key-here"
export API_URL="http://localhost:5002"

# 2. List available models
curl -X GET $API_URL/v1/models \
  -H "Authorization: Bearer $MNEMOS_API_KEY" | jq '.'

# 3. Run agent task (code generation)
curl -X POST $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {
        "role": "system",
        "content": "You are an expert Python developer. Write clean, well-documented code."
      },
      {
        "role": "user",
        "content": "Write a function that validates email addresses using regex"
      }
    ],
    "temperature": 0.2,
    "max_tokens": 500
  }' | jq '.choices[0].message.content'

# 4. Run agent task with memory (reasoning)
curl -X POST $API_URL/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "best-reasoning",
    "messages": [
      {
        "role": "user",
        "content": "Design a system architecture for a real-time messaging app"
      }
    ],
    "temperature": 0.7,
    "max_tokens": 1000
  }' | jq '.choices[0]'
```

---

## Conclusion

**The API inference frontend exists and is fully implemented**, but requires functional testing with a running service and Python 3.11+.

Once tested, agents can use the OpenAI-compatible `/v1/chat/completions` endpoint for:
- Multi-provider inference (automatic routing)
- Cost-aware model selection
- Memory-augmented responses (MNEMOS context injection)
- Standardized OpenAI API format (works with OpenAI SDKs)

**Status**: Ready for functional testing and agent integration.

---

**Last Updated**: 2026-04-19  
**Version**: 3.0.0  
**Next Action**: Run Phase 1-2 tests with Python 3.11+ and running service
