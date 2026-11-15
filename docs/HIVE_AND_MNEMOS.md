# Hive and MNEMOS

Hive is the agent job queue and coordination service in the NCZ-OS project family. It lives in
[graeae-hive](https://gitlab.com/ncz-os/graeae-hive). It was previously distributed as STIPHOS
(`mnemos-stiphos`, now archived). MNEMOS and Hive are separate projects: each can be used on its own.

## We strongly recommend running them together

Hive works without MNEMOS: jobs are still queued, picked up, checked and merged. We strongly recommend running them together, because MNEMOS is where the stack keeps its memory. When Hive is pointed at MNEMOS (`MNEMOS_URL` and `MNEMOS_TOKEN`), a job's brief tells the coding agent to search for what earlier attempts on that job found (`zoder mnemos --search`) and to record its progress as it goes (`zoder mnemos --record`). Hive reads those saved loop events to work out why a job ended. Its dashboard shows the status of every MNEMOS server, and its MCP bridge exposes Hive and MNEMOS through one MCP server. With `MNEMOS_URL` unset, Hive turns its MNEMOS features off and logs one line. Without MNEMOS, every attempt starts from its brief alone, and nothing is remembered between attempts or sessions.

## Connecting them

Give Hive the address and token of your MNEMOS server in its environment: `MNEMOS_URL` and `MNEMOS_TOKEN`.
To list several MNEMOS servers on the Hive dashboard, set `HIVE_MNEMOS_SERVERS_FILE`.

## What this page is based on

Read from graeae-hive `deploy/integrate-20261017` at 60c6987f on 2026-11-15:
`deploy/hive_worker.py` (the job brief), `hive/workers/loop_outcome.py` (reads saved loop events),
`hive/bus/routers/mnemos_servers.py` (the dashboard panel), `hive/mcp/bridge.py` (the MCP bridge) and
`hive/core/config.py` (the switch-off). Not measured: how much the memory improves job outcomes.
