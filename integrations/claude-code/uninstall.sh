#!/usr/bin/env bash
# MNEMOS hook uninstaller — idempotent. Removes only our hook entries from
# ~/.claude/settings.json. Leaves the scripts and config file in place.

set -euo pipefail

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
HOOKS_DIR="$CLAUDE_DIR/mnemos-hooks"
SETTINGS_PATH="$CLAUDE_DIR/settings.json"

have() { command -v "$1" >/dev/null 2>&1; }

if ! have jq; then
  echo "error: jq is required" >&2
  exit 1
fi

if [[ ! -f "$SETTINGS_PATH" ]]; then
  echo "no $SETTINGS_PATH — nothing to uninstall"
  exit 0
fi

cp "$SETTINGS_PATH" "$SETTINGS_PATH.bak.$(date +%Y%m%d%H%M%S)"

tmp=$(mktemp)
jq \
  --arg prefix "$HOOKS_DIR/" \
  '
  def strip(event):
    if .hooks[event] then
      .hooks[event] |= map(
        .hooks |= map(select(.command // "" | startswith($prefix) | not))
      )
      | .hooks[event] |= map(select((.hooks // []) | length > 0))
    else . end;

  .
  | strip("SessionStart")
  | strip("UserPromptSubmit")
  | strip("Stop")
  | if (.hooks | length == 0) then del(.hooks) else . end
  ' "$SETTINGS_PATH" > "$tmp"
mv "$tmp" "$SETTINGS_PATH"

echo "[ok] removed MNEMOS hook entries from $SETTINGS_PATH"
echo "     scripts remain at $HOOKS_DIR"
echo "     config remains at $CLAUDE_DIR/mnemos-hooks.config"
