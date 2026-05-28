# mnemosctl — Design

**Status:** design draft 1
**Authored:** 2026-05-23
**Hive job:** `019e55e7-328a` (mnemos:mnemosctl-cli-design)
**Owner:** Studio Claude (per `project_mnemos_desktop_delegation.md`)
**Companion:** `mnemos-rs` desktop client

---

## Purpose

`mnemosctl` is the user-facing CLI for MNEMOS memory operations. Sits
alongside `mnemos-rs` (desktop GUI) as the scriptable / terminal-native
interface. Targets:

- Power users on the fleet (replaces ad-hoc `curl` against `/v1/*`)
- Shell scripts that need to query / store / export memory
- CI pipelines that need to read or push canonical memory state
- Operators managing federation peers

Out of scope: real-time interactive search UI (use `mnemos-rs`),
embedding / vector index admin (lives in server-side migrations),
multi-tenant RBAC management (server admin tool, not user-facing).

## Language + runtime

**Rust**, matching `mnemos-rs`. Reuses the existing `mnemos-rs` core
crates for HTTP client + schema types. Compiled binary distributed via:

- `cargo install mnemosctl` from crates.io (when public)
- fleet binary at `/usr/local/bin/mnemosctl` via deb/rpm + cixmini
  installer (per ncz-os bundling pattern)
- macOS via Homebrew tap

Single static binary, no Python interpreter required. Matches the
"cixmini story: Postgres + NATS only" goal — no extra runtime deps.

Rejected alternative: Python (Click/Typer). Faster to scaffold, but
duplicates the HTTP client work already done in `mnemos-rs`, drags
Python interpreter dependency onto edge boxes (cixmini, bigpi), and
splinters schema types across two languages.

## Command surface

Top-level command tree:

```
mnemosctl
├── auth
│   ├── login               OAuth device-code flow against configured server
│   ├── logout              Clear local token cache
│   ├── status              Show active server + identity
│   ├── token               Print active token (for piping to curl)
│   └── server <url>        Switch active server
├── search <query>          Vector + lexical search
├── get <id>                Fetch single memory by id
├── store <text>            Store new memory (text from stdin if "-")
├── update <id>             Patch existing memory (read body from stdin)
├── delete <id>             Soft-delete (deletion-request flow)
├── list                    List memories (filtered)
├── export                  Export memories to local archive
│   ├── --namespace <ns>
│   ├── --category <cat>
│   ├── --since <date>
│   └── --format jsonl|parquet
├── import <file>           Import memories from archive
├── federation
│   ├── peers               List configured peers
│   ├── peer add <url>      Add peer
│   ├── peer sync <id>      Force-sync a peer now
│   ├── peer disable <id>   Toggle peer enabled flag
│   └── pull-status         Show last_sync / last_error per peer
├── stats                   Server stats (memory count, recent activity)
├── version                 Local + server version
└── doctor                  Health checks (config, auth, server reach)
```

Hidden / advanced subcommands:

- `mnemosctl raw <method> <path>` — call any REST endpoint with active auth
- `mnemosctl schema <table>` — dump JSON schema for a memory type
- `mnemosctl dag` — DAG operations for memory provenance

## Output

Three modes:

1. **Pretty** (default, TTY-detected) — colored, paged, table layouts
2. **JSON** (`--json`) — single document, suitable for `jq`
3. **JSONL** (`--jsonl`) — newline-delimited, suitable for streaming /
   pipes / xargs

Examples:

```
$ mnemosctl search "oracle data guard" --jsonl | jq -r '.id' | xargs -I{} mnemosctl get {}
$ mnemosctl list --category rules --json | jq '.[] | .name'
$ mnemosctl federation peers --json
```

All output formats stable per semver — major version bump if schema
breaks.

## Config

Layered: env > local file > XDG default.

Local file: `$XDG_CONFIG_HOME/mnemosctl/config.toml` (default
`~/.config/mnemosctl/config.toml`).

```toml
[server.default]
url = "http://192.168.207.67:5002"
auth = "token"

[server.default.token]
# read from $MNEMOS_TOKEN env or keyring
storage = "keyring"   # "keyring" | "file" | "env"

[output]
pretty_when_tty = true
default_format = "pretty"   # pretty | json | jsonl

[federation]
default_compat_mode = "strict"
```

Token storage priority order:

1. `--token` flag (one-shot, never persisted)
2. `MNEMOS_TOKEN` env var
3. System keyring (macOS Keychain / Linux secret-service / Windows
   Credential Manager) under service name `mnemosctl`, key
   `{server.url}`
4. `$XDG_CONFIG_HOME/mnemosctl/tokens.json` (0600 file, fallback
   when no keyring available — e.g. CI containers)

**Never write tokens to shell history.** `auth login` uses OAuth
device-code or paste-token-only, no `--token` on CLI for persistence.

## Auth flow

**Phase 1 (now):** bearer token + paste flow.

```
$ mnemosctl auth login
Open https://mnemos.your.host/cli/login in browser.
Paste authorization code: ___
```

Server endpoint `/cli/login` returns a bearer token + token info JSON.
mnemosctl writes to keyring + saves server identity in config.

**Phase 2 (later):** OAuth device-code (matches `mnemos-rs` desktop).
Server emits `/v1/oauth/device` and `/v1/oauth/token` for device-code
grant (RFC 8628).

**Phase 3 (much later):** mTLS for fleet ops accounts.

## Federation peer UX

The `federation` subcommand is THE pain-point this CLI removes. Today
operators run raw `curl` against `/v1/federation/peers`. Examples:

```
$ mnemosctl federation peers
ID                                   NAME      URL                          COMPAT   LAST_SYNC  LAST_ERROR
240f1485-3c7d-428b-b20f-bff45298158d achilles  http://192.168.207.76:5002   permis   2m ago     -
2f7e2cde-c7f1-4dfb-af3a-3287d7c6f332 cerberus  http://192.168.207.96:5003   strict   1h ago     HTTP 500

$ mnemosctl federation peer sync 2f7e2cde-c7f1-4dfb-af3a-3287d7c6f332
syncing... cerberus
  HTTP 500: oracledb DPY-6005 (db disconnected — standby MOUNTED)
sync FAILED (last_error recorded on peer record)

$ mnemosctl federation peer disable 2f7e2cde-c7f1-4dfb-af3a-3287d7c6f332
ok — peer disabled (won't be pulled until re-enabled)
```

## Error handling

Exit codes (matches sysexits.h convention where applicable):

| code | meaning |
|---:|---|
| 0 | success |
| 1 | generic error (bad usage, parse error) |
| 64 | bad CLI args (sysexits EX_USAGE) |
| 65 | bad input data (sysexits EX_DATAERR) |
| 66 | server returned 4xx (auth, not-found, validation) |
| 69 | server returned 5xx (server-side issue) |
| 75 | network / DNS / connection refused |
| 77 | permission denied / 401 |

Errors print to stderr. Pretty mode shows the request id from server
for cross-referencing logs. JSON mode emits `{"error": "...", "code":
N, "request_id": "..."}` to stdout AND non-zero exit.

## Implementation breakdown (follow-up hive jobs to file)

1. `mnemos:mnemosctl-scaffold` P7 — Rust workspace, Cargo manifest,
   `clap` parser, dummy subcommands, CI build matrix (Linux x86,
   Linux arm64, macOS arm64, macOS x86)
2. `mnemos:mnemosctl-config-loader` P7 — TOML config + env layering,
   keyring integration via `keyring-rs`
3. `mnemos:mnemosctl-http-client` P7 — share crate with `mnemos-rs`,
   request id propagation, retry / timeout policy
4. `mnemos:mnemosctl-auth-login` P7 — paste-token Phase 1 flow,
   `auth status / logout / token / server`
5. `mnemos:mnemosctl-search-get-list-store` P8 — core read/write
   operations
6. `mnemos:mnemosctl-federation` P8 — peers + peer add/sync/disable
   subcommand tree
7. `mnemos:mnemosctl-export-import` P6 — JSONL + Parquet archives
8. `mnemos:mnemosctl-doctor` P5 — health checks
9. `mnemos:mnemosctl-fleet-deploy` P6 — deb/rpm + Homebrew tap +
   ncz-os bundling
10. `mnemos:mnemosctl-oauth-device-code` P5 — Phase 2 auth

## Distribution + naming

- Binary name: `mnemosctl` (not `mctl`, not `mnemos` — avoid clash
  with future `mnemos` server binary).
- crates.io publish under same owner as `mnemos-rs` (TBD).
- `mnemos-rs` GUI shells out to `mnemosctl` for non-interactive ops
  where possible — single source of truth for HTTP behavior.
- Fleet hosts get `mnemosctl` via `ncz agent install` bundle (matches
  zeroclaw/openclaw/hermes/ic-engine/mnemosctl as the 5th NCZ agent
  per cixmini deploy story).

## Non-goals

- GUI / curses interface. That is `mnemos-rs` desktop.
- Server admin (user creation, RBAC). Separate `mnemos-admin` tool.
- Embedding model selection / vector index tuning. Server-side concern.
- Multi-account profile switching beyond `auth server <url>`. Single
  active server per shell session; layered via env for parallel use.
