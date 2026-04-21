#!/usr/bin/env bash
# MNEMOS UserPromptSubmit hook — semantic retrieval, inject top matches.
#
# Behaviour:
#   - If MNEMOS_BASE is unset, exits silently (noop).
#   - Short prompts skipped (below MNEMOS_SEARCH_MIN_CHARS).
#   - API errors / unreachable MNEMOS → log + noop.

set -u

: "${MNEMOS_HOOK_LOG:=/tmp/mnemos-hooks.log}"
: "${MNEMOS_SEARCH_LIMIT:=3}"
: "${MNEMOS_SEARCH_MIN_CHARS:=20}"

log() { printf '[%s] prompt-submit: %s\n' "$(date -Iseconds)" "$*" >> "$MNEMOS_HOOK_LOG"; }

if [[ -z "${MNEMOS_BASE:-}" ]]; then
  printf '{}\n'
  exit 0
fi

payload=$(cat)
prompt=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("prompt",""))' <<< "$payload")

if [[ ${#prompt} -lt $MNEMOS_SEARCH_MIN_CHARS ]]; then
  printf '{}\n'
  exit 0
fi

auth_args=()
[[ -n "${MNEMOS_API_KEY:-}" ]] && auth_args=(-H "Authorization: Bearer $MNEMOS_API_KEY")

query_json=$(python3 -c 'import json,sys; print(json.dumps({"query": sys.argv[1], "limit": int(sys.argv[2]), "semantic": True}))' "$prompt" "$MNEMOS_SEARCH_LIMIT")

if ! response=$(curl -sS --max-time 5 \
  -X POST "$MNEMOS_BASE/memories/search" \
  -H 'Content-Type: application/json' \
  "${auth_args[@]}" \
  -d "$query_json" 2>>"$MNEMOS_HOOK_LOG"); then
  log "search failed — skipping"
  printf '{}\n'
  exit 0
fi

context=$(printf '%s' "$response" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    mems = d.get("memories", [])
    if not mems:
        sys.exit(0)
    lines = ["## Relevant MNEMOS memories", ""]
    for m in mems:
        cat = m.get("category", "?")
        content = (m.get("content") or "")[:500].replace("\n", " ")
        lines.append(f"- [{cat}] {content}")
    print("\n".join(lines))
except Exception:
    pass
')

if [[ -z "$context" ]]; then
  printf '{}\n'
  exit 0
fi

python3 -c '
import json, sys
ctx = sys.stdin.read()
print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}))
' <<< "$context"

log "injected retrieval for prompt: ${prompt:0:50}..."
