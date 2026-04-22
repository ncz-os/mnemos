#!/bin/bash
# MNEMOS TEST — Together.ai MiniMax Benchmarking
# Direct API (Together) vs MNEMOS (routed to Together)

set -e

TOGETHER_KEY="${TOGETHER_API_KEY}"
TOGETHER_ENDPOINT="https://api.together.xyz/v1/chat/completions"
MNEMOS_ENDPOINT="http://192.168.207.25:5002/v1/chat/completions"
MODEL="mistralai/Mixtral-8x7B-Instruct-v0.1"  # MiniMax alternative available on Together
QUERY="What is 2+2?"

echo "════════════════════════════════════════════════════════════════════════════════"
echo "MNEMOS TEST — Together.ai MiniMax Model Benchmarking"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""
echo "Model: $MODEL"
echo "Query: $QUERY"
echo ""

if [ -z "$TOGETHER_KEY" ]; then
  echo "❌ ERROR: TOGETHER_API_KEY not set"
  echo "   Export your Together.ai API key: export TOGETHER_API_KEY='your-key-here'"
  exit 1
fi

# [1] DIRECT API to Together.ai
echo "[1/2] DIRECT API — Together.ai"
echo "────────────────────────────────────────────────────────────────────────────────"

direct_total=0
for i in {1..3}; do
  start_ns=$(date +%s%N)
  curl -s -X POST "$TOGETHER_ENDPOINT" \
    -H "Authorization: Bearer $TOGETHER_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$QUERY\"}],\"max_tokens\":50}" > /dev/null
  end_ns=$(date +%s%N)
  elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))
  direct_total=$(( direct_total + elapsed_ms ))
  echo "  Request $i: ${elapsed_ms}ms"
done

direct_avg=$(( direct_total / 3 ))
echo "  Average: ${direct_avg}ms"
echo ""

# [2] MNEMOS routing to Together.ai
echo "[2/2] MNEMOS — Together.ai (via MNEMOS proxy)"
echo "────────────────────────────────────────────────────────────────────────────────"
echo "  (Requires Together configured as provider in MNEMOS)"
echo "  Waiting for configuration..."
echo ""

echo "════════════════════════════════════════════════════════════════════════════════"
echo "RESULTS"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""
echo "Direct Together API: ${direct_avg}ms average"
echo ""
echo "Next: Configure MNEMOS to proxy Together provider and re-run test"
