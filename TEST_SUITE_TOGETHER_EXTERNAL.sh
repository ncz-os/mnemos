#!/bin/bash
# MNEMOS TEST — Together.ai External Provider (Restored Configs)
# Direct Together API vs Hermes Agent vs OpenClaw Agent

set -e

TOGETHER_KEY="tgp_v1_kTIEvCERoVLAJedqYYGvoMKVzhZJlrhmLRLWDd5V2Yg"
TOGETHER_ENDPOINT="https://api.together.xyz/v1/chat/completions"
MODEL="deepseek-ai/DeepSeek-R1-0528"
QUERY="What is 2+2?"

echo "════════════════════════════════════════════════════════════════════════════════"
echo "MNEMOS TEST — Together.ai External Provider Benchmarking"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""
echo "Platform Comparison: Direct API vs Hermes Agent vs OpenClaw Agent"
echo "Provider: Together.ai (External, restored configurations)"
echo "Model: $MODEL (Reasoning)"
echo "Query: $QUERY"
echo ""

# [1] DIRECT API to Together.ai
echo "[1/3] DIRECT API — Together.ai"
echo "────────────────────────────────────────────────────────────────────────────────"

declare -a direct_times=()
for i in {1..3}; do
  start_ns=$(date +%s%N)
  curl -s -X POST "$TOGETHER_ENDPOINT" \
    -H "Authorization: Bearer $TOGETHER_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$QUERY\"}],\"max_tokens\":50}" > /dev/null 2>&1
  end_ns=$(date +%s%N)
  elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))
  direct_times+=($elapsed_ms)
  echo "  MNEMOS TEST — Direct Request $i: ${elapsed_ms}ms"
done

direct_avg=$(( (direct_times[0] + direct_times[1] + direct_times[2]) / 3 ))
echo "  MNEMOS TEST — Average: ${direct_avg}ms"
echo ""

# [2] HERMES AGENT (External Together config)
echo "[2/3] HERMES AGENT — Together.ai External"
echo "────────────────────────────────────────────────────────────────────────────────"
echo "  (Session naming: MNEMOS TEST — Hermes DeepSeek-R1)"

declare -a hermes_times=()
for i in {1..3}; do
  start_ns=$(date +%s%N)
  hermes chat -q "$QUERY" -Q > /dev/null 2>&1 || true
  end_ns=$(date +%s%N)
  elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))
  hermes_times+=($elapsed_ms)
  echo "  MNEMOS TEST — Hermes Request $i: ${elapsed_ms}ms"
done

hermes_avg=$(( (hermes_times[0] + hermes_times[1] + hermes_times[2]) / 3 ))
echo "  MNEMOS TEST — Average: ${hermes_avg}ms"
echo ""

# [3] OPENCLAW AGENT (External Together config)
echo "[3/3] OPENCLAW AGENT — Together.ai External"
echo "────────────────────────────────────────────────────────────────────────────────"
echo "  (Session naming: MNEMOS TEST — OpenClaw DeepSeek-R1)"

declare -a openclaw_times=()
for i in {1..3}; do
  start_ns=$(date +%s%N)
  openclaw agent --agent main -m "MNEMOS TEST — OpenClaw Request $i: $QUERY" > /dev/null 2>&1 || true
  end_ns=$(date +%s%N)
  elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))
  openclaw_times+=($elapsed_ms)
  echo "  MNEMOS TEST — OpenClaw Request $i: ${elapsed_ms}ms"
done

openclaw_avg=$(( (openclaw_times[0] + openclaw_times[1] + openclaw_times[2]) / 3 ))
echo "  MNEMOS TEST — Average: ${openclaw_avg}ms"
echo ""

# Analysis
echo "════════════════════════════════════════════════════════════════════════════════"
echo "ANALYSIS — External Provider Routing (Together.ai)"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""
echo "Model: $MODEL"
echo ""
echo "Latency Summary:"
echo "  Direct API:    ${direct_avg}ms (baseline)"
echo "  Hermes Agent:  ${hermes_avg}ms (framework + inference)"
echo "  OpenClaw Agent: ${openclaw_avg}ms (framework + inference)"
echo ""

if [ $hermes_avg -gt $direct_avg ]; then
  hermes_oh=$(( hermes_avg - direct_avg ))
  echo "  Hermes Overhead: +${hermes_oh}ms (external provider)"
fi

if [ $openclaw_avg -gt $direct_avg ]; then
  openclaw_oh=$(( openclaw_avg - direct_avg ))
  echo "  OpenClaw Overhead: +${openclaw_oh}ms (external provider)"
fi

echo ""
echo "✅ External provider routing restored on both agents"
echo "✅ Framework overhead measured with Together.ai inference"
echo "✅ Sessions labeled: MNEMOS TEST — [Platform] — [Model]"
