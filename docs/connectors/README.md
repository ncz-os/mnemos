# MNEMOS Connectors — experimental

> **Status: experimental.** This subsystem is published for power users and
> developers who want MNEMOS as a memory layer behind their existing agent
> tooling (Claude Desktop, Claude Code, ChatGPT Pro Developer Mode, Cursor,
> Codex CLI). Defaults are off; configuration is opt-in; surface area is
> intentionally narrow. APIs may change between minor releases without a
> deprecation cycle until the surface is promoted to `stable` in v3.5+.

## Audience

MNEMOS is a power-user / developer memory operating system. The connector
work makes its existing MCP surface usable from agent surfaces beyond
Claude Code (the original deployment target). It does not target
non-technical end users — that's a different problem space (see
`docs/positioning.md`).

If you fit this profile, the connectors are for you:

- You run MNEMOS yourself (homelab, dev box, NAS, cloud VM, or a fleet
  alongside the rest of your infra).
- You use multiple coding agents day-to-day (Claude, ChatGPT Pro,
  Cursor, Codex) and want them to share memory.
- You're comfortable with concepts like reverse tunnels, bearer auth,
  TLS termination, and editing config files.

If you're looking for a one-click consumer memory app: that's not what
MNEMOS is. We recommend [MemPalace](https://github.com/mempalace/mempalace)
for the local-first single-user Claude-Desktop experience; MNEMOS is
deliberately scaled differently. The two compose via the
[KNOSSOS bridge](../KNOSSOS.md) when you want both.

## Surfaces supported

| Agent surface | Transport | Status | Notes |
|---|---|---|---|
| Claude Code | stdio MCP | ✅ stable | The original target; works out of the box |
| Claude Desktop | stdio MCP or HTTP/SSE | ✅ stable (stdio) / 🧪 experimental (HTTP) | Stdio for local; HTTP/SSE if you want the same MNEMOS to back multiple machines |
| Cursor | stdio MCP or HTTP/SSE | ✅ stable | Cursor's MCP support is mature |
| Codex CLI (OpenAI) | stdio MCP or HTTP/SSE | 🧪 experimental | Codex 0.125.0+ has MCP; we test against 0.126.0-alpha.1 |
| ChatGPT Pro Developer Mode (web) | HTTP/SSE | 🧪 experimental | Requires the Pro / Team / Enterprise / Edu tier with Developer Mode enabled, plus a public HTTPS URL pointing at your MNEMOS |
| ChatGPT consumer (free / Plus) | none | ❌ not supported | OpenAI hasn't broadened MCP to those tiers; no plan to ship a non-MCP shim for them |

## Quick start

### If you already have MNEMOS running locally and just want stdio MCP

For Claude Code, Claude Desktop, Cursor, or Codex CLI on the same
machine as MNEMOS — no tunnel needed, the agent spawns MNEMOS's MCP
server as a child process. See the per-surface guides:

- [Claude Code](./claude-code.md)
- [Claude Desktop](./claude-desktop.md)
- [Cursor](./cursor.md)
- [Codex CLI](./codex-cli.md)

### If you want ChatGPT Pro / Team to talk to your MNEMOS

ChatGPT's web app needs a public HTTPS URL — it can't spawn local
processes. You expose MNEMOS's MCP HTTP/SSE endpoint via a tunnel,
register it as a Custom Connector, paste the bearer token. See:

- [ChatGPT Pro Developer Mode](./chatgpt-pro-developer-mode.md) — full
  walkthrough including ngrok setup and the experimental
  `mnemos-tunnel-setup` helper script.

### Mobile / laptop tether to a home or SOHO MNEMOS

The "MNEMOS Lite" laptop edition (planned v4.0) runs a single-tenant
SQLite-backed MNEMOS locally and tethers to your authoritative
MNEMOS on a server via federation. Same MCP surface, offline-tolerant,
conflict resolution via the existing version DAG. Until lite ships,
power users use SSH port-forwarding or Tailscale to point a local
agent at a remote MNEMOS — the MCP server doesn't care which transport
delivers the bytes.

## Why we publish these as experimental

Three reasons:

1. **The remote-MCP story is new** in the broader ecosystem. ChatGPT
   Pro Developer Mode landed recently; Codex CLI's MCP shipped in
   0.125; Claude Desktop's HTTP transport is in flux. Anything we
   publish here may need changes when upstream surfaces stabilize.
2. **The audience is narrow on purpose**. We're not going to spend
   2026 building an installer-app for the consumer market — that's a
   different product with a different operations footprint. The
   connectors targeting that market (a hosted SaaS, a Tauri desktop
   app) aren't on the roadmap. See `ROADMAP.md` for the v4.x charter.
3. **We are not trying to displace MemPalace, OpenWebUI, Mem0, Letta,
   Graphiti, or Cognee**. Each of those serves a real audience well.
   MNEMOS exists for users who outgrew them or whose workload —
   multi-tenant, production-data-rate, schema-extensible, audit-and-
   rollback grade — was never their target. Connector publication is
   about making MNEMOS easy to wire into the agent surfaces that
   people in our audience already use, not about market displacement.

## Stability commitments

While `experimental`:

- Endpoints under `/admin/tunnels/*` may be renamed, restructured, or
  withdrawn in any minor release.
- Default ports (5004 for the MCP HTTP/SSE bridge) may change.
- Bearer-auth model is the v1 baseline; OAuth + per-user tokens come
  in a later iteration without backwards compatibility for the v1
  shared-token mode.
- The `mnemos-tunnel-setup` script's argument shape and config-file
  location (`~/.mnemos/tunnel.toml`) may change.

When the connector subsystem promotes to `stable` (target: v3.5),
those guarantees flip — semver applies, deprecation cycles apply.
