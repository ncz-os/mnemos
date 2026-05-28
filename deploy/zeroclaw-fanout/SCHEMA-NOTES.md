# zeroclaw 0.8 config schema notes (2026-05-24)

Three core insights:

1. **Provider type names**: use `anthropic` (NOT `claude`), `openai`, `gemini`, `groq`, `xai`, `nvidia`, `perplexity`, `together`. Anything else fails with `[dangling_reference]`.

2. **Path shape**: `[providers.models.<TYPE>.<PROFILE>]` — exactly 4 segments. Each table is ONE model. Multiple profiles per type fine.

3. **Agent reference**: `agents.<name>.model_provider = "<TYPE>.<PROFILE>"` — same 2-segment form.

4. **CLI override**: `zeroclaw agent --provider <TYPE>.<PROFILE>` works AT RUNTIME even if config has dangling-ref warning (booting-anyway). Always pass `--provider` from worker shim to bypass.

5. **Risk profile gotchas**:
   - `level = "supervised"` valid (NOT "limited"). 
   - `auto_approve = [...]` is an ARRAY of tool names (NOT boolean true).
   - Default `forbidden_paths = ["/home", "/tmp", ...]` blocks everything. Set `forbidden_paths = []` + `allowed_roots = ["/home", "/tmp"]`.
   - Default `allowed_commands` is very restrictive. Add `bash`, `sh`, `sed`, `awk`, `tee`, `mv`, `cp`, `rm`, etc.
   - `block_high_risk_commands = false` AND `require_approval_for_medium_risk = false` needed for write ops.
   - `level = "full"` enables full filesystem.

6. **Workspace**: Each agent has `~/.zeroclaw/agents/<alias>/workspace/` as its sandboxed root. file_write + shell tools operate there. Per-job code-exec means cloning repo INTO that path before invoking agent.

7. **Per-instance collision**: All workers using same agent alias collide on workspace. Must serialize OR use per-instance aliases (hive-1, hive-2, ...).

8. **Migration**: `zeroclaw config migrate` rewrites file to v3 format BUT mangles `[providers.models.X.Y]` (v1) to `[providers.models.X.default.Y]` (5-segment in v3). Don't trust migrate; author v3 natively.

9. **Provider validation**: `zeroclaw config set` requires v3 schema.
