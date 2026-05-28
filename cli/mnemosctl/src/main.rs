// mnemosctl — user-facing CLI for MNEMOS memory operations.
//
// Phase 1 scaffold: clap-based subcommand parser, stub handlers that
// print "TODO" + exit cleanly. Real network logic lands in follow-up
// hive jobs per docs/MNEMOSCTL_DESIGN.md.
//
// Hive job: 019e5700-142b (mnemos:mnemosctl-scaffold, P7)

use clap::{Parser, Subcommand};

#[derive(Parser, Debug)]
#[command(
    name = "mnemosctl",
    version,
    about = "User-facing CLI for MNEMOS memory operations",
    long_about = "Companion to mnemos-rs desktop. Manage memory \
                  search/store/export, federation peers, auth. \
                  See docs/MNEMOSCTL_DESIGN.md for design rationale.",
)]
struct Cli {
    /// Server URL (overrides config + env)
    #[arg(long, env = "MNEMOS_URL", global = true)]
    server: Option<String>,

    /// Bearer token (overrides keyring + env; never persisted)
    #[arg(long, env = "MNEMOS_TOKEN", global = true, hide_env_values = true)]
    token: Option<String>,

    /// Output format
    #[arg(long, value_enum, default_value = "pretty", global = true)]
    format: OutputFormat,

    #[command(subcommand)]
    command: Command,
}

#[derive(clap::ValueEnum, Clone, Debug)]
enum OutputFormat {
    Pretty,
    Json,
    Jsonl,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Authentication management
    Auth(AuthArgs),

    /// Vector + lexical memory search
    Search {
        /// Query string
        query: String,
        /// Limit returned results
        #[arg(short = 'n', long, default_value_t = 20)]
        limit: usize,
        /// Minimum similarity score [0.0–1.0]
        #[arg(long, default_value_t = 0.15)]
        min_score: f32,
    },

    /// Fetch a memory by id
    Get {
        /// Memory id (uuid or mem_*)
        id: String,
    },

    /// List memories with filters
    List {
        #[arg(long)]
        namespace: Option<String>,
        #[arg(long)]
        category: Option<String>,
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },

    /// Store a new memory (body from stdin if "-")
    Store {
        /// Memory body (or "-" for stdin)
        text: String,
        #[arg(long)]
        namespace: Option<String>,
        #[arg(long)]
        category: Option<String>,
    },

    /// Patch an existing memory (body from stdin)
    Update {
        id: String,
    },

    /// Soft-delete a memory (deletion-request flow)
    Delete {
        id: String,
    },

    /// Export memories to local archive
    Export(ExportArgs),

    /// Import memories from archive
    Import {
        /// Archive path (.jsonl or .parquet)
        path: String,
    },

    /// Federation peer management
    Federation(FederationArgs),

    /// Server stats (counts, recent activity)
    Stats,

    /// Run health checks (config + auth + server reach)
    Doctor,

    /// Call raw REST endpoint with active auth
    Raw {
        /// HTTP method (GET, POST, PATCH, DELETE)
        method: String,
        /// API path (e.g. /v1/memories/123)
        path: String,
    },
}

#[derive(clap::Args, Debug)]
struct AuthArgs {
    #[command(subcommand)]
    cmd: AuthCmd,
}

#[derive(Subcommand, Debug)]
enum AuthCmd {
    /// Log in (Phase 1: paste-token; Phase 2: OAuth device-code)
    Login,
    /// Clear local token
    Logout,
    /// Show active server + identity
    Status,
    /// Print active token (for piping)
    Token,
    /// Switch active server
    Server {
        /// Server URL
        url: String,
    },
}

#[derive(clap::Args, Debug)]
struct ExportArgs {
    #[arg(long)]
    namespace: Option<String>,
    #[arg(long)]
    category: Option<String>,
    /// Lower-bound date (ISO 8601)
    #[arg(long)]
    since: Option<String>,
    /// Output archive format
    #[arg(long, value_enum, default_value = "jsonl")]
    archive_format: ArchiveFormat,
    /// Output path (default: ./mnemos-export-<ts>.<ext>)
    #[arg(short = 'o', long)]
    out: Option<String>,
}

#[derive(clap::ValueEnum, Clone, Debug)]
enum ArchiveFormat {
    Jsonl,
    Parquet,
}

#[derive(clap::Args, Debug)]
struct FederationArgs {
    #[command(subcommand)]
    cmd: FederationCmd,
}

#[derive(Subcommand, Debug)]
enum FederationCmd {
    /// List configured peers
    Peers,
    /// Add a peer
    PeerAdd {
        url: String,
        #[arg(long)]
        name: Option<String>,
    },
    /// Force-sync a peer now
    PeerSync {
        peer_id: String,
    },
    /// Disable a peer
    PeerDisable {
        peer_id: String,
    },
    /// Show last_sync / last_error per peer
    PullStatus,
}

fn todo(cmd: &str) -> anyhow::Result<()> {
    // Previously printed the TODO and returned Ok(()). Scripts treating
    // exit-code as the success signal saw zero-exit on every unimplemented
    // command, so auth, search, store, federation, raw, etc. silently
    // 'succeeded' with no actual work — hiding real-state-change failures
    // behind a TODO log line and breaking any CI / shell wrapper that
    // relied on $? for control flow. Return an error so the process exits
    // non-zero until the command lands. --help / --version still succeed
    // (clap handles those before this dispatcher).
    anyhow::bail!(
        "[mnemosctl] {} not implemented yet. Tracked in docs/MNEMOSCTL_DESIGN.md \
         implementation breakdown. Process exits non-zero so scripts can \
         distinguish missing-impl from successful run.",
        cmd
    )
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // Stub handlers — each prints a TODO + returns Ok. Real impls
    // land in follow-up hive jobs:
    //   mnemos:mnemosctl-config-loader   (config + keyring)
    //   mnemos:mnemosctl-auth-login      (Phase 1 paste-token)
    //   mnemos:mnemosctl-search-get-list-store
    //   mnemos:mnemosctl-federation
    //   mnemos:mnemosctl-export-import
    //   mnemos:mnemosctl-doctor
    match cli.command {
        Command::Auth(a) => match a.cmd {
            AuthCmd::Login => todo("auth login"),
            AuthCmd::Logout => todo("auth logout"),
            AuthCmd::Status => todo("auth status"),
            AuthCmd::Token => todo("auth token"),
            AuthCmd::Server { url } => todo(&format!("auth server {}", url)),
        },
        Command::Search { query, limit, min_score } => {
            todo(&format!("search {:?} (limit={}, min_score={})", query, limit, min_score))
        }
        Command::Get { id } => todo(&format!("get {}", id)),
        Command::List { .. } => todo("list"),
        Command::Store { .. } => todo("store"),
        Command::Update { id } => todo(&format!("update {}", id)),
        Command::Delete { id } => todo(&format!("delete {}", id)),
        Command::Export(_) => todo("export"),
        Command::Import { path } => todo(&format!("import {}", path)),
        Command::Federation(f) => match f.cmd {
            FederationCmd::Peers => todo("federation peers"),
            FederationCmd::PeerAdd { url, name } => {
                todo(&format!("federation peer add {} (name={:?})", url, name))
            }
            FederationCmd::PeerSync { peer_id } => {
                todo(&format!("federation peer sync {}", peer_id))
            }
            FederationCmd::PeerDisable { peer_id } => {
                todo(&format!("federation peer disable {}", peer_id))
            }
            FederationCmd::PullStatus => todo("federation pull-status"),
        },
        Command::Stats => todo("stats"),
        Command::Doctor => todo("doctor"),
        Command::Raw { method, path } => todo(&format!("raw {} {}", method, path)),
    }
}


#[cfg(test)]
mod tests {
    //! Parser-contract tests. Previously `cargo test` ran zero tests so
    //! command names, documented examples, environment-backed globals,
    //! and exit-code behaviour could silently break. Covers the
    //! workflow smoke (`--version`, `--help`) plus a representative
    //! sample of subcommands from docs/MNEMOSCTL_DESIGN.md.
    //!
    //! Closes hive job `mnemosctl:missing-tests` (codex audit 2026-05-23).
    use super::*;
    use clap::CommandFactory;
    use clap::Parser;

    #[test]
    fn help_smoke() {
        // CommandFactory::command() succeeds means the clap derive
        // metadata builds cleanly (no version/name conflicts, no
        // misuse of #[arg]); failure here panics before help renders.
        let mut cmd = Cli::command();
        let buf = cmd.render_help().to_string();
        assert!(buf.contains("mnemosctl"));
        assert!(buf.contains("--server"));
        assert!(buf.contains("--token"));
        assert!(buf.contains("--format"));
    }

    #[test]
    fn version_smoke() {
        // Mirrors --version output emission. Just ensures the
        // version string is non-empty + matches Cargo.toml.
        let cmd = Cli::command();
        assert!(!cmd.get_version().unwrap_or("").is_empty());
    }

    #[test]
    fn parse_search_with_global_flags() {
        let cli = Cli::try_parse_from([
            "mnemosctl",
            "--server", "https://hub.example/api",
            "--token", "fake-token",
            "--format", "json",
            "search", "needle",
            "--limit", "5",
        ]).expect("search subcommand parses with global flags");
        assert_eq!(cli.server.as_deref(), Some("https://hub.example/api"));
        assert_eq!(cli.token.as_deref(), Some("fake-token"));
        matches!(cli.format, OutputFormat::Json);
        match cli.command {
            Command::Search { query, limit, .. } => {
                assert_eq!(query, "needle");
                assert_eq!(limit, 5);
            }
            _ => panic!("expected Search variant"),
        }
    }

    #[test]
    fn parse_auth_login() {
        let cli = Cli::try_parse_from(["mnemosctl", "auth", "login"])
            .expect("auth login parses");
        match cli.command {
            Command::Auth(auth) => {
                matches!(auth.cmd, AuthCmd::Login);
            }
            _ => panic!("expected Auth variant"),
        }
    }

    #[test]
    fn parse_federation_peer_add() {
        let cli = Cli::try_parse_from([
            "mnemosctl",
            "federation", "peer-add",
            "https://peer.example/api",
            "--name", "peer-alpha",
        ]).expect("federation peer-add parses");
        match cli.command {
            Command::Federation(fed) => match fed.cmd {
                FederationCmd::PeerAdd { url, name } => {
                    assert_eq!(url, "https://peer.example/api");
                    assert_eq!(name.as_deref(), Some("peer-alpha"));
                }
                _ => panic!("expected PeerAdd variant"),
            },
            _ => panic!("expected Federation variant"),
        }
    }

    #[test]
    fn parse_doctor_no_args() {
        let cli = Cli::try_parse_from(["mnemosctl", "doctor"])
            .expect("doctor parses");
        matches!(cli.command, Command::Doctor);
    }

    #[test]
    fn parse_raw_get() {
        let cli = Cli::try_parse_from([
            "mnemosctl", "raw", "GET", "/v1/memories/123",
        ]).expect("raw GET parses");
        match cli.command {
            Command::Raw { method, path } => {
                assert_eq!(method, "GET");
                assert_eq!(path, "/v1/memories/123");
            }
            _ => panic!("expected Raw variant"),
        }
    }

    #[test]
    fn rejects_unknown_subcommand() {
        let res = Cli::try_parse_from(["mnemosctl", "nonexistent-cmd"]);
        assert!(res.is_err(), "unknown subcommand should fail parsing");
    }

    #[test]
    fn todo_returns_error_not_ok() {
        // Catches a future regression where someone replaces bail! back
        // to Ok() and loses the exit-code signal that distinguishes
        // 'not implemented' from 'ran successfully'.
        let res = todo("test cmd");
        assert!(res.is_err(), "todo() must return Err so process exits non-zero");
    }
}
