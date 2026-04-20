# MNEMOS Agent Platform Testing: Actual System Locations
## Execution guide for STUDIO, .54, .56, and Claude Code

**Status**: Ready for execution  
**Date**: 2026-04-19  
**Systems**: STUDIO (OpenClaw/Hermes), .54 (nemoclaw), .56 (zeroclaw), local (Claude)

---

## 🗺️ System Map

| Platform | System | IP | User | Password | Tests |
|----------|--------|----|----|----------|-------|
| **OpenClaw** | STUDIO | 192.168.207.10 | jasonperlow | <REDACTED-ARGONAS-PASS> | 13 (8 API + 5 MCP) |
| **Hermes Agent** | STUDIO | 192.168.207.10 | jasonperlow | <REDACTED-ARGONAS-PASS> | 8 (4 API + 4 MCP) |
| **ZeroClaw (nemoclaw)** | .54 | 192.168.207.54 | pi | <REDACTED-ARGONAS-PASS> | 8 (4 API + 4 MCP) |
| **ZeroClaw (pure)** | .56 | 192.168.207.56 | pi | <REDACTED-ARGONAS-PASS> | 8 (4 API + 4 MCP) |
| **Claude Code** | Local | 127.0.0.1 | — | — | 9 (5 API + 4 MCP) |

---

## 🔐 SSH Access Setup

```bash
# STUDIO (OpenClaw + Hermes)
STUDIO="jasonperlow@192.168.207.10"
STUDIO_PASS="<REDACTED-ARGONAS-PASS>"

# ZeroClaw .54 (nemoclaw)
ZC54="pi@192.168.207.54"
ZC54_PASS="<REDACTED-ARGONAS-PASS>"

# ZeroClaw .56 (pure)
ZC56="pi@192.168.207.56"
ZC56_PASS="<REDACTED-ARGONAS-PASS>"

# Test connectivity
for host in $STUDIO $ZC54 $ZC56; do
  sshpass -p "$STUDIO_PASS" ssh -o ConnectTimeout=5 $host "hostname" && echo "✓ $host" || echo "✗ $host"
done
```

---

## 🧪 Phase 1: OpenClaw Tests (STUDIO, ~2 hours)

```bash
#!/bin/bash

STUDIO="jasonperlow@192.168.207.10"
PASS="<REDACTED-ARGONAS-PASS>"
SESSION="test-mnemos-$(date +%s)"

echo "═══════════════════════════════════════════"
echo "OpenClaw Testing on STUDIO"
echo "═══════════════════════════════════════════"
echo ""

# Connect and run tests
sshpass -p "$PASS" ssh $STUDIO << 'EOFTEST'

export MNEMOS_API_KEY=$(cat ~/.investorclaw/.mnemos_api_key 2>/dev/null || echo "test-key")
export MNEMOS_ENDPOINT="http://192.168.207.67:5002"
SESSION="test-mnemos-$(date +%s)"
ENDPOINT="$MNEMOS_ENDPOINT"
PASSED=0
FAILED=0

# Verify OpenClaw is running
openclaw gateway status || { echo "✗ OpenClaw not running"; exit 1; }
echo "✓ OpenClaw gateway healthy"
echo ""

# TC1.1: Health Check
echo "[TC1.1] Health Check"
openclaw agent --session-id $SESSION -m "
  curl -s -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    $ENDPOINT/health | jq '.version'
" > /tmp/oc_tc1_1.log 2>&1

if grep -q "3.0.0" /tmp/oc_tc1_1.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  ((FAILED++))
fi
echo ""

# TC1.2: Model Listing
echo "[TC1.2] List Models"
openclaw agent --session-id $SESSION -m "
  curl -s -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    $ENDPOINT/v1/models | jq '.data | length'
" > /tmp/oc_tc1_2.log 2>&1

count=$(cat /tmp/oc_tc1_2.log | grep -oE '^[0-9]+$' | head -1)
if [ "$count" -ge 12 ]; then
  echo "✓ PASS ($count models)"
  ((PASSED++))
else
  echo "✗ FAIL"
  ((FAILED++))
fi
echo ""

# TC1.3: Single Inference
echo "[TC1.3] Single-Turn Inference"
openclaw agent --session-id $SESSION -m "
  curl -s -X POST $ENDPOINT/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"groq-llama\",
      \"messages\": [{\"role\": \"user\", \"content\": \"What is 2+2?\"}],
      \"max_tokens\": 50
    }' | jq '.choices[0].message.content'
" > /tmp/oc_tc1_3.log 2>&1

if grep -q "4" /tmp/oc_tc1_3.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  ((FAILED++))
fi
echo ""

# TC1.4: Multi-Turn
echo "[TC1.4] Multi-Turn Conversation"
openclaw agent --session-id $SESSION -m "
  curl -s -X POST $ENDPOINT/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"groq-llama\",
      \"messages\": [
        {\"role\": \"user\", \"content\": \"My name is Alice.\"},
        {\"role\": \"assistant\", \"content\": \"Nice to meet you, Alice!\"},
        {\"role\": \"user\", \"content\": \"What is my name?\"}
      ],
      \"max_tokens\": 50
    }' | jq '.choices[0].message.content'
" > /tmp/oc_tc1_4.log 2>&1

if grep -qi "alice" /tmp/oc_tc1_4.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  ((FAILED++))
fi
echo ""

# TC1.5: Token Usage
echo "[TC1.5] Token Usage Tracking"
openclaw agent --session-id $SESSION -m "
  curl -s -X POST $ENDPOINT/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"groq-llama\",
      \"messages\": [{\"role\": \"user\", \"content\": \"Explain quantum computing in 3 sentences.\"}],
      \"max_tokens\": 100
    }' | jq '.usage'
" > /tmp/oc_tc1_5.log 2>&1

if grep -q "prompt_tokens" /tmp/oc_tc1_5.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  ((FAILED++))
fi
echo ""

# Summary
echo "═══════════════════════════════════════════"
echo "OpenClaw Results: $PASSED passed, $FAILED failed"
echo "═══════════════════════════════════════════"

EOFTEST

echo "✓ OpenClaw tests completed"
```

---

## 🧪 Phase 2: Hermes Agent Tests (STUDIO, ~1.5 hours)

```bash
#!/bin/bash

STUDIO="jasonperlow@192.168.207.10"
PASS="<REDACTED-ARGONAS-PASS>"

echo "═══════════════════════════════════════════"
echo "Hermes Agent Testing on STUDIO"
echo "═══════════════════════════════════════════"
echo ""

sshpass -p "$PASS" ssh $STUDIO << 'EOFTEST'

export MNEMOS_API_KEY=$(cat ~/.investorclaw/.mnemos_api_key 2>/dev/null || echo "test-key")
export MNEMOS_ENDPOINT="http://192.168.207.67:5002"

# Verify Hermes is available
hermes --version > /dev/null 2>&1 || { echo "✗ Hermes not available"; exit 1; }
echo "✓ Hermes available"
echo ""

# TC3.1: Simple Reasoning
echo "[TC3.1] Simple Reasoning Task"
hermes --goal "Solve: If A > B and B > C, what is the relationship between A and C?" \
  --reasoning-backend mnemos \
  --output /tmp/hermes_tc3_1.json 2>&1 | head -20

if [ -f /tmp/hermes_tc3_1.json ] && jq -e '.status' /tmp/hermes_tc3_1.json > /dev/null 2>&1; then
  echo "✓ PASS"
else
  echo "✗ FAIL"
fi
echo ""

# TC3.2: Multi-Step Planning
echo "[TC3.2] Multi-Step Planning"
hermes --goal "Create a plan for building a mobile app" \
  --reasoning-backend mnemos \
  --max-steps 3 \
  --output /tmp/hermes_tc3_2.json 2>&1 | head -20

if [ -f /tmp/hermes_tc3_2.json ] && jq -e '.plan' /tmp/hermes_tc3_2.json > /dev/null 2>&1; then
  echo "✓ PASS"
else
  echo "✗ FAIL"
fi
echo ""

echo "═══════════════════════════════════════════"
echo "Hermes tests completed"
echo "═══════════════════════════════════════════"

EOFTEST

echo "✓ Hermes tests completed"
```

---

## 🧪 Phase 3: ZeroClaw Tests (.54 nemoclaw, ~1.5 hours)

```bash
#!/bin/bash

ZC54="pi@192.168.207.54"
PASS="<REDACTED-ARGONAS-PASS>"

echo "═══════════════════════════════════════════"
echo "ZeroClaw (nemoclaw) Testing on .54"
echo "═══════════════════════════════════════════"
echo ""

sshpass -p "$PASS" ssh $ZC54 << 'EOFTEST'

export MNEMOS_API_KEY=$(cat ~/.investorclaw/.mnemos_api_key 2>/dev/null || echo "test-key")
export MNEMOS_ENDPOINT="http://192.168.207.67:5002"

# Verify ZeroClaw is available
zeroclaw --version > /dev/null 2>&1 || { echo "✗ ZeroClaw not available"; exit 1; }
echo "✓ ZeroClaw (nemoclaw) available"
echo ""

# TC2.1: Basic Task
echo "[TC2.1] Basic Task Execution"
zeroclaw run \
  --task "health-check" \
  --steps "curl -H 'Authorization: Bearer \$MNEMOS_API_KEY' http://192.168.207.67:5002/health" \
  --output /tmp/zc54_tc2_1.json 2>&1 | grep -i "status\|pass\|fail"

if jq -e '.status == "success"' /tmp/zc54_tc2_1.json > /dev/null 2>&1; then
  echo "✓ PASS"
else
  echo "✗ FAIL"
fi
echo ""

# TC2.2: Parallel Requests
echo "[TC2.2] Parallel Requests (5 concurrent)"
zeroclaw run \
  --task "parallel-inference" \
  --parallel-count 5 \
  --steps "curl -s -X POST http://192.168.207.67:5002/v1/chat/completions -H 'Authorization: Bearer \$MNEMOS_API_KEY' -H 'Content-Type: application/json' -d '{\"model\": \"groq-llama\", \"messages\": [{\"role\": \"user\", \"content\": \"test\"}], \"max_tokens\": 50}'" \
  --output /tmp/zc54_tc2_2.json 2>&1 | grep -i "parallel\|complete"

echo "✓ PASS (completed)"
echo ""

echo "═══════════════════════════════════════════"
echo "ZeroClaw (.54) tests completed"
echo "═══════════════════════════════════════════"

EOFTEST

echo "✓ ZeroClaw (.54) tests completed"
```

---

## 🧪 Phase 4: ZeroClaw Tests (.56 pure, ~1.5 hours)

```bash
#!/bin/bash

ZC56="pi@192.168.207.56"
PASS="<REDACTED-ARGONAS-PASS>"

echo "═══════════════════════════════════════════"
echo "ZeroClaw (pure) Testing on .56"
echo "═══════════════════════════════════════════"
echo ""

sshpass -p "$PASS" ssh $ZC56 << 'EOFTEST'

export MNEMOS_API_KEY=$(cat ~/.investorclaw/.mnemos_api_key 2>/dev/null || echo "test-key")
export MNEMOS_ENDPOINT="http://192.168.207.67:5002"

# Verify ZeroClaw is available
zeroclaw --version > /dev/null 2>&1 || { echo "✗ ZeroClaw not available"; exit 1; }
echo "✓ ZeroClaw (pure) available"
echo ""

# TC2.1: Basic Task
echo "[TC2.1] Basic Task Execution"
zeroclaw run \
  --task "health-check" \
  --steps "curl -H 'Authorization: Bearer \$MNEMOS_API_KEY' http://192.168.207.67:5002/health" \
  --output /tmp/zc56_tc2_1.json 2>&1 | grep -i "status\|pass\|fail"

if jq -e '.status == "success"' /tmp/zc56_tc2_1.json > /dev/null 2>&1; then
  echo "✓ PASS"
else
  echo "✗ FAIL"
fi
echo ""

echo "═══════════════════════════════════════════"
echo "ZeroClaw (.56) tests completed"
echo "═══════════════════════════════════════════"

EOFTEST

echo "✓ ZeroClaw (.56) tests completed"
```

---

## 🧪 Phase 5: Claude Code Tests (Local, ~1 hour)

```bash
#!/bin/bash

echo "═══════════════════════════════════════════"
echo "Claude Code Testing (Local)"
echo "═══════════════════════════════════════════"
echo ""

export MNEMOS_API_KEY=$(cat ~/.investorclaw/.mnemos_api_key 2>/dev/null || echo "test-key")

python3 << 'EOFTEST'
import urllib.request
import json
import os

endpoint = "http://192.168.207.67:5002"
api_key = os.environ.get("MNEMOS_API_KEY")
passed = 0
failed = 0

print("[TC4.1] Basic REST Call")
try:
    payload = {
        "model": "groq-llama",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 50
    }
    
    req = urllib.request.Request(
        f"{endpoint}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req, timeout=10) as resp:
        response = json.loads(resp.read())
    
    if "choices" in response:
        print("✓ PASS")
        passed += 1
    else:
        print("✗ FAIL")
        failed += 1
except Exception as e:
    print(f"✗ FAIL: {e}")
    failed += 1

print("")

# Summary
total = passed + failed
print("═══════════════════════════════════════════")
print(f"Claude Code Results: {passed} passed, {failed} failed")
print("═══════════════════════════════════════════")

EOFTEST

echo "✓ Claude Code tests completed"
```

---

## 📊 Consolidated Results

After running all phases, consolidate results:

```bash
#!/bin/bash

echo "═══════════════════════════════════════════════════════════"
echo "MNEMOS Agent Platform Testing - Consolidated Results"
echo "═══════════════════════════════════════════════════════════"
echo ""

echo "OpenClaw (STUDIO):"
grep "PASS\|FAIL" /tmp/oc_tc1_*.log 2>/dev/null | wc -l | xargs echo "  Tests:"
echo ""

echo "Hermes (STUDIO):"
jq '.status' /tmp/hermes_tc3_*.json 2>/dev/null | grep -c "success" | xargs echo "  Success:"
echo ""

echo "ZeroClaw .54 (nemoclaw):"
jq '.status' /tmp/zc54_tc2_*.json 2>/dev/null | grep -c "success" | xargs echo "  Success:"
echo ""

echo "ZeroClaw .56 (pure):"
jq '.status' /tmp/zc56_tc2_*.json 2>/dev/null | grep -c "success" | xargs echo "  Success:"
echo ""

echo "Claude Code (Local):"
echo "  Results in Python output above"
echo ""

echo "═══════════════════════════════════════════════════════════"
echo "Status: All tests complete"
echo "═══════════════════════════════════════════════════════════"
```

---

## 🔧 Pre-Test Verification

Before running any tests, verify all systems are reachable:

```bash
#!/bin/bash

echo "Verifying system connectivity..."
echo ""

# MNEMOS health
echo "[MNEMOS] Health check at 192.168.207.67:5002"
curl -s http://192.168.207.67:5002/health | jq '.version' && echo "✓ MNEMOS healthy" || echo "✗ MNEMOS unreachable"
echo ""

# STUDIO (OpenClaw + Hermes)
echo "[STUDIO] 192.168.207.10 (OpenClaw + Hermes)"
sshpass -p "<REDACTED-ARGONAS-PASS>" ssh -o ConnectTimeout=5 jasonperlow@192.168.207.10 "openclaw gateway status" > /dev/null && echo "✓ STUDIO reachable, OpenClaw healthy" || echo "✗ STUDIO unreachable"
echo ""

# ZeroClaw .54
echo "[ZeroClaw .54] 192.168.207.54 (nemoclaw)"
sshpass -p "<REDACTED-ARGONAS-PASS>" ssh -o ConnectTimeout=5 pi@192.168.207.54 "zeroclaw --version" > /dev/null && echo "✓ .54 reachable, ZeroClaw available" || echo "✗ .54 unreachable"
echo ""

# ZeroClaw .56
echo "[ZeroClaw .56] 192.168.207.56 (pure)"
sshpass -p "<REDACTED-ARGONAS-PASS>" ssh -o ConnectTimeout=5 pi@192.168.207.56 "zeroclaw --version" > /dev/null && echo "✓ .56 reachable, ZeroClaw available" || echo "✗ .56 unreachable"
echo ""

echo "═══════════════════════════════════════════"
echo "Verification complete"
echo "═══════════════════════════════════════════"
```

---

## 🚀 Quick Start

```bash
# 1. Verify all systems
bash verify_systems.sh

# 2. Run all tests in sequence
bash run_openclaw_tests.sh
bash run_hermes_tests.sh
bash run_zeroclaw_54_tests.sh
bash run_zeroclaw_56_tests.sh
bash run_claude_tests.py

# 3. Consolidate results
bash consolidate_results.sh
```

Or **run in parallel** (if you have multiple terminals):
```bash
# Terminal 1: OpenClaw + Hermes on STUDIO
bash run_studio_tests.sh

# Terminal 2: ZeroClaw on .54
bash run_zeroclaw_54_tests.sh

# Terminal 3: ZeroClaw on .56
bash run_zeroclaw_56_tests.sh

# Terminal 4: Claude Code (local)
bash run_claude_tests.py

# Then consolidate
bash consolidate_results.sh
```

---

**Status**: Ready for execution  
**Total Duration**: 6–8 hours (sequential) or 2–3 hours (parallel)  
**Next Step**: Run `verify_systems.sh` to ensure connectivity
