#!/usr/bin/env bash
# MNEMOS Stop hook — post session transcript to /ingest/session.
#
# Behaviour:
#   - If MNEMOS_BASE is unset, exits silently.
#   - If transcript missing or unreadable, skips.
#   - Failures are logged but never block Stop processing.

set -u

: "${MNEMOS_HOOK_LOG:=/tmp/mnemos-hooks.log}"
: "${MNEMOS_SESSION_CATEGORY:=session_activity}"

log() { printf '[%s] stop: %s\n' "$(date -Iseconds)" "$*" >> "$MNEMOS_HOOK_LOG"; }

if [[ -z "${MNEMOS_BASE:-}" ]]; then
  printf '{}\n'
  exit 0
fi

payload=$(cat)

read -r session_id transcript_path < <(python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("session_id","unknown"), d.get("transcript_path",""))
' <<< "$payload")

if [[ -z "$transcript_path" || ! -r "$transcript_path" ]]; then
  log "no readable transcript — skipping"
  printf '{}\n'
  exit 0
fi

body=$(python3 -c '
import json, sys, os
sid = sys.argv[1]
path = sys.argv[2]
try:
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except Exception:
                pass
    payload = {
        "source": "claude-code",
        "session_id": sid,
        "machine_id": os.uname().nodename,
        "agent_id": "claude-code",
        "raw_data": {"messages": messages},
    }
    print(json.dumps(payload))
except Exception as e:
    sys.stderr.write(f"transcript parse error: {e}\n")
' "$session_id" "$transcript_path" 2>>"$MNEMOS_HOOK_LOG")

if [[ -z "$body" ]]; then
  log "empty transcript body — skipping"
  printf '{}\n'
  exit 0
fi

auth_args=()
[[ -n "${MNEMOS_API_KEY:-}" ]] && auth_args=(-H "Authorization: Bearer $MNEMOS_API_KEY")

curl -sS --max-time 10 \
  -X POST "$MNEMOS_BASE/ingest/session" \
  -H 'Content-Type: application/json' \
  "${auth_args[@]}" \
  -d "$body" >> "$MNEMOS_HOOK_LOG" 2>&1 || log "ingest failed"

log "ingested session $session_id"
printf '{}\n'
