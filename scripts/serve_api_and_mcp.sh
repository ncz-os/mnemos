#!/bin/sh
# Run the API and MCP transports as one supervised container workload.
#
# The API must be ready before the MCP process starts, because MCP uses the
# API's configured persistence and auth state.  Once both are running, a
# failure in either is terminal: exiting lets the container manager restart
# the pair instead of leaving a healthy-looking shell with a dead child.
set -eu

api_pid=""
mcp_pid=""

cleanup() {
    for pid in "$mcp_pid" "$api_pid"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
}

trap cleanup EXIT HUP INT TERM

mnemos serve --host "${MNEMOS_API_HOST:-0.0.0.0}" --port "${MNEMOS_API_PORT:-5002}" &
api_pid=$!

while ! curl -fsS "http://127.0.0.1:${MNEMOS_API_PORT:-5002}/health" >/dev/null 2>&1; do
    if ! kill -0 "$api_pid" 2>/dev/null; then
        wait "$api_pid"
        exit $?
    fi
    sleep 1
done

mnemos serve mcp-http --host "${MNEMOS_MCP_HOST:-0.0.0.0}" --port "${MNEMOS_MCP_PORT:-5004}" &
mcp_pid=$!

while kill -0 "$api_pid" 2>/dev/null && kill -0 "$mcp_pid" 2>/dev/null; do
    sleep 1
done

if ! kill -0 "$api_pid" 2>/dev/null; then
    wait "$api_pid"
    exit $?
fi

wait "$mcp_pid"
