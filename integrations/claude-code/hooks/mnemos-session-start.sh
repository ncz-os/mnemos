#!/usr/bin/env bash
# MNEMOS SessionStart hook — emit a compact context block for Claude to ingest.
#
# Behaviour:
#   - If MNEMOS_BASE is unset, exits silently with {} (noop).
#   - If MNEMOS_API_KEY is unset, skips the Authorization header (personal-profile installs).
#   - If MNEMOS is unreachable, logs to $MNEMOS_HOOK_LOG and exits silently with {}.
#
# Input:  JSON on stdin (Claude Code event payload)
# Output: JSON on stdout with additionalContext populated, or {} to noop.

set -u

: "${MNEMOS_HOOK_LOG:=/tmp/mnemos-hooks.log}"
: "${MNEMOS_CONTEXT_LIMIT:=5}"

log() { printf '[%s] session-start: %s\n' "$(date -Iseconds)" "$*" >> "$MNEMOS_HOOK_LOG"; }

# Noop silently when MNEMOS isn't configured — not every session wants memory.
if [[ -z "${MNEMOS_BASE:-}" ]]; then
  log "MNEMOS_BASE unset — skipping"
  printf '{}\n'
  exit 0
fi

auth_args=()
[[ -n "${MNEMOS_API_KEY:-}" ]] && auth_args=(-H "Authorization: Bearer $MNEMOS_API_KEY")

if ! response=$(curl -sS --max-time 3 \
  "${auth_args[@]}" \
  "$MNEMOS_BASE/memories?limit=$MNEMOS_CONTEXT_LIMIT" 2>>"$MNEMOS_HOOK_LOG"); then
  log "MNEMOS unreachable at $MNEMOS_BASE — skipping"
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
    lines = ["## Recent MNEMOS context", ""]
    for m in mems:
        cat = m.get("category", "?")
        content = (m.get("content") or "")[:400].replace("\n", " ")
        lines.append(f"- [{cat}] {content}")
    print("\n".join(lines))
except Exception as e:
    sys.stderr.write(f"parse error: {e}\n")
' 2>>"$MNEMOS_HOOK_LOG")

if [[ -z "$context" ]]; then
  printf '{}\n'
  exit 0
fi

python3 -c '
import json, sys
ctx = sys.stdin.read()
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))
' <<< "$context"

log "injected $(grep -c '^- \[' <<< "$context") memories"
