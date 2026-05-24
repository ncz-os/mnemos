# Goose patterns research — porting candidates for zeroclaw

**Source clone**: `github.com/block/goose` HEAD (2026-05-24)
**Studied**: ~411 Rust files, focus on `crates/goose/src/{agents,execution,recipe}/`
**Goal**: identify patterns zeroclaw 0.8 lacks that hive workers need

## Top 6 candidate patterns

### 1. `TaskConfig.parent_working_dir: PathBuf`

**Source**: `crates/goose/src/agents/subagent_task_config.rs`

```rust
pub struct TaskConfig {
    pub provider: Arc<dyn Provider>,
    pub parent_session_id: String,
    pub parent_working_dir: PathBuf,   // <-- per-task cwd
    pub extensions: Vec<ExtensionConfig>,
    pub max_turns: Option<usize>,
}
```

**zeroclaw 0.8 gap**: workspace path is per-agent-alias in `[agents.<alias>]` config, not per-task. Multiple concurrent jobs on same agent alias share the workspace → race.

**Port**: shim already does per-job ephemeral workspace via `_prepare_workspace()` (commit 0939676). For upstream parity, zeroclaw could accept `--working-dir` CLI flag override or `--task-config` JSON envelope.

**Upstream PR opportunity**: add `--working-dir <path>` to `zeroclaw agent` CLI; agent treats this as per-invocation cwd override.

---

### 2. Recipe schema (structured job descriptor)

**Source**: `crates/goose/src/recipe/mod.rs:42`

```rust
pub struct Recipe {
    pub version: String,
    pub title: String,
    pub description: String,
    pub instructions: Option<String>,
    pub prompt: Option<String>,
    pub extensions: Option<Vec<ExtensionConfig>>,
    pub settings: Option<Settings>,         // provider+model+temp+max_turns
    pub activities: Option<Vec<String>>,
    pub author: Option<Author>,
    pub parameters: Option<Vec<RecipeParameter>>,
    pub response: Option<Response>,          // json_schema!
    pub sub_recipes: Option<Vec<SubRecipe>>,
    pub retry: Option<RetryConfig>,
}
```

**zeroclaw 0.8 gap**: jobs are only `kind: str` + `description: str` in hive. No structured shape.

**Port**:
- Extend hive `POST /v1/jobs` schema with optional `recipe: Recipe` field.
- Shim parses `recipe.instructions` / `recipe.prompt` / `recipe.settings` (overrides agent default model/provider per task).
- `recipe.response.json_schema` enables structured output (see #3).

**Hive change**: add `recipe` JSON column to `jobs` table.

---

### 3. FinalOutputTool with `json_schema` validation

**Source**: `crates/goose/src/agents/final_output_tool.rs:20`

```rust
if response.json_schema.is_none() {
    panic!("Cannot create FinalOutputTool: json_schema is required");
}
// ...
match jsonschema::validator_for(self.response.json_schema.as_ref().unwrap()) {
    Ok(validator) => {
        // agent's final output validated against schema
    }
}
```

**zeroclaw 0.8 gap**: agent output is freeform text. Caller has to parse + validate manually.

**Port**: when recipe declares `response.json_schema`, shim injects a special "final_output" tool the agent MUST call to terminate. Tool validates payload against schema; rejects + re-prompts on schema fail.

**Hive payoff**: code-emitting jobs return validated `{"commits": [], "files_changed": [], "test_results": {...}}` schema, not raw text.

**Upstream PR opportunity**: zeroclaw could ship a `FinalOutputTool` analog — currently relies on agent self-reporting structure.

---

### 4. `SubRecipe` composition

**Source**: `crates/goose/src/recipe/mod.rs:124`

```rust
pub struct SubRecipe {
    pub name: String,
    pub path: String,
    pub values: Option<HashMap<String, String>>,
    pub sequential_when_repeated: bool,
    pub description: Option<String>,
}
```

**zeroclaw 0.8 gap**: hive has `parent_job_id` for ad-hoc parent-child runtime linking, but no DECLARATIVE composition. A recipe says "first run A, then B with values from A's output, then C in parallel".

**Hive parallel**: this is essentially a DAG-of-jobs. Hive could grow `recipe.sub_recipes` semantics where the orchestrator job spawns sub-jobs.

---

### 5. SubagentRunParams full surface

**Source**: `crates/goose/src/agents/subagent_handler.rs:38`

```rust
pub struct SubagentRunParams {
    pub config: AgentConfig,
    pub recipe: Recipe,
    pub task_config: TaskConfig,
    pub return_last_only: bool,
    pub session_id: String,
    pub cancellation_token: Option<CancellationToken>,
    pub on_message: Option<OnMessageCallback>,
    pub notification_tx: Option<UnboundedSender<ServerNotification>>,
}
```

**zeroclaw 0.8 gap**: `spawn_subagent` per docs supports basic depth-limited spawning, but the production cancellation/message-streaming surface is leaner.

**Port**: shim could expose a `--cancel-token-fd <N>` flag where zeroclaw reads cancellation from a file descriptor. Hive `PATCH /v1/jobs/{id}` with status=cancelled signals worker to write to FD; agent process tree dies cleanly.

---

### 6. AgentManager top-level orchestrator

**Source**: `crates/goose/src/execution/manager.rs:21` (720 LoC)

Manages session lifecycle, cleanup, recovery. Zeroclaw worker shim does this ad-hoc; goose abstracts it.

**Port**: not urgent. Shim is already fairly small; extracting a manager class adds complexity without immediate payoff.

---

## Recommended action priority

### Tier 1 — port to OUR shim (no upstream dep)

1. **Workspace-per-job lifecycle** ✅ SHIPPED in commit `0939676` (mirrors goose's `parent_working_dir`).
2. **Recipe-style description prefix** ✅ PARTIAL — `[repo:URL branch:B base:M]` directive in description is a lightweight Recipe analog. Could grow to `[recipe:...]` with full JSON schema embed.
3. **Structured output capture** — shim already reports `commits[]`, `files_changed[]`. Add schema validation if Recipe declares one.

### Tier 2 — upstream zeroclaw PRs

These need zeroclaw core changes:

1. `zeroclaw agent --working-dir <path>` CLI flag (currently hardcoded per `[agents.<alias>]` config).
2. `zeroclaw agent --cancel-fd <N>` for cooperative cancellation.
3. `[agents.<alias>] working_dir = ...` config field (per-task override).
4. Built-in `final_output` tool with optional JSON-schema validation.

PR target: `github.com/zeroclaw-labs/zeroclaw`. Fork to `github.com/perlowja/zeroclaw` (the existing ncz-os mirror) for staging.

### Tier 3 — hive schema enhancement

1. Add `recipe` JSON column to hive `jobs` table — accepts the goose Recipe shape.
2. `/v1/jobs/{id}/cancel` endpoint that signals worker cancellation FD.
3. `parent_working_dir` per-job in hive payload — overrides shim default.

---

## What's already in zeroclaw 0.8 (no port needed)

From CLAUDE.md + cixmini config inspection:

- `[agents.<alias>]` config — per-agent workspace + model_provider + risk_profile.
- `spawn_subagent` depth-limited.
- `[risk_profiles.<name>]` — workspace_only, auto_approve, level (limited/full).
- ACP session persist.
- Schema V3 + auto-migration.
- Provider fallback REMOVED — one explicit provider per agent.

Don't re-port these.

---

## Next session work

If pursuing upstream PRs, expected sequence:

1. Fork `zeroclaw-labs/zeroclaw` to `perlowja/zeroclaw` (already done per ncz-os mirror).
2. Cut `feat/working-dir-flag` branch.
3. Add `--working-dir` to `zeroclaw agent` CLI in `crates/zeroclaw-cli/src/commands/agent.rs`.
4. Thread through to `zeroclaw_runtime::agent::loop_` as workspace override.
5. Test against fleet (cixmini → other 8 hosts).
6. Open PR with cixmini fleet stats as motivation.

Repeat for cancel-fd + final_output tool.
