# mnemosctl

User-facing CLI for MNEMOS memory operations. Companion to `mnemos-rs`
desktop.

**Phase 1 scaffold** — clap subcommand tree only. Network logic lands
in follow-up jobs per `docs/MNEMOSCTL_DESIGN.md`.

## Build

```
cd cli/mnemosctl
cargo build --release
./target/release/mnemosctl --help
```

## Status

| subcommand | impl status |
|---|---|
| `auth login/logout/status/token/server` | stub |
| `search <query>` | stub |
| `get <id>` | stub |
| `list` | stub |
| `store <text>` | stub |
| `update <id>` | stub |
| `delete <id>` | stub |
| `export` | stub |
| `import <file>` | stub |
| `federation peers/peer-add/peer-sync/peer-disable/pull-status` | stub |
| `stats` | stub |
| `doctor` | stub |
| `raw <method> <path>` | stub |

All stubs print `[mnemosctl] TODO: <cmd> not implemented yet` and exit
0 so help/parse paths can be validated against the design.

## Design

See `docs/MNEMOSCTL_DESIGN.md` at repo root.

## Tracked hive jobs

- `mnemos:mnemosctl-cli-design` ✓ done (`docs/MNEMOSCTL_DESIGN.md`)
- `mnemos:mnemosctl-scaffold` ✓ this commit
- `mnemos:mnemosctl-config-loader` ✓ done (separate worker)
- `mnemos:mnemosctl-auth-login` pending
- ... (10 total per design doc §Implementation breakdown)
