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
    eprintln!(
        "[mnemosctl] TODO: {} not implemented yet. Tracked in docs/MNEMOSCTL_DESIGN.md \
         implementation breakdown.",
        cmd
    );
    Ok(())
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
