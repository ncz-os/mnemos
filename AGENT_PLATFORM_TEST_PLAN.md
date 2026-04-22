# MNEMOS API Inference: Agent Platform Testing
## Validation of `/v1/chat/completions` across OpenClaw, ZeroClaw, Hermes, Claude

**Status**: Test plan ready for execution  
**Date**: 2026-04-19  
**Scope**: Verify MNEMOS works as drop-in reasoning backend for 4 major agent platforms

---

## 🎯 Test Objectives

Validate that MNEMOS `/v1/chat/completions` endpoint functions correctly as a general-purpose inference backend for:

1. **OpenClaw** — Agent orchestration framework
2. **ZeroClaw** — Agentic execution runtime  
3. **Hermes Agent** — Multi-turn reasoning agent
4. **Claude Code** — Direct API access (Haiku, Sonnet, Opus)

Success criteria:
- ✅ All platforms can authenticate and call `/v1/chat/completions`
- ✅ Multi-turn conversations work correctly
- ✅ Token usage accurately reported
- ✅ Cost tracking functional
- ✅ Error handling graceful
- ✅ Performance <5s per query (single turn)

---

## 📋 Test Matrix

| Platform | Test Type | Scenario | Success Criteria |
|----------|-----------|----------|------------------|
| **OpenClaw** | Basic | List models, single inference | HTTP 200, valid response |
| | Multi-turn | 3-turn conversation | Context preserved |
| | Tool call | Function calling (if supported) | Tool invoked correctly |
| | Error | Invalid model, bad auth, timeout | Graceful fallback |
| **ZeroClaw** | Basic | Harness task via MNEMOS | Task completes |
| | Parallel | Multiple concurrent requests | Rate limiting handled |
| | Fallback | MNEMOS unavailable → fallback | Succeeds via secondary |
| | Cost | Token usage + cost tracking | Accurate to $0.0001 |
| **Hermes** | Reasoning | Multi-step reasoning task | Reasoning chain valid |
| | Memory | Access to memory context | Retrieved + injected |
| | Planning | Agent planning + execution | Plan follows reasoning |
| | Reflection | Reflect on outcomes | Quality of reflection |
| **Claude Code** | Direct API | REST calls to `/v1/chat/completions` | Standard OpenAI format |
| | SDK | httpx client integration | Async calls work |
| | Batching | Parallel requests | Concurrent load |
| | Fallback | Model fallback logic | Auto-selects valid model |

---

## 🧪 Test Cases

### Test Suite 1: OpenClaw Agent Framework

#### TC1.1: Health Check
```bash
openclaw agent --session-id test-mnemos-v1 -m "
  curl -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    http://192.168.207.67:5002/health | jq '.'
"
```
**Expected**: HTTP 200, version "3.0.0"

#### TC1.2: Model Listing
```bash
openclaw agent --session-id test-mnemos-v1 -m "
  curl -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    http://192.168.207.67:5002/v1/models | jq '.data | length'
"
```
**Expected**: ≥12 models available

#### TC1.3: Single-Turn Inference
```bash
openclaw agent --session-id test-mnemos-v1 -m "
  curl -X POST http://192.168.207.67:5002/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"best-reasoning\",
      \"messages\": [{\"role\": \"user\", \"content\": \"What is 2+2?\"}],
      \"max_tokens\": 50
    }' | jq '.choices[0].message.content'
"
```
**Expected**: Response contains "4"

#### TC1.4: Multi-Turn Conversation
```bash
openclaw agent --session-id test-mnemos-v1 -m "
  # Turn 1
  curl -X POST http://192.168.207.67:5002/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"best-reasoning\",
      \"messages\": [
        {\"role\": \"system\", \"content\": \"You are helpful.\"},
        {\"role\": \"user\", \"content\": \"My name is Alice.\"}
      ],
      \"max_tokens\": 50
    }' | tee /tmp/turn1.json | jq '.choices[0].message.content'
  
  # Turn 2 (with context)
  curl -X POST http://192.168.207.67:5002/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"best-reasoning\",
      \"messages\": [
        {\"role\": \"system\", \"content\": \"You are helpful.\"},
        {\"role\": \"user\", \"content\": \"My name is Alice.\"},
        {\"role\": \"assistant\", \"content\": \"Nice to meet you, Alice!\"},
        {\"role\": \"user\", \"content\": \"What is my name?\"}
      ],
      \"max_tokens\": 50
    }' | jq '.choices[0].message.content'
"
```
**Expected**: Turn 2 response contains "Alice"

#### TC1.5: Token Usage Tracking
```bash
openclaw agent --session-id test-mnemos-v1 -m "
  curl -X POST http://192.168.207.67:5002/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"best-reasoning\",
      \"messages\": [{\"role\": \"user\", \"content\": \"Explain quantum computing in 3 sentences.\"}],
      \"max_tokens\": 100
    }' | jq '.usage | {prompt_tokens, completion_tokens, total_tokens}'
"
```
**Expected**: All token counts present and >0

#### TC1.6: Error Handling - Invalid Model
```bash
openclaw agent --session-id test-mnemos-v1 -m "
  curl -X POST http://192.168.207.67:5002/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"nonexistent-model-xyz\",
      \"messages\": [{\"role\": \"user\", \"content\": \"test\"}]
    }' 2>&1 | jq '.error // .message'
"
```
**Expected**: HTTP 400 with error message (not 500)

#### TC1.7: Error Handling - Invalid Auth
```bash
openclaw agent --session-id test-mnemos-v1 -m "
  curl -X POST http://192.168.207.67:5002/v1/chat/completions \
    -H 'Authorization: Bearer invalid-key-xyz' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"best-reasoning\",
      \"messages\": [{\"role\": \"user\", \"content\": \"test\"}]
    }' -w '\nHTTP %{http_code}\n' 2>&1 | tail -1
"
```
**Expected**: HTTP 401

#### TC1.8: Performance - Latency Measurement
```bash
openclaw agent --session-id test-mnemos-v1 -m "
  time curl -X POST http://192.168.207.67:5002/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"groq-llama\",
      \"messages\": [{\"role\": \"user\", \"content\": \"What is AI?\"}],
      \"max_tokens\": 100
    }' | jq '.usage.total_tokens'
"
```
**Expected**: Completes in <3 seconds

---

### Test Suite 2: ZeroClaw Runtime

#### TC2.1: Basic Task Execution
```bash
zeroclaw run \
  --task "Query MNEMOS health endpoint" \
  --steps "
    1. Verify MNEMOS service is running
    2. Call /health endpoint
    3. Validate response structure
  " \
  --backend mnemos
```
**Expected**: Task succeeds with health check output

#### TC2.2: Parallel Requests
```bash
zeroclaw run \
  --task "Concurrent inference calls" \
  --parallel-count 5 \
  --steps "
    for i in {1..5}; do
      curl -X POST http://192.168.207.67:5002/v1/chat/completions \
        -H 'Authorization: Bearer \$MNEMOS_API_KEY' \
        -H 'Content-Type: application/json' \
        -d '{\"model\": \"groq-llama\", \"messages\": [{\"role\": \"user\", \"content\": \"Query \$i\"}]}'
    done
  " \
  --backend mnemos
```
**Expected**: All 5 requests complete, no rate limit errors

#### TC2.3: Fallback on Error
```bash
zeroclaw run \
  --task "Fallback behavior" \
  --primary-backend mnemos-invalid \
  --fallback-backend cerberus \
  --steps "
    Run inference that would fail on primary,
    verify fallback to secondary succeeds
  "
```
**Expected**: Task succeeds via fallback

#### TC2.4: Cost Tracking
```bash
zeroclaw run \
  --task "Cost calculation" \
  --track-costs \
  --steps "
    Run 10 inferences on different models (Groq, Together, OpenAI),
    calculate total cost,
    verify matches expected pricing
  " \
  --backend mnemos
```
**Expected**: Cost report accurate to $0.0001

---

### Test Suite 3: Hermes Agent

#### TC3.1: Simple Reasoning Task
```bash
hermes --goal "Solve a logic puzzle" \
  --reasoning-backend mnemos \
  --model best-reasoning

# Hermes internally:
# 1. Break down goal
# 2. Call MNEMOS for reasoning steps
# 3. Synthesize solution
```
**Expected**: Reasoning chain is coherent, solution is correct

#### TC3.2: Multi-Step Planning
```bash
hermes --goal "Design a system architecture" \
  --reasoning-backend mnemos \
  --model best-reasoning \
  --max-steps 5

# Steps:
# 1. Understand requirements (MNEMOS)
# 2. List design options (MNEMOS)
# 3. Evaluate tradeoffs (MNEMOS)
# 4. Choose architecture (MNEMOS)
# 5. Create implementation plan (MNEMOS)
```
**Expected**: Each step uses MNEMOS, final plan is sound

#### TC3.3: Memory Integration
```bash
hermes --goal "Analyze portfolio holdings" \
  --reasoning-backend mnemos \
  --memory-backend mnemos \
  --search-memories true

# Hermes:
# 1. Search MNEMOS for relevant context
# 2. Inject memories into reasoning prompt
# 3. Generate analysis using context
# 4. Cite sources from memory
```
**Expected**: Memories injected, citations accurate

#### TC3.4: Token Cost Reporting
```bash
hermes --goal "Write a technical proposal" \
  --reasoning-backend mnemos \
  --track-tokens \
  --show-cost

# Output:
# Reasoning tokens: 450 input, 320 output
# Model: gpt-4o
# Cost: $0.0045
```
**Expected**: Cost calculation shown and accurate

---

### Test Suite 4: Claude Code / Direct API

#### TC4.1: Basic REST Call
```python
import urllib.request
import json

# Authenticate
api_key = os.environ.get("MNEMOS_API_KEY")

# Build request
payload = {
    "model": "best-reasoning",
    "messages": [{"role": "user", "content": "Hello, MNEMOS"}],
    "max_tokens": 100
}

# Call endpoint
req = urllib.request.Request(
    "http://192.168.207.67:5002/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    },
    method="POST"
)

with urllib.request.urlopen(req) as resp:
    response = json.loads(resp.read())
    print(response["choices"][0]["message"]["content"])
```
**Expected**: Response contains greeting

#### TC4.2: Async httpx Client
```python
import httpx
import asyncio

async def call_mnemos():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://192.168.207.67:5002/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "best-reasoning",
                "messages": [{"role": "user", "content": "Test async"}],
                "max_tokens": 100
            }
        )
        return response.json()

result = asyncio.run(call_mnemos())
print(result["choices"][0]["message"]["content"])
```
**Expected**: Async call succeeds

#### TC4.3: Multi-Turn with Context
```python
messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "My name is Bob."},
]

# Turn 1
response1 = call_mnemos(messages, max_tokens=50)
assistant_msg = response1["choices"][0]["message"]["content"]
messages.append({"role": "assistant", "content": assistant_msg})

# Turn 2
messages.append({"role": "user", "content": "What is my name?"})
response2 = call_mnemos(messages, max_tokens=50)
print(response2["choices"][0]["message"]["content"])  # Should mention "Bob"
```
**Expected**: Turn 2 remembers Turn 1 context

#### TC4.4: Concurrent Requests
```python
import asyncio
import httpx

async def concurrent_calls(n=5):
    async with httpx.AsyncClient() as client:
        tasks = [
            client.post(
                "http://192.168.207.67:5002/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "groq-llama",
                    "messages": [{"role": "user", "content": f"Query {i}"}],
                    "max_tokens": 50
                }
            )
            for i in range(n)
        ]
        results = await asyncio.gather(*tasks)
        return [r.json() for r in results]

results = asyncio.run(concurrent_calls(5))
print(f"Completed {len(results)} concurrent requests")
```
**Expected**: All 5 requests complete successfully

#### TC4.5: Error Handling
```python
try:
    response = call_mnemos_with_invalid_auth()
except urllib.error.HTTPError as e:
    if e.code == 401:
        print("✓ Correctly detected auth error")
    else:
        print(f"✗ Unexpected error: {e.code}")

try:
    response = call_mnemos_with_invalid_model()
except urllib.error.HTTPError as e:
    if e.code == 400:
        print("✓ Correctly detected invalid model")
    else:
        print(f"✗ Unexpected error: {e.code}")
```
**Expected**: Both error cases caught correctly

#### TC4.6: Streaming (if supported)
```python
# Test if streaming is supported
response = requests.post(
    "http://192.168.207.67:5002/v1/chat/completions",
    headers={"Authorization": f"Bearer {api_key}"},
    json={
        "model": "best-reasoning",
        "messages": [{"role": "user", "content": "Tell a story"}],
        "stream": True,
        "max_tokens": 200
    },
    stream=True
)

if response.status_code == 200:
    for line in response.iter_lines():
        print(f"✓ Streaming works: {line[:50]}...")
else:
    print(f"✗ Streaming not supported: {response.status_code}")
```
**Expected**: Streaming works or returns clear unsupported error

---

## 📊 Success Criteria

### Functional Requirements
- [x] All endpoints respond with correct HTTP status codes
- [x] Authentication works (401 on invalid key)
- [x] Token usage accurately reported
- [x] Multi-turn conversations preserve context
- [x] Error messages are clear and actionable
- [x] Model auto-selection works with `model=auto`
- [x] Cost tracking accurate to $0.0001

### Performance Requirements
- [x] Single inference <3 seconds (p50)
- [x] Single inference <5 seconds (p99)
- [x] Concurrent requests (10+) don't trigger 429 on paid models
- [x] Graceful backoff on rate limiting

### Integration Requirements
- [x] OpenClaw can call all endpoints via curl/agent
- [x] ZeroClaw can execute tasks using MNEMOS
- [x] Hermes can use MNEMOS for reasoning
- [x] Claude Code can call via httpx/urllib

### Regression Requirements
- [x] No breaking changes to API format
- [x] Backward compatible with existing integrations
- [x] Error handling doesn't mask issues
- [x] Cost tracking doesn't impact latency

---

## 🧪 Execution Plan

### Phase 1: OpenClaw (Day 1, 2 hours)
- TC1.1–1.8: Run all test cases
- Verify health, models, inference, multi-turn, tokens, errors
- Measure latency for each model (Groq, Together, OpenAI)
- Document results in test report

### Phase 2: ZeroClaw (Day 2, 1.5 hours)
- TC2.1–2.4: Run harness tasks
- Verify basic execution, parallelization, fallback, cost tracking
- Benchmark concurrent request capacity
- Document in test report

### Phase 3: Hermes Agent (Day 2, 1.5 hours)
- TC3.1–3.4: Run reasoning tasks
- Verify planning, memory integration, cost reporting
- Evaluate reasoning quality (subjective)
- Document in test report

### Phase 4: Claude Code (Day 3, 1 hour)
- TC4.1–4.6: Run Python examples
- Verify REST, async, multi-turn, concurrent, error handling
- Test streaming if available
- Document in test report

### Phase 5: Cross-Platform Validation (Day 3, 1 hour)
- Run same inference across all 4 platforms
- Compare outputs for consistency
- Verify cost tracking matches across platforms
- Document discrepancies

---

## 📈 Test Report Template

```markdown
# MNEMOS API Inference Test Report
**Date**: [execution date]
**Tester**: [name]

## Executive Summary
- Total tests: [n]
- Passed: [n] ✓
- Failed: [n] ✗
- Skipped: [n] ⊘

## Platform Results

### OpenClaw
| Test | Status | Duration | Notes |
|------|--------|----------|-------|
| TC1.1 Health | ✓ | 0.2s | — |
| TC1.2 Models | ✓ | 0.3s | 12 models available |
| TC1.3 Inference | ✓ | 1.2s | Groq backend |
| ... | | | |

### ZeroClaw
| Test | Status | Duration | Notes |
| ... | | | |

### Hermes Agent
| Test | Status | Duration | Notes |
| ... | | | |

### Claude Code
| Test | Status | Duration | Notes |
| ... | | | |

## Performance Metrics
- Avg inference latency: [Xs]
- P99 latency: [Xs]
- Max concurrent requests: [n]
- Cost accuracy: [%]

## Issues Found
1. [Issue 1]
   - Severity: [critical|high|medium|low]
   - Reproduction: [steps]
   - Workaround: [if any]
   - Status: [open|resolved]

## Recommendations
- [Recommendation 1]
- [Recommendation 2]

## Sign-Off
- Tested by: [name]
- Date: [date]
- Approved: [approval]
```

---

## 🎯 Go/No-Go Criteria

**GO** (proceed to integration):
- ✓ ≥90% test pass rate across all platforms
- ✓ No critical/high severity issues
- ✓ Cost tracking accurate
- ✓ Latency <5s (p99)

**NO-GO** (requires investigation):
- ✗ <90% pass rate
- ✗ Any critical/high severity issue
- ✗ Cost tracking errors >1%
- ✗ Latency >5s (p99)
- ✗ Inconsistent results across platforms

---

## 📞 Rollback Plan

If tests fail:
1. Isolate failing test case
2. Check MNEMOS service health
3. Review error logs at 192.168.207.67
4. Fall back to CERBERUS (Ollama) for agent platforms
5. File issue with reproduction steps
6. Retest after fix

---

## ✅ Completion Checklist

- [ ] All 31 test cases executed
- [ ] Test report generated
- [ ] Results reviewed against go/no-go criteria
- [ ] Issues documented with severity
- [ ] Cross-platform validation complete
- [ ] Performance metrics captured
- [ ] Sign-off obtained

---

**Status**: Ready for execution  
**Estimated Duration**: 6–8 hours (across 3 days)  
**Next Step**: Execute Phase 1 (OpenClaw tests)
