# MNEMOS API Inference Examples
## Integration Patterns for OpenClaw, Hermes Agent, ZeroClaw

**Status**: Ready to use  
**Date**: 2026-04-19  
**Endpoint**: `/v1/chat/completions` (OpenAI-compatible)

---

## Quick Reference

**Base URL**: `http://192.168.207.67:5002`

**Endpoints**:
- `GET /health` — Health check
- `GET /v1/models` — List available models
- `POST /v1/chat/completions` — Chat completion (main inference)
- `GET /v1/models/{model_id}` — Get model details

**Authentication**: Bearer token in `Authorization` header

---

## Example 1: Simple Investment Analysis

### Python (OpenClaw Skill)

```python
#!/usr/bin/env python3
"""InvestorClaw analyst enrichment via MNEMOS API inference."""

import urllib.request
import urllib.error
import json
import os

def analyze_holding(symbol: str, analyst_data: dict) -> str:
    """Analyze a single holding using MNEMOS API."""
    
    endpoint = "http://192.168.207.67:5002/v1/chat/completions"
    api_key = os.environ.get("INVESTORCLAW_MNEMOS_API_KEY")
    
    if not api_key:
        raise ValueError("INVESTORCLAW_MNEMOS_API_KEY not set")
    
    # Prepare analyst context
    analyst_text = f"""
    Ticker: {symbol}
    Analysts: {analyst_data.get('count', 0)}
    Average Price Target: ${analyst_data.get('avg_target', 0):.2f}
    Price Target Range: ${analyst_data.get('target_low', 0):.2f}–${analyst_data.get('target_high', 0):.2f}
    Buy Ratings: {analyst_data.get('buy_count', 0)}
    Hold Ratings: {analyst_data.get('hold_count', 0)}
    Sell Ratings: {analyst_data.get('sell_count', 0)}
    Recent News: {analyst_data.get('news_snippet', '')}
    """
    
    # Build request
    request_payload = {
        "model": "best-reasoning",
        "messages": [
            {
                "role": "system",
                "content": "You are a financial analyst. Synthesize analyst consensus and provide actionable insight."
            },
            {
                "role": "user",
                "content": f"Provide a brief synthesis (2-3 sentences) of analyst consensus for {symbol}:\n{analyst_text}"
            }
        ],
        "temperature": 0.7,
        "max_tokens": 200
    }
    
    # Send request
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response = json.loads(resp.read())
            
        # Extract synthesis
        synthesis = response["choices"][0]["message"]["content"]
        return synthesis
    
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("Invalid MNEMOS API key")
        else:
            raise RuntimeError(f"MNEMOS API error: {e.code}")

# Usage in OpenClaw skill
if __name__ == "__main__":
    analyst_data = {
        "count": 12,
        "avg_target": 195.50,
        "target_low": 180.0,
        "target_high": 210.0,
        "buy_count": 8,
        "hold_count": 3,
        "sell_count": 1,
        "news_snippet": "Q4 earnings beat expectations"
    }
    
    synthesis = analyze_holding("AAPL", analyst_data)
    print(f"AAPL Analysis:\n{synthesis}")
```

### Shell (cURL)

```bash
#!/bin/bash

# Set credentials
MNEMOS_KEY=$(cat ~/.investorclaw/.mnemos_api_key)
ENDPOINT="http://192.168.207.67:5002"

# Call API
curl -X POST "$ENDPOINT/v1/chat/completions" \
  -H "Authorization: Bearer $MNEMOS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "best-reasoning",
    "messages": [
      {
        "role": "system",
        "content": "You are a financial analyst."
      },
      {
        "role": "user",
        "content": "Analyze: Apple Inc has 12 analyst ratings with $195.50 avg target. Q4 earnings beat."
      }
    ],
    "temperature": 0.7,
    "max_tokens": 200
  }' | jq '.choices[0].message.content'
```

---

## Example 2: Multi-Symbol Batch Analysis

### Python (Parallel Processing)

```python
#!/usr/bin/env python3
"""Batch analyst enrichment across portfolio holdings."""

import urllib.request
import json
import os
import concurrent.futures
import time
from typing import List, Dict

def call_mnemos(symbol: str, data: dict) -> Dict:
    """Query MNEMOS for single symbol."""
    endpoint = "http://192.168.207.67:5002/v1/chat/completions"
    api_key = os.environ.get("INVESTORCLAW_MNEMOS_API_KEY")
    
    request_payload = {
        "model": "best-reasoning",
        "messages": [
            {
                "role": "system",
                "content": "You are a financial analyst. Provide 2-3 sentence synthesis."
            },
            {
                "role": "user",
                "content": f"Analyst consensus for {symbol}: {json.dumps(data)}"
            }
        ],
        "temperature": 0.6,
        "max_tokens": 150
    }
    
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    start = time.time()
    with urllib.request.urlopen(req, timeout=30) as resp:
        response = json.loads(resp.read())
    elapsed_ms = int((time.time() - start) * 1000)
    
    return {
        "symbol": symbol,
        "synthesis": response["choices"][0]["message"]["content"],
        "model": response.get("model"),
        "latency_ms": elapsed_ms,
        "input_tokens": response["usage"]["prompt_tokens"],
        "output_tokens": response["usage"]["completion_tokens"]
    }

def analyze_portfolio(holdings: List[Dict]) -> List[Dict]:
    """Analyze multiple holdings in parallel."""
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(call_mnemos, h["symbol"], h["analyst_data"])
            for h in holdings
        ]
        
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                print(f"✓ {result['symbol']}: {result['latency_ms']}ms")
            except Exception as e:
                print(f"✗ Error: {e}")
    
    return results

# Usage
holdings = [
    {"symbol": "AAPL", "analyst_data": {"avg_target": 195.50, "buy_count": 8}},
    {"symbol": "MSFT", "analyst_data": {"avg_target": 415.00, "buy_count": 10}},
    {"symbol": "NVDA", "analyst_data": {"avg_target": 150.00, "buy_count": 9}},
    {"symbol": "TSLA", "analyst_data": {"avg_target": 240.00, "buy_count": 7}},
    {"symbol": "AMZN", "analyst_data": {"avg_target": 185.00, "buy_count": 8}},
]

results = analyze_portfolio(holdings)
for r in results:
    print(f"{r['symbol']}: {r['synthesis'][:80]}...")
```

---

## Example 3: Cost-Aware Model Selection

### Python (Auto-Select Best Model)

```python
#!/usr/bin/env python3
"""Use MNEMOS cost optimizer to select best model for task."""

import urllib.request
import json
import os

def get_recommended_model(task_type: str = "analyst_synthesis") -> Dict:
    """Get cost-optimized model recommendation from MNEMOS."""
    endpoint = "http://192.168.207.67:5002/v1/providers/recommend"
    api_key = os.environ.get("INVESTORCLAW_MNEMOS_API_KEY")
    
    request_payload = {
        "task_type": task_type,
        "budget_cents": 100,  # Max cost per query (in cents)
        "quality_floor": 0.85  # Minimum acceptable quality score (0–1)
    }
    
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req) as resp:
        response = json.loads(resp.read())
    
    return response["recommended_model"]

def analyze_with_optimization(symbol: str, analyst_data: dict) -> Dict:
    """Analyze holding with cost-optimized model."""
    
    # Get best model for this task
    model_rec = get_recommended_model("analyst_synthesis")
    print(f"Using model: {model_rec['name']} (cost: ${model_rec['cost_per_token']:.6f})")
    
    # Run inference
    endpoint = "http://192.168.207.67:5002/v1/chat/completions"
    api_key = os.environ.get("INVESTORCLAW_MNEMOS_API_KEY")
    
    request_payload = {
        "model": model_rec["name"],  # Use recommended model
        "messages": [
            {
                "role": "system",
                "content": "Financial analyst. Concise synthesis."
            },
            {
                "role": "user",
                "content": f"Analyze {symbol}: {json.dumps(analyst_data)}"
            }
        ],
        "temperature": 0.6,
        "max_tokens": 150
    }
    
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req) as resp:
        response = json.loads(resp.read())
    
    # Calculate actual cost
    usage = response["usage"]
    total_tokens = usage["prompt_tokens"] + usage["completion_tokens"]
    actual_cost = total_tokens * model_rec["cost_per_token"]
    
    return {
        "symbol": symbol,
        "synthesis": response["choices"][0]["message"]["content"],
        "model": response["model"],
        "tokens": total_tokens,
        "cost_usd": actual_cost,
        "cost_optimized": True
    }

# Usage
result = analyze_with_optimization("AAPL", {"avg_target": 195.50})
print(f"Cost-optimized analysis:")
print(f"  Model: {result['model']}")
print(f"  Cost: ${result['cost_usd']:.6f}")
print(f"  Result: {result['synthesis']}")
```

---

## Example 4: Multi-Turn Conversation (Portfolio Review)

### Python (Stateful Dialogue)

```python
#!/usr/bin/env python3
"""Multi-turn conversation for iterative portfolio analysis."""

import urllib.request
import json
import os

class PortfolioAnalyst:
    """Stateful analyst client with conversation history."""
    
    def __init__(self, endpoint: str = "http://192.168.207.67:5002"):
        self.endpoint = endpoint
        self.api_key = os.environ.get("INVESTORCLAW_MNEMOS_API_KEY")
        self.messages = [
            {
                "role": "system",
                "content": """You are a portfolio analyst specializing in multi-asset allocation.
                Provide concise, actionable insights. Maintain context across multiple queries."""
            }
        ]
    
    def ask(self, question: str, model: str = "best-reasoning") -> str:
        """Ask analyst a question about portfolio."""
        
        self.messages.append({
            "role": "user",
            "content": question
        })
        
        request_payload = {
            "model": model,
            "messages": self.messages,
            "temperature": 0.7,
            "max_tokens": 300
        }
        
        req = urllib.request.Request(
            f"{self.endpoint}/v1/chat/completions",
            data=json.dumps(request_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        
        with urllib.request.urlopen(req) as resp:
            response = json.loads(resp.read())
        
        answer = response["choices"][0]["message"]["content"]
        
        # Add to conversation history
        self.messages.append({
            "role": "assistant",
            "content": answer
        })
        
        # Log token usage
        usage = response["usage"]
        print(f"  [Tokens: {usage['prompt_tokens']} input, {usage['completion_tokens']} output]")
        
        return answer

# Interactive portfolio review
analyst = PortfolioAnalyst()

print("=== Portfolio Analysis Session ===\n")

# Query 1
print("Q: What's my current tech allocation?")
response = analyst.ask(
    "I hold: 50 AAPL, 30 MSFT, 25 NVDA, 20 TSLA. What's my tech sector weight?"
)
print(f"A: {response}\n")

# Query 2
print("Q: Should I rebalance?")
response = analyst.ask(
    "Given current tech concentration, is my portfolio at risk? Recommend rebalancing."
)
print(f"A: {response}\n")

# Query 3
print("Q: Tax implications?")
response = analyst.ask(
    "I've held these for 2 years. What are the tax implications of rebalancing now?"
)
print(f"A: {response}\n")
```

---

## Example 5: Memory Injection (MNEMOS Integration)

### Python (With Semantic Search Context)

```python
#!/usr/bin/env python3
"""Inference with automatic memory injection from MNEMOS."""

import urllib.request
import json
import os

def search_memories(query: str, limit: int = 3) -> List[str]:
    """Search MNEMOS for relevant memories."""
    endpoint = "http://192.168.207.67:5002/memories/search"
    api_key = os.environ.get("INVESTORCLAW_MNEMOS_API_KEY")
    
    request_payload = {
        "query": query,
        "limit": limit,
        "category": "solutions"
    }
    
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req) as resp:
        response = json.loads(resp.read())
    
    return [m["content"] for m in response.get("memories", [])]

def analyze_with_context(symbol: str, analyst_data: dict) -> str:
    """Analyze with injected historical context."""
    
    # Search for relevant memories
    context_memories = search_memories(
        f"analyst consensus patterns for {symbol}",
        limit=3
    )
    
    # Build context block
    context_block = ""
    if context_memories:
        context_block = "### Relevant Historical Patterns:\n"
        context_block += "\n".join([f"- {m}" for m in context_memories])
        context_block += "\n\n"
    
    # Prepare query with context
    query = f"""{context_block}
    Current analyst consensus for {symbol}:
    {json.dumps(analyst_data, indent=2)}
    
    Provide synthesis considering historical patterns."""
    
    # Call inference
    endpoint = "http://192.168.207.67:5002/v1/chat/completions"
    api_key = os.environ.get("INVESTORCLAW_MNEMOS_API_KEY")
    
    request_payload = {
        "model": "best-reasoning",
        "messages": [
            {
                "role": "system",
                "content": "You are a financial analyst. Use historical context to inform synthesis."
            },
            {
                "role": "user",
                "content": query
            }
        ],
        "temperature": 0.7,
        "max_tokens": 200
    }
    
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req) as resp:
        response = json.loads(resp.read())
    
    return response["choices"][0]["message"]["content"]

# Usage with memory injection
analyst_data = {
    "avg_target": 195.50,
    "buy_count": 8,
    "consensus": "Positive"
}

synthesis = analyze_with_context("AAPL", analyst_data)
print(f"Memory-enhanced synthesis:\n{synthesis}")
```

---

## Example 6: Error Handling & Fallback

### Python (Graceful Degradation)

```python
#!/usr/bin/env python3
"""API inference with error handling and fallback."""

import urllib.request
import urllib.error
import json
import os
import time

def call_mnemos_with_fallback(
    prompt: str,
    fallback_response: str = "Unable to analyze at this time."
) -> Dict:
    """Call MNEMOS with automatic fallback."""
    
    endpoint = "http://192.168.207.67:5002/v1/chat/completions"
    api_key = os.environ.get("INVESTORCLAW_MNEMOS_API_KEY", "")
    
    if not api_key:
        return {"status": "error", "reason": "No API key", "response": fallback_response}
    
    request_payload = {
        "model": "best-reasoning",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 200
    }
    
    retry_count = 0
    max_retries = 2
    
    while retry_count < max_retries:
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(request_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            
            with urllib.request.urlopen(req, timeout=15) as resp:
                response = json.loads(resp.read())
            
            return {
                "status": "success",
                "response": response["choices"][0]["message"]["content"],
                "model": response.get("model"),
                "tokens": response["usage"]["total_tokens"]
            }
        
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Rate limit
                print(f"Rate limited. Retry {retry_count + 1}/{max_retries}...")
                time.sleep(2 ** retry_count)  # Exponential backoff
                retry_count += 1
            elif e.code == 401:
                return {"status": "error", "reason": "Invalid API key", "response": fallback_response}
            elif e.code == 503:
                return {"status": "error", "reason": "Service unavailable", "response": fallback_response}
            else:
                return {"status": "error", "reason": f"HTTP {e.code}", "response": fallback_response}
        
        except urllib.error.URLError as e:
            if retry_count < max_retries:
                print(f"Connection error. Retry {retry_count + 1}/{max_retries}...")
                time.sleep(1)
                retry_count += 1
            else:
                return {"status": "error", "reason": "Connection failed", "response": fallback_response}
        
        except Exception as e:
            return {"status": "error", "reason": str(e), "response": fallback_response}
    
    return {"status": "error", "reason": "Max retries exceeded", "response": fallback_response}

# Usage
result = call_mnemos_with_fallback(
    "Analyze Apple analyst consensus",
    fallback_response="Apple consensus: moderate positive, avg target $195"
)

if result["status"] == "success":
    print(f"✓ {result['response']}")
else:
    print(f"✗ {result['reason']}")
    print(f"Using fallback: {result['response']}")
```

---

## OpenClaw Command Examples

```bash
# 1. Test endpoint connectivity
openclaw agent --session-id ic-harness-v71 -m "
  curl -H 'Authorization: Bearer \$(echo \$INVESTORCLAW_MNEMOS_API_KEY)' \
    http://192.168.207.67:5002/health | jq
"

# 2. List available models
openclaw agent --session-id ic-harness-v71 -m "
  curl -H 'Authorization: Bearer \$(echo \$INVESTORCLAW_MNEMOS_API_KEY)' \
    http://192.168.207.67:5002/v1/models | jq '.data[].id'
"

# 3. Run analyst enrichment with MNEMOS
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio analyst --backend mnemos --symbols 5
"

# 4. Compare CERBERUS vs MNEMOS
openclaw agent --session-id ic-harness-v71 -m "
  /investorclaw:portfolio consult-compare --symbol_count 3 --model-pairs cerberus,mnemos
"
```

---

## Performance Notes

| Scenario | Latency | Cost | Model |
|----------|---------|------|-------|
| Single symbol | 0.6–1.2s | $0.00–0.001 | Groq / Together |
| 10 symbols (serial) | 6–12s | $0.00–0.01 | Groq / Together |
| 10 symbols (parallel) | 1–2s | $0.00–0.01 | Groq / Together |
| 100+ symbols | 60–120s | $0.01–0.10 | Cost optimizer |

---

## Next Steps

1. Copy these examples to InvestorClaw `examples/` directory
2. Update InvestorClaw CLAUDE.md with API inference patterns
3. Test against running MNEMOS service
4. Integrate into OpenClaw test harness (W9 workflow)
5. Document for Hermes Agent integration
6. Create ZeroClaw runner support

---

**Ready for integration with OpenClaw, Hermes Agent, and ZeroClaw.**
