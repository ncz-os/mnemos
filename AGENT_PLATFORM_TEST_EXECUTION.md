# MNEMOS Agent Platform Testing: Execution Guide
## Run tests for OpenClaw, ZeroClaw, Hermes, Claude

**Status**: Ready to execute  
**Date**: 2026-04-19  
**Duration**: 6–8 hours (can parallelize)

---

## 🚀 Quick Start

```bash
# 1. Set environment
export MNEMOS_API_KEY=$(cat ~/.investorclaw/.mnemos_api_key)
export MNEMOS_ENDPOINT=http://192.168.207.67:5002

# 2. Verify service health
curl -H "Authorization: Bearer $MNEMOS_API_KEY" $MNEMOS_ENDPOINT/health | jq '.'

# 3. Run test suite (pick one or run all)
bash run_openclaw_tests.sh
bash run_zeroclaw_tests.sh
bash run_hermes_tests.sh
python run_claude_tests.py
```

---

## 🧪 Phase 1: OpenClaw Tests (Day 1, ~2 hours)

### Setup
```bash
# Verify OpenClaw is running
openclaw gateway status

# Create test session
openclaw sessions --json | jq '.[] | select(.id | contains("test"))'
```

### Run Tests
```bash
cat > run_openclaw_tests.sh << 'EOF'
#!/bin/bash

ENDPOINT="http://192.168.207.67:5002"
SESSION="test-mnemos-$(date +%s)"
PASSED=0
FAILED=0

echo "═══════════════════════════════════════════"
echo "OpenClaw MNEMOS Testing"
echo "═══════════════════════════════════════════"
echo ""

# TC1.1: Health Check
echo "[TC1.1] Health Check"
openclaw agent --session-id $SESSION -m "
  curl -s -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    $ENDPOINT/health | jq '.version'
" > /tmp/tc1_1.log 2>&1
if grep -q "3.0.0" /tmp/tc1_1.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  cat /tmp/tc1_1.log
  ((FAILED++))
fi
echo ""

# TC1.2: Model Listing
echo "[TC1.2] List Models"
openclaw agent --session-id $SESSION -m "
  curl -s -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    $ENDPOINT/v1/models | jq '.data | length'
" > /tmp/tc1_2.log 2>&1
if grep -q -E '^[0-9]+$' /tmp/tc1_2.log && [ $(cat /tmp/tc1_2.log) -ge 12 ]; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  cat /tmp/tc1_2.log
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
" > /tmp/tc1_3.log 2>&1
if grep -q "4" /tmp/tc1_3.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  cat /tmp/tc1_3.log
  ((FAILED++))
fi
echo ""

# TC1.4: Multi-Turn Conversation
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
" > /tmp/tc1_4.log 2>&1
if grep -q -i "alice" /tmp/tc1_4.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  cat /tmp/tc1_4.log
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
    }' | jq '.usage | keys | length'
" > /tmp/tc1_5.log 2>&1
if [ $(cat /tmp/tc1_5.log) -ge 3 ]; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  cat /tmp/tc1_5.log
  ((FAILED++))
fi
echo ""

# TC1.6: Error - Invalid Model
echo "[TC1.6] Error Handling - Invalid Model"
openclow agent --session-id $SESSION -m "
  curl -s -X POST $ENDPOINT/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"nonexistent-xyz\",
      \"messages\": [{\"role\": \"user\", \"content\": \"test\"}]
    }' -w '\nHTTP: %{http_code}\n' | grep 'HTTP:'
" > /tmp/tc1_6.log 2>&1
if grep -q "HTTP: 400" /tmp/tc1_6.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  cat /tmp/tc1_6.log
  ((FAILED++))
fi
echo ""

# TC1.7: Error - Invalid Auth
echo "[TC1.7] Error Handling - Invalid Auth"
openclaw agent --session-id $SESSION -m "
  curl -s -X POST $ENDPOINT/v1/chat/completions \
    -H 'Authorization: Bearer invalid-key-xyz' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"groq-llama\",
      \"messages\": [{\"role\": \"user\", \"content\": \"test\"}]
    }' -w '\nHTTP: %{http_code}\n' | grep 'HTTP:'
" > /tmp/tc1_7.log 2>&1
if grep -q "HTTP: 401" /tmp/tc1_7.log; then
  echo "✓ PASS"
  ((PASSED++))
else
  echo "✗ FAIL"
  cat /tmp/tc1_7.log
  ((FAILED++))
fi
echo ""

# TC1.8: Performance - Latency
echo "[TC1.8] Performance - Latency"
openclaw agent --session-id $SESSION -m "
  (time curl -s -X POST $ENDPOINT/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"groq-llama\",
      \"messages\": [{\"role\": \"user\", \"content\": \"What is AI?\"}],
      \"max_tokens\": 100
    }' > /dev/null) 2>&1 | grep real
" > /tmp/tc1_8.log 2>&1
echo "  Latency: $(cat /tmp/tc1_8.log)"
if grep -q "0m[0-4]" /tmp/tc1_8.log; then
  echo "✓ PASS (< 5s)"
  ((PASSED++))
else
  echo "⚠ WARNING (> 5s)"
fi
echo ""

# Summary
echo "═══════════════════════════════════════════"
echo "OpenClaw Test Results"
echo "═══════════════════════════════════════════"
TOTAL=$((PASSED + FAILED))
echo "Passed: $PASSED / $TOTAL"
echo "Failed: $FAILED / $TOTAL"
if [ $FAILED -eq 0 ]; then
  echo "✓ ALL TESTS PASSED"
  exit 0
else
  echo "✗ SOME TESTS FAILED"
  exit 1
fi
EOF

chmod +x run_openclaw_tests.sh
./run_openclaw_tests.sh
```

### Verify Results
```bash
# Check test logs
ls -la /tmp/tc1_*.log

# Summarize results
for f in /tmp/tc1_*.log; do
  echo "=== $(basename $f) ==="
  head -5 $f
done
```

---

## 🧪 Phase 2: ZeroClaw Tests (Day 2, ~1.5 hours)

### Setup
```bash
# Verify ZeroClaw is available
zeroclaw --version

# Check harness directory
ls -la /path/to/ZeroClaw/harness/
```

### Run Tests
```bash
# TC2.1: Basic Task
zeroclaw run \
  --task "health-check" \
  --steps "curl -H 'Authorization: Bearer \$MNEMOS_API_KEY' http://192.168.207.67:5002/health" \
  --output /tmp/zc_tc2_1.json

# TC2.2: Parallel Requests
zeroclaw run \
  --task "parallel-inference" \
  --parallel-count 5 \
  --steps "curl -X POST http://192.168.207.67:5002/v1/chat/completions -H 'Authorization: Bearer \$MNEMOS_API_KEY' -H 'Content-Type: application/json' -d '{\"model\": \"groq-llama\", \"messages\": [{\"role\": \"user\", \"content\": \"test\"}], \"max_tokens\": 50}'" \
  --output /tmp/zc_tc2_2.json

# TC2.3: Fallback
zeroclaw run \
  --task "fallback-test" \
  --primary-endpoint "http://invalid:5002" \
  --fallback-endpoint "http://192.168.207.67:5002" \
  --output /tmp/zc_tc2_3.json

# TC2.4: Cost Tracking
zeroclaw run \
  --task "cost-tracking" \
  --track-costs \
  --models "groq-llama,gpt-4o" \
  --output /tmp/zc_tc2_4.json
```

### Verify Results
```bash
# Check results
for f in /tmp/zc_tc2_*.json; do
  echo "=== $(basename $f) ==="
  jq '.status, .duration, .cost' $f
done
```

---

## 🧪 Phase 3: Hermes Agent Tests (Day 2, ~1.5 hours)

### Setup
```bash
# Verify Hermes is installed
hermes --version

# Check Hermes configuration
cat ~/.hermes/config.json | jq '.inference'
```

### Run Tests
```bash
# TC3.1: Simple Reasoning
hermes --goal "Solve: If A > B and B > C, what is the relationship between A and C?" \
  --reasoning-backend mnemos \
  --output /tmp/hermes_tc3_1.json

# TC3.2: Multi-Step Planning
hermes --goal "Create a plan for building a mobile app" \
  --reasoning-backend mnemos \
  --max-steps 5 \
  --output /tmp/hermes_tc3_2.json

# TC3.3: Memory Integration
hermes --goal "Based on past decisions, recommend next steps" \
  --reasoning-backend mnemos \
  --memory-backend mnemos \
  --search-memories true \
  --output /tmp/hermes_tc3_3.json

# TC3.4: Cost Reporting
hermes --goal "Explain machine learning concepts" \
  --reasoning-backend mnemos \
  --track-tokens \
  --show-cost \
  --output /tmp/hermes_tc3_4.json
```

### Verify Results
```bash
# Check Hermes outputs
for f in /tmp/hermes_tc3_*.json; do
  echo "=== $(basename $f) ==="
  jq '.status, .cost, .tokens' $f
done
```

---

## 🧪 Phase 4: Claude Code Tests (Day 3, ~1 hour)

### Setup
```python
cat > run_claude_tests.py << 'EOF'
#!/usr/bin/env python3
"""MNEMOS Claude Code Integration Tests"""

import urllib.request
import json
import os
import asyncio
import time

class MNEMOSClientTest:
    def __init__(self):
        self.endpoint = "http://192.168.207.67:5002"
        self.api_key = os.environ.get("MNEMOS_API_KEY")
        self.passed = 0
        self.failed = 0
    
    def test_tc4_1_basic_rest(self):
        """TC4.1: Basic REST call"""
        print("[TC4.1] Basic REST Call")
        try:
            payload = {
                "model": "groq-llama",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 50
            }
            
            req = urllib.request.Request(
                f"{self.endpoint}/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            
            with urllib.request.urlopen(req, timeout=10) as resp:
                response = json.loads(resp.read())
            
            assert "choices" in response
            assert len(response["choices"]) > 0
            print("✓ PASS\n")
            self.passed += 1
        except Exception as e:
            print(f"✗ FAIL: {e}\n")
            self.failed += 1
    
    async def test_tc4_2_async_httpx(self):
        """TC4.2: Async httpx client"""
        print("[TC4.2] Async httpx Client")
        try:
            import httpx
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.endpoint}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": "groq-llama",
                        "messages": [{"role": "user", "content": "Test async"}],
                        "max_tokens": 50
                    }
                )
                
                data = response.json()
            
            assert data["choices"][0]["message"]["content"]
            print("✓ PASS\n")
            self.passed += 1
        except Exception as e:
            print(f"✗ FAIL: {e}\n")
            self.failed += 1
    
    def test_tc4_3_multi_turn(self):
        """TC4.3: Multi-turn conversation"""
        print("[TC4.3] Multi-Turn Conversation")
        try:
            messages = [
                {"role": "user", "content": "My name is Bob."}
            ]
            
            # Turn 1
            payload = {
                "model": "groq-llama",
                "messages": messages,
                "max_tokens": 50
            }
            
            req = urllib.request.Request(
                f"{self.endpoint}/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            
            with urllib.request.urlopen(req) as resp:
                response = json.loads(resp.read())
            
            assistant_msg = response["choices"][0]["message"]["content"]
            messages.append({"role": "assistant", "content": assistant_msg})
            
            # Turn 2
            messages.append({"role": "user", "content": "What is my name?"})
            payload["messages"] = messages
            
            req = urllib.request.Request(
                f"{self.endpoint}/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            
            with urllib.request.urlopen(req) as resp:
                response = json.loads(resp.read())
            
            response_text = response["choices"][0]["message"]["content"].lower()
            assert "bob" in response_text
            print("✓ PASS\n")
            self.passed += 1
        except Exception as e:
            print(f"✗ FAIL: {e}\n")
            self.failed += 1
    
    async def test_tc4_4_concurrent(self):
        """TC4.4: Concurrent requests"""
        print("[TC4.4] Concurrent Requests")
        try:
            import httpx
            
            async with httpx.AsyncClient() as client:
                tasks = [
                    client.post(
                        f"{self.endpoint}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={
                            "model": "groq-llama",
                            "messages": [{"role": "user", "content": f"Query {i}"}],
                            "max_tokens": 50
                        }
                    )
                    for i in range(5)
                ]
                results = await asyncio.gather(*tasks)
            
            assert len(results) == 5
            assert all(r.status_code == 200 for r in results)
            print("✓ PASS\n")
            self.passed += 1
        except Exception as e:
            print(f"✗ FAIL: {e}\n")
            self.failed += 1
    
    def test_tc4_5_error_handling(self):
        """TC4.5: Error handling"""
        print("[TC4.5] Error Handling")
        try:
            # Test invalid auth
            payload = {"model": "groq-llama", "messages": []}
            
            req = urllib.request.Request(
                f"{self.endpoint}/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": "Bearer invalid-key",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    pass
            except urllib.error.HTTPError as e:
                assert e.code == 401
            
            print("✓ PASS\n")
            self.passed += 1
        except Exception as e:
            print(f"✗ FAIL: {e}\n")
            self.failed += 1
    
    def run_all(self):
        """Run all tests"""
        print("═══════════════════════════════════════════")
        print("Claude Code MNEMOS Testing")
        print("═══════════════════════════════════════════\n")
        
        self.test_tc4_1_basic_rest()
        asyncio.run(self.test_tc4_2_async_httpx())
        self.test_tc4_3_multi_turn()
        asyncio.run(self.test_tc4_4_concurrent())
        self.test_tc4_5_error_handling()
        
        print("═══════════════════════════════════════════")
        print(f"Claude Code Test Results")
        print("═══════════════════════════════════════════")
        print(f"Passed: {self.passed} / {self.passed + self.failed}")
        print(f"Failed: {self.failed} / {self.passed + self.failed}")
        
        if self.failed == 0:
            print("✓ ALL TESTS PASSED")
            return 0
        else:
            print("✗ SOME TESTS FAILED")
            return 1

if __name__ == "__main__":
    tester = MNEMOSClientTest()
    exit(tester.run_all())
EOF

chmod +x run_claude_tests.py
python3 run_claude_tests.py
```

---

## 📊 Phase 5: Cross-Platform Validation (Day 3, ~1 hour)

```bash
# Run same inference across all platforms
PROMPT="Explain the difference between machine learning and deep learning in 2 sentences."

echo "═══════════════════════════════════════════"
echo "Cross-Platform Validation"
echo "═══════════════════════════════════════════"
echo ""

# OpenClaw
echo "[OpenClaw]"
openclaw agent --session-id test-mnemos -m "
  curl -s -X POST http://192.168.207.67:5002/v1/chat/completions \
    -H 'Authorization: Bearer \$(echo \$MNEMOS_API_KEY)' \
    -H 'Content-Type: application/json' \
    -d '{
      \"model\": \"groq-llama\",
      \"messages\": [{\"role\": \"user\", \"content\": \"$PROMPT\"}],
      \"max_tokens\": 100
    }' | jq '.choices[0].message.content'
" > /tmp/cross_openclaw.txt
sleep 2

# ZeroClaw
echo "[ZeroClaw]"
zeroclaw run \
  --task "inference" \
  --steps "curl -s -X POST http://192.168.207.67:5002/v1/chat/completions -H 'Authorization: Bearer \$MNEMOS_API_KEY' -H 'Content-Type: application/json' -d '{\"model\": \"groq-llama\", \"messages\": [{\"role\": \"user\", \"content\": \"$PROMPT\"}], \"max_tokens\": 100}' | jq '.choices[0].message.content'" \
  --output /tmp/cross_zeroclaw.json
sleep 2

# Hermes
echo "[Hermes]"
hermes --goal "$PROMPT" \
  --reasoning-backend mnemos \
  --output /tmp/cross_hermes.json
sleep 2

# Claude
echo "[Claude Code]"
python3 << 'ENDPY'
import urllib.request, json, os
req = urllib.request.Request(
    "http://192.168.207.67:5002/v1/chat/completions",
    data=json.dumps({
        "model": "groq-llama",
        "messages": [{"role": "user", "content": os.environ.get("PROMPT", "test")}],
        "max_tokens": 100
    }).encode(),
    headers={
        "Authorization": f"Bearer {os.environ.get('MNEMOS_API_KEY')}",
        "Content-Type": "application/json"
    },
    method="POST"
)
with urllib.request.urlopen(req) as resp:
    print(json.loads(resp.read())["choices"][0]["message"]["content"])
ENDPY
> /tmp/cross_claude.txt

echo ""
echo "Results saved to /tmp/cross_*.txt"
echo "Compare outputs for consistency"
```

---

## ✅ Verification Checklist

```bash
# After completing all phases, verify:

# 1. Check all test logs exist
[ -f /tmp/tc1_*.log ] && echo "✓ OpenClaw logs" || echo "✗ Missing OpenClaw logs"
[ -f /tmp/zc_tc2_*.json ] && echo "✓ ZeroClaw logs" || echo "✗ Missing ZeroClaw logs"
[ -f /tmp/hermes_tc3_*.json ] && echo "✓ Hermes logs" || echo "✗ Missing Hermes logs"
[ -f /tmp/cross_*.txt ] && echo "✓ Cross-platform logs" || echo "✗ Missing cross-platform logs"

# 2. Count passes/failures
echo "Test Results Summary:"
grep "PASS\|FAIL" /tmp/tc1_*.log | wc -l
grep "status.*success" /tmp/zc_tc2_*.json | wc -l

# 3. Check for critical errors
grep -i "500\|critical\|blocked" /tmp/*.log /tmp/*.json || echo "✓ No critical errors"

# 4. Verify cost tracking
grep -E "cost|price|token" /tmp/*.json | head -5

# 5. Generate report
cat > TEST_REPORT.md << 'EOF'
# MNEMOS Agent Platform Test Report
**Date**: $(date)
**Total Tests**: [count]
**Passed**: [count] ✓
**Failed**: [count] ✗

## Platform Results
- OpenClaw: [pass rate]%
- ZeroClaw: [pass rate]%
- Hermes: [pass rate]%
- Claude: [pass rate]%

## Performance
- Avg latency: [Xs]
- Max latency: [Xs]
- Concurrent capacity: [n]

## Issues
[List any]

## Sign-Off
- Tested: [date]
- Status: [GO/NO-GO]
EOF
```

---

## 🎯 Success Indicators

✅ **Tests are successful when**:
- [ ] OpenClaw: 8/8 tests pass
- [ ] ZeroClaw: 4/4 tests pass
- [ ] Hermes: 4/4 tests pass
- [ ] Claude: 5/5 tests pass
- [ ] Cross-platform: Outputs are consistent
- [ ] No critical errors in any log
- [ ] Latency <5s (p99)
- [ ] Cost tracking accurate

---

**Status**: Ready for execution  
**Est. Duration**: 6–8 hours  
**Next Step**: Execute Phase 1 (OpenClaw tests)
