# MNEMOS Testing: Both MCP and Direct API
## Test plan for OpenClaw, ZeroClaw, Hermes, Claude (MCP + `/v1/chat/completions`)

**Status**: Extended test plan  
**Date**: 2026-04-19  
**Scope**: Validate MNEMOS via both MCP server and direct OpenAI-compatible API

---

## 🏗️ Architecture

### Two Integration Paths

#### Path 1: Direct API (OpenAI-compatible)
```
Agent → HTTP/REST → MNEMOS `/v1/chat/completions`
  ├─ OpenClaw: curl/httpx to endpoint
  ├─ ZeroClaw: task runner calling endpoint
  ├─ Hermes: inference backend config
  └─ Claude: urllib/httpx calls
```

#### Path 2: MCP Server (Model Context Protocol)
```
Agent → MCP Client → MNEMOS MCP Server → MNEMOS Backend
  ├─ OpenClaw: native MCP support
  ├─ ZeroClaw: task MCP integration
  ├─ Hermes: MCP capability discovery
  └─ Claude: MCP protocol client
```

---

## 📋 Dual Test Matrix

| Platform | Direct API | MCP | Success Criteria |
|----------|-----------|-----|------------------|
| **OpenClaw** | TC1.1–1.8 | MC1.1–1.5 | Both paths work equivalently |
| **ZeroClaw** | TC2.1–2.4 | MC2.1–2.4 | Both paths handle concurrency |
| **Hermes** | TC3.1–3.4 | MC3.1–3.4 | Both paths preserve reasoning |
| **Claude** | TC4.1–4.6 | MC4.1–4.4 | Both paths functional |

---

## 🧪 Part A: Direct API Tests (Existing)

See `AGENT_PLATFORM_TEST_PLAN.md` for complete test cases:
- TC1.1–1.8: OpenClaw
- TC2.1–2.4: ZeroClaw
- TC3.1–3.4: Hermes
- TC4.1–4.6: Claude Code

---

## 🧪 Part B: MCP Tests (New)

### MCP Server Setup

First, verify MNEMOS MCP server is available:

```bash
# Check if MNEMOS MCP is running
curl http://192.168.207.67:5002/mcp/info 2>/dev/null && echo "✓ MNEMOS MCP available" || echo "✗ MCP not available"

# Or via stdio (if MNEMOS has MCP stdio mode)
python -m mnemos.mcp --stdio < /dev/null 2>&1 | head -1
```

### MCP Test Suite 1: OpenClaw Agent (MCP)

#### MC1.1: Discover MCP Resources
```bash
openclaw agent --session-id test-mnemos-mcp -m "
  # OpenClaw discovers available MCP resources from MNEMOS
  openclaw capabilities --mcp-server mnemos
"
```
**Expected**: Lists available inference tools/resources

#### MC1.2: Call MCP Tool - Simple Inference
```bash
openclaw agent --session-id test-mnemos-mcp -m "
  # Use MNEMOS MCP resource for inference
  /mnemos:chat-completion \
    --model groq-llama \
    --prompt 'What is 2+2?' \
    --max-tokens 50
"
```
**Expected**: Response contains "4"

#### MC1.3: MCP Tool - Multi-Turn
```bash
openclaw agent --session-id test-mnemos-mcp -m "
  # Store conversation in MCP memory
  /mnemos:chat-session-create --session-id test-session
  
  # Turn 1
  /mnemos:chat-completion \
    --session test-session \
    --prompt 'My name is Alice.'
  
  # Turn 2
  /mnemos:chat-completion \
    --session test-session \
    --prompt 'What is my name?'
"
```
**Expected**: Turn 2 remembers Turn 1

#### MC1.4: MCP Tool - Memory Injection
```bash
openclaw agent --session-id test-mnemos-mcp -m "
  # Search MNEMOS memories via MCP
  /mnemos:memory-search --query 'analyst consensus patterns'
  
  # Use memories in inference
  /mnemos:chat-completion \
    --prompt 'Analyze holding based on memories' \
    --inject-memories true
"
```
**Expected**: Memories injected into prompt

#### MC1.5: MCP Tool - Cost Optimization
```bash
openclaw agent --session-id test-mnemos-mcp -m "
  # Request cost-optimized inference via MCP
  /mnemos:chat-completion \
    --model auto \
    --prompt 'Analyze portfolio' \
    --budget-cents 100 \
    --quality-floor 0.85
"
```
**Expected**: Model selected by optimizer, cost ≤ $1.00

---

### MCP Test Suite 2: ZeroClaw Runtime (MCP)

#### MC2.1: MCP Task Execution
```bash
zeroclaw run \
  --task "mcp-inference" \
  --use-mcp mnemos \
  --steps "
    Use MNEMOS MCP resource to:
    1. Query /mnemos:list-models
    2. Call /mnemos:chat-completion with test prompt
    3. Verify response structure
  " \
  --output /tmp/mcp_tc2_1.json
```
**Expected**: Task completes, response valid

#### MC2.2: MCP Parallel Requests
```bash
zeroclaw run \
  --task "mcp-parallel" \
  --use-mcp mnemos \
  --parallel-count 5 \
  --steps "
    Call /mnemos:chat-completion 5 times in parallel,
    track concurrency, verify all succeed
  " \
  --output /tmp/mcp_tc2_2.json
```
**Expected**: All 5 succeed, no rate limit errors

#### MC2.3: MCP Fallback Chain
```bash
zeroclaw run \
  --task "mcp-fallback" \
  --use-mcp "mnemos → cerberus" \
  --steps "
    Try MNEMOS MCP first,
    if unavailable, fall back to CERBERUS Ollama
  " \
  --output /tmp/mcp_tc2_3.json
```
**Expected**: Task succeeds via one path or the other

#### MC2.4: MCP Cost Aggregation
```bash
zeroclaw run \
  --task "mcp-costs" \
  --use-mcp mnemos \
  --track-costs \
  --steps "
    Call /mnemos:chat-completion multiple times,
    aggregate costs across calls,
    verify total matches expected
  " \
  --output /tmp/mcp_tc2_4.json
```
**Expected**: Cost report accurate, breakdown by model

---

### MCP Test Suite 3: Hermes Agent (MCP)

#### MC3.1: MCP-Powered Reasoning
```bash
hermes --goal "Solve a logic puzzle" \
  --mcp-server mnemos \
  --reasoning-backend mcp \
  --output /tmp/mcp_hermes_tc3_1.json
```
**Expected**: Hermes uses MCP for each reasoning step

#### MC3.2: MCP with Memory
```bash
hermes --goal "Design system architecture" \
  --mcp-server mnemos \
  --memory-source mcp \
  --output /tmp/mcp_hermes_tc3_2.json
```
**Expected**: Memories injected via MCP, citations valid

#### MC3.3: MCP Cost Tracking
```bash
hermes --goal "Explain machine learning" \
  --mcp-server mnemos \
  --track-tokens \
  --output /tmp/mcp_hermes_tc3_3.json
```
**Expected**: Token/cost data from MCP tools included

#### MC3.4: MCP Tool Discovery
```bash
hermes --mcp-server mnemos \
  --discover-tools \
  --output /tmp/mcp_hermes_tc3_4.json
```
**Expected**: Lists all MNEMOS MCP tools available

---

### MCP Test Suite 4: Claude Code (MCP)

#### MC4.1: MCP Client Connection
```python
import sys
sys.path.insert(0, '/path/to/claude')

from claude.mcp import MCPClient

client = MCPClient(server_url="http://192.168.207.67:5002/mcp")
tools = client.discover_tools()

assert "chat_completion" in [t.name for t in tools]
print(f"✓ MCP connected, {len(tools)} tools available")
```
**Expected**: Tools discovered

#### MC4.2: MCP Tool Call
```python
from claude.mcp import MCPClient

client = MCPClient(server_url="http://192.168.207.67:5002/mcp")

result = client.call_tool(
    "chat_completion",
    {
        "model": "groq-llama",
        "prompt": "Hello from MCP",
        "max_tokens": 50
    }
)

assert result["response"]
print(f"✓ MCP call successful: {result['response'][:50]}...")
```
**Expected**: Response returned via MCP

#### MC4.3: MCP Async Context
```python
import asyncio
from claude.mcp import AsyncMCPClient

async def test_mcp():
    async with AsyncMCPClient(server_url="http://192.168.207.67:5002/mcp") as client:
        result = await client.call_tool_async(
            "chat_completion",
            {
                "model": "groq-llama",
                "prompt": "Test async MCP",
                "max_tokens": 50
            }
        )
        return result["response"]

response = asyncio.run(test_mcp())
print(f"✓ Async MCP: {response[:50]}...")
```
**Expected**: Async calls work

#### MC4.4: MCP Error Handling
```python
from claude.mcp import MCPClient, MCPError

client = MCPClient(server_url="http://192.168.207.67:5002/mcp")

try:
    result = client.call_tool("invalid_tool", {})
except MCPError as e:
    assert e.code == "unknown_tool"
    print(f"✓ MCP error handling: {e.message}")
```
**Expected**: Clear error for invalid tool

---

## 📊 Comparison Matrix

After running both test suites, compare results:

| Metric | Direct API | MCP | Winner |
|--------|-----------|-----|--------|
| **Latency** | X ms | Y ms | [API/MCP] |
| **Throughput** | X req/s | Y req/s | [API/MCP] |
| **Error handling** | [qual] | [qual] | [API/MCP] |
| **Complexity** | [low/med/high] | [low/med/high] | [API/MCP] |
| **Cost tracking** | [accurate?] | [accurate?] | [API/MCP] |
| **Memory injection** | [supported?] | [supported?] | [API/MCP] |

---

## 🎯 Dual-Path Validation

### Equivalence Tests (All Platforms)

#### EQ1: Same Prompt → Same Output
```bash
# Run identical prompt via both API and MCP
PROMPT="Explain quantum computing in 2 sentences"

# API
API_RESULT=$(curl -s -X POST http://192.168.207.67:5002/v1/chat/completions \
  -H "Authorization: Bearer $MNEMOS_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\": \"groq-llama\", \"messages\": [{\"role\": \"user\", \"content\": \"$PROMPT\"}]}" \
  | jq -r '.choices[0].message.content')

# MCP
MCP_RESULT=$(agent-call /mnemos:chat-completion --prompt "$PROMPT" --model groq-llama)

# Compare
if [ "$API_RESULT" = "$MCP_RESULT" ]; then
  echo "✓ Outputs match"
else
  echo "⚠ Outputs differ (expected due to temperature/sampling)"
  echo "API:  ${API_RESULT:0:100}..."
  echo "MCP:  ${MCP_RESULT:0:100}..."
fi
```
**Expected**: Outputs similar (may not be identical due to sampling)

#### EQ2: Token Count Consistency
```bash
# Token counts should match between API and MCP
API_TOKENS=$(curl ... | jq '.usage.total_tokens')
MCP_TOKENS=$(agent-call /mnemos:chat-completion ... | jq '.metadata.tokens.total')

if [ "$API_TOKENS" = "$MCP_TOKENS" ]; then
  echo "✓ Token counts match"
else
  echo "⚠ Token counts differ: API=$API_TOKENS, MCP=$MCP_TOKENS"
fi
```
**Expected**: Token counts identical

#### EQ3: Model Resolution
```bash
# `model=auto` should select same model in both paths
API_MODEL=$(curl -s ... -d '{"model": "auto", ...}' | jq -r '.model')
MCP_MODEL=$(agent-call /mnemos:chat-completion --model auto | jq -r '.metadata.model')

echo "API selected: $API_MODEL"
echo "MCP selected: $MCP_MODEL"
```
**Expected**: Both select same model

---

## ✅ Success Criteria (Dual-Path)

### Individual Path Success
- ✅ **API Path**: All 23 test cases pass (TC1–TC4)
- ✅ **MCP Path**: All 18 test cases pass (MC1–MC4)

### Equivalence Success
- ✅ **Output quality**: Qualitatively similar (sampling expected)
- ✅ **Token counts**: Identical
- ✅ **Model selection**: Same models chosen by `auto`
- ✅ **Cost tracking**: Identical to $0.0001
- ✅ **Error handling**: Both paths handle errors gracefully

### Cross-Platform Success
- ✅ **OpenClaw**: Both API and MCP work
- ✅ **ZeroClaw**: Both paths handle concurrency
- ✅ **Hermes**: Both paths support reasoning
- ✅ **Claude**: Both paths functional

---

## 🚀 Execution Timeline

| Phase | Duration | What's Tested |
|-------|----------|---------------|
| **1a** | 2 hrs | Direct API (OpenClaw) |
| **1b** | 1.5 hrs | MCP (OpenClaw) |
| **2a** | 1.5 hrs | Direct API (ZeroClaw) |
| **2b** | 1.5 hrs | MCP (ZeroClaw) |
| **3a** | 1.5 hrs | Direct API (Hermes) |
| **3b** | 1.5 hrs | MCP (Hermes) |
| **4a** | 1 hr | Direct API (Claude) |
| **4b** | 1 hr | MCP (Claude) |
| **5** | 1.5 hrs | Equivalence + cross-platform |
| **Total** | **14 hours** | Complete validation |

*Can parallelize: 1a+1b, 2a+2b, etc. → ~8 hours total*

---

## 📝 Test Report Template (Dual-Path)

```markdown
# MNEMOS Dual-Path Test Report
**Date**: [date]
**Duration**: [hours]

## Executive Summary
- Direct API Tests: [pass/total]
- MCP Tests: [pass/total]
- Equivalence Tests: [pass/total]
- Overall: [GO/NO-GO]

## Platform Results

### OpenClaw
| Test Type | Status | Notes |
|-----------|--------|-------|
| Direct API | ✓ 8/8 | — |
| MCP | ✓ 5/5 | — |

### ZeroClaw
| Test Type | Status | Notes |
| Direct API | ✓ 4/4 | — |
| MCP | ✓ 4/4 | — |

[... Hermes, Claude ...]

## Equivalence Results
| Test | API | MCP | Match |
|------|-----|-----|-------|
| Output quality | ✓ | ✓ | Similar |
| Token counts | 450 | 450 | ✓ |
| Model selection | gpt-4o | gpt-4o | ✓ |
| Cost | $0.0045 | $0.0045 | ✓ |

## Performance Comparison
| Metric | Direct API | MCP | Recommendation |
|--------|-----------|-----|--------|
| Latency (p50) | 450ms | 480ms | API (slightly faster) |
| Throughput | 10 req/s | 9 req/s | API (higher) |
| Complexity | Low | Medium | API (simpler) |
| Features | ✓ Full | ✓ Full | Both capable |

## Recommendation
- **For simple inference**: Use Direct API (lower latency)
- **For tool discovery**: Use MCP (structured interface)
- **For flexibility**: Support both (agents can choose)

## Sign-Off
- Tested: [date]
- Status: [GO/NO-GO]
```

---

## 🎓 Key Findings

### Direct API Advantages
- Lower latency (no MCP overhead)
- Simpler implementation
- Fewer dependencies
- Works with any HTTP client

### MCP Advantages
- Structured tool discovery
- Automatic capability negotiation
- Better error reporting
- Native platform integration
- Future extensibility

### Recommendation
**Support both**:
- Agents use MCP for capability discovery
- Agents fall back to direct API if MCP unavailable
- Hermes/Claude use MCP for reasoning tasks
- ZeroClaw uses direct API for parallel execution
- OpenClaw supports both paths

---

**Status**: Extended test plan ready  
**Scope**: Both MCP and direct API paths  
**Next Step**: Execute Phase 1a (Direct API - OpenClaw)
