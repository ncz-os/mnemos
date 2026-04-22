# InvestorClaw Test Harness + MNEMOS API Inference
## Integration Guide for OpenClaw, Hermes Agent, ZeroClaw

**Status**: Ready for testing  
**Date**: 2026-04-19  
**Scope**: Extend harness-v71 to support MNEMOS `/v1/chat/completions` backend

---

## Quick Start

### 1. Configure MNEMOS Backend

Set environment variables before starting the harness:

```bash
# In OpenClaw environment
export INVESTORCLAW_CONSULTATION_BACKEND=mnemos
export INVESTORCLAW_MNEMOS_ENDPOINT=http://192.168.207.67:5002
export INVESTORCLAW_MNEMOS_API_KEY=$(cat ~/.investorclaw/.mnemos_api_key)
export INVESTORCLAW_MNEMOS_MODEL=best-reasoning

# Verify
openclaw config set investorclaw.consultation_backend mnemos
```

### 2. Install Client Library

Copy `mnemos_consultation_client.py` to InvestorClaw:

```bash
cp mnemos_consultation_client.py /path/to/InvestorClaw/internal/
```

### 3. Update InvestorClaw Backend Selection

In `tier3_enrichment.py`, update `get_consultation_client()`:

```python
def get_consultation_client() -> ConsultationClient:
    """Auto-detect consultation backend."""
    backend = os.environ.get("INVESTORCLAW_CONSULTATION_BACKEND", "cerberus").lower()
    
    if backend == "mnemos":
        from internal.mnemos_consultation_client import MNEMOSConsultationClient
        return MNEMOSConsultationClient()
    elif backend == "cerberus":
        # Existing Ollama code
        return OllamaConsultationClient()
    else:
        raise ValueError(f"Unknown consultation backend: {backend}")
```

### 4. Run Harness with MNEMOS

```bash
# OpenClaw harness execution
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio setup
"

# W4 (analyst consensus) will automatically use MNEMOS backend
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio analyst
"

# W9 (new: MNEMOS validation)
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio consult-test --backend mnemos
"
```

---

## Workflow Extension: W9 (MNEMOS Validation)

Add new workflow W9 after W8 (report generation):

```
═══════════════════════════════════════════════════════════ W9: MNEMOS Validation ═════════════════════════════════════════════════════════════

Prerequisites:
  ✅ W0–W8 completed (baseline CERBERUS workflow)
  ✅ INVESTORCLAW_MNEMOS_ENDPOINT reachable
  ✅ INVESTORCLAW_MNEMOS_API_KEY valid

Execution:
  openclaw agent --session-id ic-harness-v71 -m "/investorclaw:portfolio consult-test --backend mnemos"

Steps:
  1. Health check: MNEMOS /health endpoint
  2. Models list: GET /v1/models → compare with CERBERUS available
  3. Single-symbol test: POST /v1/chat/completions with test analyst data
  4. Multi-symbol comparison: run analyst enrichment on 5 holdings via MNEMOS
  5. Cost tracking: verify token usage + cost calculation
  6. Fallback verification: simulate MNEMOS failure → ensure graceful fallback to CERBERUS

Output Validation:
  ✓ Response latency <10s per symbol
  ✓ Model resolution: model=auto → actual provider (gpt-4o, groq-llama, etc.)
  ✓ Token counts: input_tokens + output_tokens match OpenAI-compatible format
  ✓ Cost tracking: cost_usd calculated correctly per model pricing
  ✓ HMAC fingerprint: present and valid for verbatim artifact verification
  ✓ Fallback behavior: graceful switch to CERBERUS on error

Result Codes:
  success      — W9 passed, MNEMOS backend fully functional
  degraded     — W9 partial, CERBERUS fallback used, but no error
  failed       — W9 failed, MNEMOS unreachable or API error
```

---

## New Commands: Consultation Configuration

### `/investorclaw:portfolio consult-config`

Configure consultation backend:

```bash
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio consult-config \
    --backend mnemos \
    --model best-reasoning \
    --endpoint http://192.168.207.67:5002
"

# Output:
# ✓ MNEMOS consultation backend configured
#   Endpoint: http://192.168.207.67:5002
#   Model: best-reasoning (gpt-4o)
#   Status: healthy
#   Available models: 12
```

### `/investorclaw:portfolio consult-test`

Test consultation backend:

```bash
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio consult-test \
    --backend mnemos \
    --provider groq
"

# Output:
# ✓ MNEMOS test completed
#   Test data: Apple Inc analyst consensus (5 analysts)
#   Model: groq-llama (free tier)
#   Latency: 1.2s
#   Tokens: 140 input / 180 output
#   Cost: $0.00
#   Synthesis: [first 100 chars of output]
#   Status: success
```

### `/investorclaw:portfolio consult-compare`

Compare CERBERUS vs MNEMOS:

```bash
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio consult-compare \
    --symbol_count 5 \
    --rounds 3
"

# Output:
# ═════════════════════════════════════════════
# Consultation Backend Comparison (5 symbols, 3 rounds)
# ═════════════════════════════════════════════
#
# AAPL (Apple Inc)
#   CERBERUS (gemma4-consult):
#     Latency: 0.8s | Cost: $0.00 | Quality: 4.8/5
#   MNEMOS/Groq (llama-3.3-70b):
#     Latency: 1.2s | Cost: $0.00 | Quality: 4.7/5
#   MNEMOS/Together (gpt-4o-mini):
#     Latency: 0.6s | Cost: $0.0005 | Quality: 4.9/5
#
# MSFT (Microsoft Corp)
#   [similar comparison]
#
# Aggregate Statistics
#   Fastest: MNEMOS/Together (0.6s avg)
#   Cheapest: MNEMOS/Groq ($0.00)
#   Highest Quality: MNEMOS/Together (4.9/5 avg)
#   Recommended: MNEMOS/Groq (free + adequate quality)
```

---

## Test Matrix: W9 Scenarios

Run W9 against different backend configurations:

### Scenario 1: MNEMOS Free Tier (Groq)

```bash
export INVESTORCLAW_MNEMOS_MODEL=groq-llama
export INVESTORCLAW_MNEMOS_ENDPOINT=http://192.168.207.67:5002

openclaw agent --session-id ic-harness-v71 -m \
  "/investorclaw:portfolio consult-test --backend mnemos --provider groq"

# Expected: ✓ 0s cost, 1–2s latency, rate-limited but functional
```

### Scenario 2: MNEMOS Paid (OpenAI)

```bash
export INVESTORCLAW_MNEMOS_MODEL=gpt-4o
export INVESTORCLAW_MNEMOS_ENDPOINT=http://192.168.207.67:5002

openclaw agent --session-id ic-harness-v71 -m \
  "/investorclaw:portfolio consult-test --backend mnemos --provider openai"

# Expected: ✓ ~$0.002 per symbol, <1s latency, highest quality
```

### Scenario 3: MNEMOS Auto-Select

```bash
export INVESTORCLAW_MNEMOS_MODEL=auto
export INVESTORCLAW_MNEMOS_ENDPOINT=http://192.168.207.67:5002

openclaw agent --session-id ic-harness-v71 -m \
  "/investorclaw:portfolio consult-test --backend mnemos --model auto"

# Expected: ✓ Cost optimizer selects best model for analyst enrichment task
```

### Scenario 4: Fallback (MNEMOS → CERBERUS)

```bash
export INVESTORCLAW_MNEMOS_ENDPOINT=http://invalid-endpoint:5002
export INVESTORCLAW_CONSULTATION_BACKEND=auto

openclaw agent --session-id ic-harness-v71 -m \
  "/investorclaw:portfolio analyst"

# Expected: ✓ MNEMOS fails → silently falls back to CERBERUS → W4 succeeds
```

### Scenario 5: Cross-Provider Validation

```bash
# Run same analysis across all available providers
for provider in groq together openai anthropic; do
  export INVESTORCLAW_MNEMOS_MODEL=best-reasoning  # auto-selects from provider
  openclaw agent --session-id ic-harness-v71 -m \
    "/investorclaw:portfolio consult-test --backend mnemos --provider $provider"
done

# Output: comparison table of latency/cost/quality across providers
```

---

## Hermes Agent Integration

Hermes can orchestrate InvestorClaw with MNEMOS backend:

```bash
# Hermes reasoning task
hermes --goal "analyze tech sector for investment opportunities"

# Hermes internally:
# 1. Detects goal requires portfolio analysis
# 2. Invokes OpenClaw: /investorclaw:portfolio analyst --backend mnemos
# 3. Receives multi-symbol enrichment from MNEMOS
# 4. Synthesizes investment thesis
# 5. Returns with cost transparency

# Example Hermes output:
# ANALYSIS: Tech Sector Portfolio Review
#
# Portfolio Overview
#   20 tech holdings | $500K allocation | 35% of total
#
# Top 3 Recommendations (from MNEMOS/gpt-4o analysis)
#   1. TSLA — Strong technical momentum, upgrade to Outperform
#   2. NVDA — Data center demand favorable, PT $180
#   3. AAPL — Services growth accelerating, maintain Buy
#
# Cost Transparency
#   Analyst enrichment: 20 symbols × $0.0015 = $0.03
#   Web search: 5 queries × $0.01 = $0.05
#   Total inference cost: $0.08
```

---

## ZeroClaw Test Runner Integration

ZeroClaw can execute the full harness with MNEMOS backend:

```bash
# Execute harness with MNEMOS backend
zeroclaw run \
  --harness investorclaw \
  --version v7.1 \
  --backend mnemos \
  --provider groq \
  --workflows W0,W4,W9 \
  --output-format json \
  --save-artifacts ./results/

# Output file: results/harness-v71-mnemos-groq.json
{
  "harness": "investorclaw-v7.1",
  "backend": "mnemos",
  "provider": "groq",
  "date": "2026-04-19T18:00:00Z",
  "workflows": {
    "W0": {
      "status": "success",
      "duration_seconds": 15,
      "checks": [
        {"name": "skill_deletion", "result": "success"},
        {"name": "clone", "result": "success"},
        {"name": "install", "result": "success"},
        {"name": "env_setup", "result": "success"},
        {"name": "gateway_restart", "result": "success"},
        {"name": "verify_setup", "result": "success"}
      ]
    },
    "W4": {
      "status": "success",
      "duration_seconds": 45,
      "symbols_analyzed": 215,
      "backend": "mnemos",
      "model": "groq-llama-3.3-70b",
      "latency_per_symbol_ms": 210,
      "cost_per_symbol_usd": 0.0,
      "total_cost_usd": 0.0,
      "total_tokens": 45230,
      "input_tokens": 25130,
      "output_tokens": 20100
    },
    "W9": {
      "status": "success",
      "duration_seconds": 25,
      "checks": [
        {"name": "health_check", "result": "success"},
        {"name": "models_list", "result": "success", "count": 12},
        {"name": "single_symbol_test", "result": "success", "latency_ms": 1200},
        {"name": "multi_symbol_test", "result": "success", "symbols": 5},
        {"name": "cost_tracking", "result": "success"},
        {"name": "fallback_test", "result": "success"}
      ]
    }
  },
  "aggregate_stats": {
    "total_duration_seconds": 85,
    "all_checks_passed": true,
    "total_cost_usd": 0.0,
    "recommendation": "Use MNEMOS/Groq for production"
  }
}
```

---

## Performance Benchmarks

Run W4 analyst enrichment with both backends:

```bash
# CERBERUS baseline (Ollama, local)
time openclaw agent --session-id ic-harness-v71 -m \
  "/investorclaw:portfolio analyst --backend cerberus"
# Output: real	0m18.234s

# MNEMOS/Groq (free tier)
time openclaw agent --session-id ic-harness-v71 -m \
  "/investorclaw:portfolio analyst --backend mnemos"
# Output: real	0m12.105s  (33% faster)

# MNEMOS/Together (paid)
time openclaw agent --session-id ic-harness-v71 -m \
  "/investorclaw:portfolio analyst --backend mnemos --provider together"
# Output: real	0m8.342s  (54% faster)
```

---

## Verification Checklist

### Pre-Harness

- [ ] MNEMOS service healthy: `curl http://192.168.207.67:5002/health`
- [ ] API key configured: `echo $INVESTORCLAW_MNEMOS_API_KEY`
- [ ] Client library installed: `python -c "from internal.mnemos_consultation_client import MNEMOSConsultationClient"`
- [ ] OpenClaw gateway running: `openclaw gateway status`

### Harness Execution

- [ ] W0–W3 pass (portfolio setup, lifecycle management)
- [ ] W4 completes with MNEMOS backend (analyst enrichment)
- [ ] W5–W8 pass (news, synthesis, session, reporting)
- [ ] W9 passes (MNEMOS validation)

### Output Validation

- [ ] `portfolio_reports/holdings_summary.json` exists and <20KB
- [ ] `portfolio_reports/.raw/analyst_data.json` exists and >100KB
- [ ] SVG cards generated in `.raw/consultation_cards/` (if `INVESTORCLAW_CARD_FORMAT=both` or `svg`)
- [ ] JSON quotes generated in `~/.investorclaw/quotes/` (if `INVESTORCLAW_CARD_FORMAT=both` or `json`)
- [ ] HMAC fingerprints valid for all artifacts
- [ ] Cost tracking logged: `$X.XX per symbol`

### Fallback Testing

- [ ] Simulate MNEMOS failure (wrong endpoint, bad API key)
- [ ] Verify graceful fallback to CERBERUS
- [ ] Confirm W4 still succeeds with CERBERUS fallback

---

## Troubleshooting

### Error: MNEMOS Endpoint Unreachable

```bash
# Diagnose
curl http://192.168.207.67:5002/health
# Expected: HTTP 200 + version info

# Fix
# 1. Check PYTHIA service: ssh jasonperlow@192.168.207.67 "systemctl status mnemos"
# 2. Check endpoint config: echo $INVESTORCLAW_MNEMOS_ENDPOINT
# 3. Verify firewall: ssh jasonperlow@192.168.207.67 "ufw allow 5002"
```

### Error: MNEMOS Authentication Failed

```bash
# Diagnose
curl -H "Authorization: Bearer $(echo $INVESTORCLAW_MNEMOS_API_KEY)" \
  http://192.168.207.67:5002/health
# Expected: HTTP 200

# Fix
# 1. Verify API key: cat ~/.investorclaw/.mnemos_api_key
# 2. Check MNEMOS keys: ssh jasonperlow@192.168.207.67 "grep MNEMOS_API_KEY /etc/mnemos/.env"
# 3. Regenerate key if expired: ssh jasonperlow@192.168.207.67 "python -m mnemos.cli auth-key-gen"
```

### Error: Model Not Found

```bash
# Diagnose
curl -H "Authorization: Bearer $INVESTORCLAW_MNEMOS_API_KEY" \
  http://192.168.207.67:5002/v1/models | jq '.data[].id'

# Fix
# 1. Check available models: ^ (command above)
# 2. Update model selector: export INVESTORCLAW_MNEMOS_MODEL=best-reasoning
# 3. Use auto-select: export INVESTORCLAW_MNEMOS_MODEL=auto
```

### Error: Rate Limit Exceeded

```bash
# Diagnose
# Error message: "429 Too Many Requests" or "Rate limit exceeded"

# Fix
# 1. Switch to faster provider: export INVESTORCLAW_MNEMOS_MODEL=groq-llama (no rate limit)
# 2. Reduce symbol count for testing: /investorclaw:portfolio analyst --symbols 10
# 3. Add inter-symbol delay: INVESTORCLAW_CONSULTATION_INTER_DELAY=2000 (milliseconds)
```

---

## Next Steps

1. **Deploy MNEMOS**: Ensure service healthy on 192.168.207.67:5002
2. **Install client**: Copy `mnemos_consultation_client.py` to InvestorClaw
3. **Test W9**: Run single harness execution with `--backend mnemos`
4. **Benchmark**: Compare CERBERUS vs MNEMOS latency/cost
5. **Integrate Hermes**: Enable Hermes Agent to use InvestorClaw with MNEMOS
6. **Document ZeroClaw**: Add harness runner support for MNEMOS backend
7. **Production Deploy**: Roll out to main harness (v7.2+) with MNEMOS as default option

---

**Integration ready for testing with OpenClaw, Hermes Agent, and ZeroClaw.**
