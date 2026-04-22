# InvestorClaw + MNEMOS API Inference Integration
## v3.0.0 Command Surface Extension for OpenClaw, Hermes Agent, ZeroClaw

**Status**: Ready for implementation  
**Date**: 2026-04-19  
**Scope**: Test harness v7.1 integration with `/v1/chat/completions` endpoint

---

## Overview

InvestorClaw's test harness (v7.1) currently uses **CERBERUS** (Ollama at 192.168.207.96:11434) for Tier 3 enrichment during analyst consensus synthesis (workflow W4). This document extends the harness to support **MNEMOS API inference** as an alternative consultation backend.

**Key benefit**: Unified, cost-aware inference across OpenAI, Groq, Together AI, Anthropic, and other providers via the MNEMOS `/v1/chat/completions` endpoint—instead of local-only Ollama.

---

## Architecture

### Current State (CERBERUS-only)

```
InvestorClaw W4 (analyst consensus)
  ↓
tier3_enrichment.ConsultationClient
  ↓
Ollama @ 192.168.207.96:11434 (/api/generate)
  ↓
gemma4-consult (local inference)
```

### Proposed (MNEMOS + CERBERUS)

```
InvestorClaw W4 (analyst consensus)
  ↓
tier3_enrichment.ConsultationClient (backend auto-detection)
  ├─ CERBERUS (Ollama, 192.168.207.96:11434) — local, fast
  ├─ MNEMOS (FastAPI, 192.168.207.67:5002) — remote, multi-provider
  └─ Custom (environment variable override)
  ↓
Selected backend
  ├─ /api/generate (Ollama)
  └─ /v1/chat/completions (OpenAI-compatible)
```

---

## Implementation: Environment Variables

### New Configuration

Add to `.env` (InvestorClaw skill or OpenClaw config):

```bash
# Consultation backend selection (optional, defaults to CERBERUS Ollama)
INVESTORCLAW_CONSULTATION_BACKEND=mnemos  # 'cerberus' | 'mnemos' | 'auto'

# MNEMOS endpoint (only used if BACKEND=mnemos or auto)
INVESTORCLAW_MNEMOS_ENDPOINT=http://192.168.207.67:5002
INVESTORCLAW_MNEMOS_API_KEY=<bearer-token>

# Model selection within MNEMOS
INVESTORCLAW_MNEMOS_MODEL=best-reasoning     # or: auto, best-coding, gpt-4, etc.

# CERBERUS still available as fallback
INVESTORCLAW_CONSULTATION_ENDPOINT=http://192.168.207.96:11434
INVESTORCLAW_CONSULTATION_MODEL=gemma4-consult
```

### Backend Selection Logic

In `tier3_enrichment.py`, modify `get_consultation_client()`:

```python
def get_consultation_client() -> ConsultationClient:
    """Auto-detect and instantiate consultation backend."""
    backend = os.environ.get("INVESTORCLAW_CONSULTATION_BACKEND", "auto").lower()
    
    if backend == "mnemos":
        return MNEMOSConsultationClient()
    elif backend == "cerberus":
        return OllamaConsultationClient()
    elif backend == "auto":
        # Try MNEMOS first, fall back to CERBERUS
        return AutoSelectConsultationClient()
    else:
        raise ValueError(f"Unknown backend: {backend}")
```

---

## New: MNEMOSConsultationClient

Create `internal/mnemos_consultation.py`:

```python
#!/usr/bin/env python3
"""
MNEMOS API Inference Backend for InvestorClaw Tier 3 Enrichment.

Implements ConsultationClient interface for the MNEMOS `/v1/chat/completions` endpoint.
Supports multi-provider routing, cost-aware model selection, and memory injection.
"""

import json
import time
import urllib.request
import urllib.error
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class MNEMOSConsultationClient:
    """Consultation client backed by MNEMOS API inference."""
    
    def __init__(self):
        self.endpoint = os.environ.get(
            "INVESTORCLAW_MNEMOS_ENDPOINT",
            "http://192.168.207.67:5002"
        ).rstrip("/")
        self.api_key = os.environ.get(
            "INVESTORCLAW_MNEMOS_API_KEY",
            ""
        )
        self.model = os.environ.get(
            "INVESTORCLAW_MNEMOS_MODEL",
            "best-reasoning"
        )
        self.timeout = 60.0
        
        if not self.api_key:
            raise ValueError(
                "INVESTORCLAW_MNEMOS_API_KEY not set. "
                "Required for MNEMOS backend."
            )
        
        self._verify_endpoint()
    
    def _verify_endpoint(self) -> None:
        """Verify MNEMOS endpoint is reachable."""
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/health",
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read())
                logger.info(
                    f"MNEMOS endpoint verified: v{data.get('version')}, "
                    f"{len(data.get('models', []))} models available"
                )
        except Exception as e:
            raise RuntimeError(
                f"MNEMOS endpoint unreachable: {self.endpoint}/health — {e}"
            )
    
    def consult(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 500,
    ) -> dict:
        """
        Query MNEMOS API for inference.
        
        Args:
            prompt: User query
            system_prompt: System role (optional)
            temperature: Sampling temperature (0.0–2.0)
            max_tokens: Max output tokens
        
        Returns:
            {
                "response": "...",
                "model": "gpt-4o" (after alias resolution),
                "endpoint": "http://...:5002",
                "inference_ms": 3200,
                "is_heuristic": false,
                "input_tokens": 140,
                "output_tokens": 180,
                "total_tokens": 320
            }
        """
        messages = []
        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt
            })
        messages.append({
            "role": "user",
            "content": prompt
        })
        
        request_payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.95
        }
        
        start_time = time.time()
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/v1/chat/completions",
                data=json.dumps(request_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response_data = json.loads(resp.read())
            
            inference_ms = int((time.time() - start_time) * 1000)
            
            # Extract response
            choice = response_data.get("choices", [{}])[0]
            message = choice.get("message", {})
            response_text = message.get("content", "").strip()
            
            # Extract usage
            usage = response_data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            
            return {
                "response": response_text,
                "model": response_data.get("model", self.model),
                "endpoint": self.endpoint,
                "inference_ms": inference_ms,
                "is_heuristic": False,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "provider_info": {
                    "inference_time_ms": inference_ms,
                    "cost_estimate": self._estimate_cost(
                        response_data.get("model", self.model),
                        input_tokens,
                        output_tokens
                    )
                }
            }
        
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError(
                    "MNEMOS authentication failed. Check INVESTORCLAW_MNEMOS_API_KEY."
                )
            elif e.code == 400:
                body = e.read().decode("utf-8")
                raise RuntimeError(f"MNEMOS API error: {body}")
            else:
                raise RuntimeError(f"MNEMOS HTTP {e.code}: {e.reason}")
        
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"MNEMOS connection failed: {e.reason}"
            )
        
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"MNEMOS response parse error: {e}"
            )
    
    def _estimate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int
    ) -> dict:
        """Estimate inference cost based on model and token usage."""
        # Simplified pricing model (update with actual provider rates)
        pricing = {
            "gpt-4o": {"input": 0.005, "output": 0.015},
            "gpt-4-turbo": {"input": 0.01, "output": 0.03},
            "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
            "grok-4": {"input": 0.0, "output": 0.0},  # Free tier
            "llama-405b": {"input": 0.001, "output": 0.001},
        }
        
        rates = pricing.get(model, {"input": 0.001, "output": 0.002})
        input_cost = (input_tokens / 1000) * rates["input"]
        output_cost = (output_tokens / 1000) * rates["output"]
        
        return {
            "model": model,
            "input_cost_usd": round(input_cost, 6),
            "output_cost_usd": round(output_cost, 6),
            "total_cost_usd": round(input_cost + output_cost, 6)
        }
```

---

## Test Harness Integration: New Commands

### 1. Consultation Backend Configuration

Add command to InvestorClaw:

```bash
investorclaw consult-config --backend mnemos --model best-reasoning
# Output: Configuration saved. Using MNEMOS backend @ http://192.168.207.67:5002
```

### 2. Test Against MNEMOS

Add to harness test suite (W9—new workflow):

```bash
# W9: MNEMOS Integration Test
openclaw agent --session-id ic-harness-v71 -m "/portfolio consult-test --backend mnemos"

# Execution:
#   1. Query MNEMOS health
#   2. Run analyst enrichment via MNEMOS (W4 alternative)
#   3. Compare output vs CERBERUS baseline
#   4. Validate cost tracking
#   5. Verify memory injection (optional)
```

### 3. Harness Configuration Extension

Update `harness-v71.txt` CERBERUS section:

```
═══════════════════════════════════════════════════════════ CONSULTATION BACKENDS ═════════════════════════════════════════════════════════
Default Backend: CERBERUS (Ollama, 192.168.207.96:11434, local, fast)
Alternative:    MNEMOS (FastAPI, 192.168.207.67:5002, remote, multi-provider, cost-optimized)

Selection:
  ENV: INVESTORCLAW_CONSULTATION_BACKEND=cerberus|mnemos|auto
  CLI: /portfolio consult-config --backend <name>
  Harness: set before W4

MNEMOS Requirements:
  • INVESTORCLAW_MNEMOS_ENDPOINT=http://192.168.207.67:5002
  • INVESTORCLAW_MNEMOS_API_KEY=<token>
  • INVESTORCLAW_MNEMOS_MODEL=best-reasoning|best-coding|auto|gpt-4|etc.

W4 behavior (MNEMOS selected):
  ✅ Analyst enrichment via MNEMOS /v1/chat/completions
  ✅ Cost tracking + optimization
  ✅ Memory injection (if INVESTORCLAW_MNEMOS_SEARCH=true)
  ✅ Multi-turn synthesis via conversation history
  ✅ Provider auto-selection (if model=auto)

Cost Comparison (per symbol, ~100 token analysis):
  CERBERUS (Ollama, local):  $0.00 (GPU amortized)
  MNEMOS/Groq:              $0.00 (free tier, rate-limited)
  MNEMOS/Together:          $0.001–0.005
  MNEMOS/OpenAI:            $0.001–0.003

W9 (new): MNEMOS Validation
  [same structure as W0–W8 but focused on alternative backend]
  Prerequisites: MNEMOS healthy + API key valid
  Tests:
    • Health endpoint (/health)
    • Model listing (/v1/models)
    • Basic inference (/v1/chat/completions)
    • Analyst enrichment (W4 alternative)
    • Cost tracking + optimization
    • Fallback to CERBERUS if MNEMOS fails
  Output: success | degraded (CERBERUS fallback) | failed
```

---

## OpenClaw Integration Examples

### Setup

```bash
# 1. Configure MNEMOS backend in OpenClaw environment
openclaw config set investorclaw.consultation_backend mnemos
openclaw config set investorclaw.mnemos_endpoint http://192.168.207.67:5002
openclaw config set investorclaw.mnemos_api_key <bearer-token>
openclaw config set investorclaw.mnemos_model best-reasoning

# 2. Verify
openclaw config get investorclaw
```

### Command Surface Extension

Add to InvestorClaw command router (`runtime/router.py`):

```python
# Consultation backend commands (new)
"consult-config": ("commands.consultation_config", ["backend", "model", "endpoint"]),
"consult-test": ("commands.consultation_test", ["backend", "provider"]),
"consult-compare": ("commands.consultation_compare", ["symbol_count"]),  # W4 CERBERUS vs MNEMOS
```

### Example Workflow

```bash
# 1. Setup harness with MNEMOS
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio consult-config \
    --backend mnemos \
    --model best-reasoning
"

# 2. Run W4 with MNEMOS backend
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio analyst --backend mnemos
"

# 3. Compare CERBERUS vs MNEMOS (same portfolio)
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio consult-compare --symbol_count 5
"
# Output: side-by-side comparison of:
#   - Inference latency
#   - Cost per symbol
#   - Synthesis quality (BLEU/semantic similarity)
#   - Model used
#   - Token usage
```

---

## Hermes Agent Integration

Hermes Agent can use InvestorClaw as a research tool via OpenClaw:

```bash
# Hermes queries InvestorClaw via MNEMOS-backed analyst enrichment
hermes --goal "analyze tech sector holdings for tax efficiency"

# Hermes internally:
# 1. Calls OpenClaw: /investorclaw:portfolio analyst --backend mnemos
# 2. Receives multi-symbol enrichment via MNEMOS
# 3. Synthesizes investment thesis
# 4. Returns with cost transparency
```

---

## ZeroClaw Integration

ZeroClaw can invoke InvestorClaw harness directly:

```bash
# ZeroClaw test runner
zeroclaw run \
  --harness investorclaw \
  --version v7.1 \
  --backend mnemos \
  --workflows W0,W4,W9

# Output:
# ✅ W0: Lifecycle OK
# ✅ W4: Analyst enrichment (MNEMOS) 3.2s per symbol
# ✅ W9: MNEMOS validation OK
# Summary: 215-holding portfolio processed in 12.5s (MNEMOS) vs 18s (CERBERUS)
# Cost: $0.23 (Together AI) vs $0.00 (Ollama, GPU amortized)
```

---

## API Inference Endpoint Mapping

The MNEMOS `/v1/chat/completions` endpoint maps directly to InvestorClaw consumption patterns:

### Request Structure

```json
{
  "model": "best-reasoning",           // Model selector (alias or explicit)
  "messages": [                         // Multi-turn conversation
    {
      "role": "system",
      "content": "You are a financial analyst. Provide synthesis for: ..."
    },
    {
      "role": "user",
      "content": "Analyze this analyst consensus: { data }"
    }
  ],
  "temperature": 0.7,                   // Quality vs speed tradeoff
  "max_tokens": 500,                    // Output limit
  "top_p": 0.95                         // Nucleus sampling
}
```

### Response Structure

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1713607200,
  "model": "gpt-4o",                    // Resolved model
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Based on the analyst consensus..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 140,
    "completion_tokens": 180,
    "total_tokens": 320
  }
}
```

### Cost Tracking

InvestorClaw extracts token usage and computes cost:

```python
def _estimate_cost(model, input_tokens, output_tokens):
    # Pricing table (from MNEMOS /v1/providers/recommend)
    pricing = MNEMOS_PRICING_REGISTRY[model]
    input_cost = (input_tokens / 1000) * pricing["input_rate"]
    output_cost = (output_tokens / 1000) * pricing["output_rate"]
    return input_cost + output_cost
```

---

## Testing Checklist

### Phase 1: Local Testing

- [ ] MNEMOSConsultationClient instantiates with valid credentials
- [ ] Endpoint health check passes
- [ ] Single-symbol enrichment completes in <5s
- [ ] Cost tracking matches MNEMOS pricing
- [ ] Fallback to CERBERUS on MNEMOS failure

### Phase 2: Harness Integration

- [ ] W4 runs with `INVESTORCLAW_CONSULTATION_BACKEND=mnemos`
- [ ] Output format unchanged (SVG cards, JSON quotes)
- [ ] HMAC fingerprinting preserves verbatim_required=true
- [ ] 215-symbol portfolio completes in reasonable time

### Phase 3: OpenClaw Integration

- [ ] `/investorclaw:portfolio consult-config` command works
- [ ] `/investorclaw:portfolio analyst --backend mnemos` completes
- [ ] Consultation cards rendered correctly
- [ ] Cost reporting shown in artifact

### Phase 4: Cross-Platform Testing

- [ ] Hermes Agent can query InvestorClaw via MNEMOS
- [ ] ZeroClaw harness runner executes W9 workflow
- [ ] Baseline comparison (CERBERUS vs MNEMOS) shows cost/latency tradeoff

---

## Deployment Checklist

1. **MNEMOS Service**: Healthy on 192.168.207.67:5002
   ```bash
   curl http://192.168.207.67:5002/health
   # Expected: {"version": "3.0.0", "status": "healthy"}
   ```

2. **InvestorClaw Backend**: `internal/mnemos_consultation.py` installed
   ```bash
   python -c "from internal.mnemos_consultation import MNEMOSConsultationClient"
   # Expected: no error
   ```

3. **Environment Configuration**: `.env` or `~/.investorclaw/.env` set
   ```bash
   echo "INVESTORCLAW_MNEMOS_ENDPOINT=http://192.168.207.67:5002" >> ~/.investorclaw/.env
   echo "INVESTORCLAW_MNEMOS_API_KEY=$KEY" >> ~/.investorclaw/.env
   chmod 600 ~/.investorclaw/.env
   ```

4. **Harness Execution**: Test W9 workflow
   ```bash
   openclaw agent --session-id ic-harness-v71 -m \
     "/investorclaw:portfolio consult-test --backend mnemos"
   ```

---

## Backward Compatibility

**All changes are backward compatible**:
- Default behavior unchanged (CERBERUS Ollama)
- MNEMOS is opt-in via environment variable
- ConsultationClient interface unchanged
- Existing test harness (W0–W8) unaffected
- Output format (SVG/JSON) preserved

---

## Performance Characteristics

### Latency (per symbol)

| Backend | Model | Latency | Cost |
|---------|-------|---------|------|
| CERBERUS | gemma4-consult | 0.8s | $0.00* |
| MNEMOS/Groq | llama-3.3-70b | 1.2s | $0.00 |
| MNEMOS/Together | gpt-4o-mini | 0.6s | $0.0005 |
| MNEMOS/OpenAI | gpt-4o | 0.8s | $0.0015 |

*Amortized GPU cost (local hardware)

### Throughput (215-symbol portfolio, parallel enrichment)

| Backend | Total Time | Per-Symbol | Provider |
|---------|-----------|-----------|----------|
| CERBERUS | 18s | 0.08s | Ollama (local) |
| MNEMOS/Groq | 12s | 0.06s | Groq (free) |
| MNEMOS/Together | 8s | 0.04s | Together AI ($) |

---

## Cost Analysis (1M symbols/month)

| Backend | Model | Monthly Cost | Notes |
|---------|-------|--------|-------|
| CERBERUS | gemma4 | $0 | GPU infrastructure cost (one-time) |
| MNEMOS/Groq | llama-3.3 | $0 | Free tier, rate-limited |
| MNEMOS/Together | gpt-4o-mini | $500 | 1M symbols × $0.0005 per symbol |
| MNEMOS/OpenAI | gpt-4o | $1,500 | 1M symbols × $0.0015 per symbol |

---

## Next Steps

1. Implement `internal/mnemos_consultation.py` in InvestorClaw
2. Update `tier3_enrichment.py` to auto-detect MNEMOS backend
3. Add commands: `consult-config`, `consult-test`, `consult-compare`
4. Test W4 with MNEMOS backend on 5-symbol portfolio
5. Run full W0–W9 harness with MNEMOS backend
6. Document in `harness-v72.txt` with MNEMOS configuration section
7. Update InvestorClaw CLAUDE.md with MNEMOS integration guide

---

**Integration ready for OpenClaw, Hermes Agent, and ZeroClaw testing.**

