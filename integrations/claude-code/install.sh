#!/usr/bin/env bash
# MNEMOS hook installer — idempotent. Re-running is safe.
#
# Installs three hook scripts under ~/.claude/mnemos-hooks/ and merges the
# matching entries into ~/.claude/settings.json using jq. A config template
# is copied once to ~/.claude/mnemos-hooks.config and never overwritten.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
HOOKS_DIR="$CLAUDE_DIR/mnemos-hooks"
CONFIG_PATH="$CLAUDE_DIR/mnemos-hooks.config"
SETTINGS_PATH="$CLAUDE_DIR/settings.json"

have() { command -v "$1" >/dev/null 2>&1; }

if ! have jq; then
  echo "error: jq is required. Install with: brew install jq  (macOS) or apt install jq  (Debian/Ubuntu)" >&2
  exit 1
fi

mkdir -p "$HOOKS_DIR"

for f in mnemos-session-start.sh mnemos-user-prompt-submit.sh mnemos-stop.sh; do
  cp "$SCRIPT_DIR/hooks/$f" "$HOOKS_DIR/$f"
  chmod +x "$HOOKS_DIR/$f"
done
echo "[ok] copied hook scripts to $HOOKS_DIR"

if [[ ! -f "$CONFIG_PATH" ]]; then
  cp "$SCRIPT_DIR/hooks.config.example" "$CONFIG_PATH"
  chmod 600 "$CONFIG_PATH"
  echo "[new] created $CONFIG_PATH — edit to set MNEMOS_BASE"
else
  echo "[keep] $CONFIG_PATH already exists — not overwritten"
fi

if [[ ! -f "$SETTINGS_PATH" ]]; then
  echo '{}' > "$SETTINGS_PATH"
fi
cp "$SETTINGS_PATH" "$SETTINGS_PATH.bak.$(date +%Y%m%d%H%M%S)"

# Define the desired hook entries as JSON
SESSION_HOOK="$HOOKS_DIR/mnemos-session-start.sh"
PROMPT_HOOK="$HOOKS_DIR/mnemos-user-prompt-submit.sh"
STOP_HOOK="$HOOKS_DIR/mnemos-stop.sh"

# Use jq to add entries to each event group without duplicating by command
jq_script='
def merge_hook(event; cmd):
  .hooks[event] //= [] |
  # Find an existing event group that already contains our command; if none, append one.
  if (.hooks[event] | map(.hooks // []) | flatten | map(.command) | any(. == cmd)) then
    .
  else
    .hooks[event] += [ { "hooks": [ { "type": "command", "command": cmd } ] } ]
  end;

.
| merge_hook("SessionStart";    $session)
| merge_hook("UserPromptSubmit"; $prompt)
| merge_hook("Stop";             $stop)
'

tmp=$(mktemp)
jq \
  --arg session "$SESSION_HOOK" \
  --arg prompt  "$PROMPT_HOOK" \
  --arg stop    "$STOP_HOOK" \
  "$jq_script" \
  "$SETTINGS_PATH" > "$tmp"
mv "$tmp" "$SETTINGS_PATH"
echo "[ok] merged hook entries into $SETTINGS_PATH"

cat <<MSG

MNEMOS hooks installed.

Next steps:
  1. Edit $CONFIG_PATH and set MNEMOS_BASE (and optionally MNEMOS_API_KEY).
  2. Source the config in the Claude Code launch environment, e.g. add to ~/.zshrc or ~/.bashrc:
       [ -f "$CONFIG_PATH" ] && source "$CONFIG_PATH"
  3. Restart Claude Code.

To verify, start a new Claude Code session and check:
  tail -n 20 /tmp/mnemos-hooks.log

To remove the hook entries (leaves scripts + config in place):
  $SCRIPT_DIR/uninstall.sh
MSG
