# Release Checklist

This is the operator-facing per-release checklist. Run through every section
before announcing a tagged release as GA. Some sections are automated (CI,
nightly cron) and just need a green-status verification; others are manual
(chat-UI walkthroughs) and need an operator to drive a real client.

The checklist is split into **server-side** (the `ncz-os/mnemos` repo
itself) and **bridge family** (the `mnemos-bridge-*` repos). Run only the
sections that apply to what's shipping.

---

## Server-side release (`ncz-os/mnemos` x.y.z)

### Pre-merge

- [ ] `pyproject.toml` `version` and `mnemos/_version.py` `__version__` agree.
- [ ] `CHANGELOG.md` has an entry for the new version under the existing
      house style (`## [x.y.z] — YYYY-MM-DD`, `### Added/Fixed/Removed/...`,
      one bullet per user-visible change).
- [ ] Any new SQL migrations are wired into both loaders:
  - [ ] `mnemos/installer/db.py` (Postgres list)
  - [ ] `mnemos/persistence/sqlite.py` (SQLite list)
  - [ ] SQLite mirror file exists in `db/migrations_sqlite/` if the new
        migration uses Postgres-only features (TIMESTAMPTZ, gen_random_uuid,
        TEXT[]). Mirrors translate per the conventions in v5.2.1.
- [ ] `tests/` + `tests/integration*/` all green locally with both
      `--ignore=tests/integration_nats` and the integration-tier suite
      where applicable.
- [ ] `ruff check .` clean.
- [ ] Author email on all commits in the release range is
      `Jason Perlow <jperlow@gmail.com>` (per `~/.claude/CLAUDE.md`
      directive #2 — never `jperlow@nvidia.com` on public OSS).

### Tag + push

- [ ] `git tag -a vx.y.z -m "vx.y.z: <one-line summary>"`.
- [ ] `git push origin master --tags` (gitlab fires CI first; let it
      finish before the other pushes if a CI green is part of the gate).
- [ ] `git push github master --tags`.
- [ ] `GIT_SSH_COMMAND='sshpass -p "${ARGONAS_ROOT_PASS:?set ARGONAS_ROOT_PASS env var; never commit}" ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=no' git push argonas master --tags`.
- [ ] Verify all three remotes converged:
  ```bash
  for r in origin github argonas; do
    case $r in argonas) tip=$(GIT_SSH_COMMAND='sshpass -p "${ARGONAS_ROOT_PASS:?set ARGONAS_ROOT_PASS env var; never commit}" ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=no' git ls-remote $r master | awk '{print $1}');;
      *) tip=$(git ls-remote $r master | awk '{print $1}');;
    esac
    printf "  %-8s %s\n" "$r" "${tip:0:12}"
  done
  ```
  All three should show the same SHA as `git rev-parse HEAD`.

### Image build + fleet roll-out

Skip if the release is docs-only. **This section describes the current
GitHub Actions → ghcr.io pipeline** (`.github/workflows/release-images.yml`).
The image is no longer hand-built with `podman build`/`save`/`scp` — pushing
the tag is what triggers the build; there is nothing to do locally except
watch it and then pull on each host.

- [ ] Push the tag to **both** `origin` (GitLab) and a `github` remote —
      the release workflow only runs on GitHub Actions, not GitLab CI:
      `git push origin vx.y.z && git push github vx.y.z`.
- [ ] Watch the build: `gh run watch <run-id> --repo ncz-os/mnemos --exit-status`
      (find the run id with `gh run list --repo ncz-os/mnemos --limit 1`).
      It builds `linux/amd64` + `linux/arm64` natively (no cross-compile
      emulation — `Dockerfile.core` deliberately refuses it) and publishes
      only `ghcr.io/ncz-os/mnemos-enterprise:x.y.z` (also tagged
      `x.y`, `x`, `latest`, `sha-<short-sha>`). The `mnemos-core` and
      `mnemos` intermediate stages are chained build-context targets and
      are **not** published as their own packages.
- [ ] **If the build fails**: read the real job log
      (`gh api repos/ncz-os/mnemos/actions/jobs/<job-id>/logs`), root-cause
      it, fix it, and cut the **next patch version** — never retag or
      force-push a version tag once pushed (immutable-tag convention). A
      version that published no image gets a one-line CHANGELOG note
      ("published no image and should not be used") and is otherwise left
      alone.
- [ ] Verify the published manifest is genuinely multi-arch before rolling
      it out anywhere:
      ```bash
      GHCR_TOKEN=$(curl -s "https://ghcr.io/token?service=ghcr.io&scope=repository:ncz-os/mnemos-enterprise:pull" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
      curl -s -H "Authorization: Bearer $GHCR_TOKEN" -H "Accept: application/vnd.oci.image.index.v1+json" \
        "https://ghcr.io/v2/ncz-os/mnemos-enterprise/manifests/x.y.z" | python3 -c "
      import json,sys; d=json.load(sys.stdin)
      print([m['platform'] for m in d['manifests'] if m.get('platform',{}).get('os')!='unknown'])"
      ```
      Expect both `{'architecture':'arm64','os':'linux'}` and
      `{'architecture':'amd64','os':'linux'}`.
- [ ] **Validate against a real instance of every backend this release
      touched before broad rollout.** Maintain one staging node per
      supported backend family (Oracle, Db2, MariaDB, SQLite) for this
      purpose. Roll the new version to the affected node first, restart it
      **twice** (migrations replay in full on every start, so idempotency
      only shows up on the second boot), and confirm `/health` stays
      `healthy` with the new `version` both times before rolling further.
      See `docs/PERSISTENCE_ABC_STANDARDIZATION.md` Item 5.
- [ ] Roll the rest of the fleet's federated nodes, then production last:
      pull the new tag, recreate/restart the container in place (same
      volume, same env), confirm `/health` reports the new `version` and
      `database_connected: true`.
- [ ] Run smoke checks across the fleet:
  ```bash
  for h in <host> <host> <host>; do
    ssh -n <user>@$h 'curl -s http://localhost:5002/health' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['version'], d['status'], d['database_connected'])"
  done
  ```
- [ ] **Keep only one version published on ghcr.io at a time** (operator
      policy, 2026-09-13). Once the new version is confirmed healthy
      fleet-wide, delete every prior `mnemos-enterprise` package version —
      both the tagged multi-arch index and its untagged per-platform +
      attestation children (a single release leaves 6 of these; they do
      not get cleaned up automatically when the tagged parent is deleted):
      ```bash
      gh api /orgs/ncz-os/packages/container/mnemos-enterprise/versions \
        --jq '.[] | select(.created_at < "<new-version-created_at>") | .id' \
        | xargs -I{} gh api -X DELETE /orgs/ncz-os/packages/container/mnemos-enterprise/versions/{}
      ```
      Re-verify the manifest step above afterward — deleting the wrong
      version id would silently break the surviving release.

### HA / replication

- [ ] pg-host → gpu-host streaming replication still healthy:
  ```bash
  ssh <user>@<host> "podman exec mnemos-v3x-podman_postgres_1 psql -U mnemos_user -d mnemos -c 'SELECT application_name, state, replay_lag FROM pg_stat_replication;'"
  ```
- [ ] If the release added migrations, confirm they replicated to
      gpu-host automatically:
  ```bash
  ssh <user>@<host> 'podman exec mnemos-standby psql -U mnemos_user -d mnemos -p 5434 -h 127.0.0.1 -c "\dt" | grep <new_table_name>'
  ```

### Bridge tier-2 verification

- [ ] pg-host cron `bridge-tier2-nightly.sh` succeeded last night:
  ```bash
  ssh <user>@<host> 'tail -20 /tmp/bridge-tier2-$(date -u +%Y-%m-%d).log'
  ```
- [ ] Or run it on demand: `ssh <user>@<host> '/usr/local/bin/bridge-tier2-nightly.sh'`.
      All three target APIs should pass — if any fail, the bridge or the
      target SDK has drifted and needs a fix before announcing GA.

---

## Bridge family release (`mnemos-bridge-*` x.y.z)

Use this when releasing one of the per-surface adapter packages or the
shared `mnemos-bridge-core`.

### Pre-merge

- [ ] Bumped version in `pyproject.toml`.
- [ ] CHANGELOG entry for the new version.
- [ ] Tier-1 (offline) tests green: `pytest tests/ -q --ignore=tests/integration`.
- [ ] If shipping `mnemos-bridge-core`: re-run all SIX downstream adapter
      tier-1 tests against the updated core to confirm no API drift broke
      anything. Each adapter is its own git tree; the simplest route is
      `pip install -e /tmp/mnemos-bridge-core /tmp/mnemos-bridge-{openai,gemini,anthropic,aider,crewai,claude-connector}` then
      `for d in /tmp/mnemos-bridge-{openai,gemini,anthropic,aider,crewai,claude-connector}; do (cd $d && pytest tests/ --ignore=tests/integration -q); done`.

### Tier-2 (live model API)

Skip if the adapter has no live model API target (aider sidecar CLI,
crewai offline-only, claude-connector OAuth — those are tier-3 only).

- [ ] OpenAI: `OPENAI_API_KEY=... MNEMOS_TEST_BASE=http://<host>:5003/sse MNEMOS_MCP_TOKEN=... pytest tests/integration -v`
- [ ] Gemini: same with `GOOGLE_API_KEY` set.
- [ ] Anthropic: same with `ANTHROPIC_API_KEY` set.
- [ ] Each tier-2 should complete in <15s. If a test is timing out >30s
      the model API is degraded (or the SDK changed shape); investigate
      before tagging.

### Push to 3 remotes

Same pattern as the server-side push, with the per-bridge gitlab/github
namespace `ncz-os/mnemos-bridge-<name>`. The `/tmp/publish-bridge.sh`
helper script captures the canonical sequence (init + glab create + push +
gh create + push + argonas init + push).

### pg-host refresh (if the cron consumes the new version)

The nightly tier-2 cron uses clones at `/opt/mnemos-bridges/`. If a bridge
just released:

- [ ] `ssh <user>@<host> 'cd /opt/mnemos-bridges/mnemos-bridge-<name> && git pull --quiet && /opt/mnemos-bridges/.venv/bin/pip install --quiet -e .'`
- [ ] Run the cron once manually:
      `ssh <user>@<host> '/usr/local/bin/bridge-tier2-nightly.sh'`
- [ ] Confirm the daily summary memory landed in MNEMOS:
      `curl -s -H 'Authorization: Bearer <token>' "http://<host>:5002/v1/memories/search?subcategory=bridge-tier2&limit=1"`

---

## Tier-3 manual UI walkthroughs (per release, hand-driven)

These cannot be automated — they prove the integration works in the actual
chat UI an operator or end-user lands in. ~5 min per surface; record any
broken flows in the bug tracker before announcing GA.

### Claude Code

- [ ] In a fresh Claude Code session: `python3 ~/.claude/mnemos search 'infrastructure' 5` returns ≥1 result with content.
- [ ] The MCP server `mnemos` shows up in `/mcp` output as a registered server.
- [ ] Asking Claude "search MNEMOS for memories about <recent topic>" produces a tool-use indicator and a coherent answer that references the search hits.

### Claude Desktop

- [ ] `~/Library/Application Support/Claude/claude_desktop_config.json`
      points at the SSH-spawned MCP server.
- [ ] Restart Claude Desktop; ask it the same search query; verify a
      tool-use indicator + correct answer.

### Cursor / Cline / Continue / Codex CLI / Zed

- [ ] Each surface's MCP server registration is intact; the tool list
      includes the canonical 25 MNEMOS tools (fewer if optional extras
      like `pantheon`, `graeae`, or `kronos` aren't installed on that
      backend — see `mnemos/mcp/tools/__init__.py`'s `_TOOL_ORDER`).
- [ ] One search query in each surface produces a result. Spot-check.

### ChatGPT (Pro Developer Mode)

- [ ] Settings → Connectors → Add → MCP server → URL =
      `https://<your-public-mnemos-mcp-host>:5003/sse` + Bearer token.
- [ ] New chat → "Search MNEMOS for memories about infrastructure".
- [ ] Verify ChatGPT shows "Calling search_memories..." and the final
      response references actual MNEMOS data.

### Gemini AI Studio + custom Python harness

AI Studio web has no "register my MCP server" UI today (early 2026).
The realistic path is the `mnemos-bridge-gemini` v0.2.0 adapter:

- [ ] `pip install mnemos-bridge-gemini` on a dev box.
- [ ] Run the included example script that builds a `genai.Client.aio`
      session with `await adapter.gemini_tools()`.
- [ ] Send a tool-using prompt; verify the function_call lands and the
      adapter dispatches it to pg-host.

### Claude.ai Connectors

- [ ] Deploy `mnemos-bridge-claude-connector` behind TLS at a public hostname
      (e.g. `https://mnemos-connector.example.com`).
- [ ] Set `MNEMOS_BACKEND_URL`, `CONNECTOR_JWT_SECRET`,
      `CONNECTOR_PUBLIC_URL` env vars on the host.
- [ ] In Claude.ai → Connectors UI → Add custom connector → URL =
      the hostname above.
- [ ] Walk through OAuth login (paste a pre-issued MNEMOS API key on the
      consent page).
- [ ] Confirm Claude.ai shows the MNEMOS tools in its tool list.
- [ ] Send a search query; verify a tool-use indicator + correct response.

### CrewAI single-agent Crew

- [ ] In a Python script: `pip install mnemos-bridge-crewai crewai` then
      build a single-agent Crew with `tools=await adapter.crewai_tools()`.
- [ ] Kick off a task that should trigger a `search_memories` call.
- [ ] Verify the agent's run log shows the tool was invoked and the
      result was incorporated into the agent's output.

### Aider

- [ ] In a repo, run `mnemos-aider search "<keyword>"`.
- [ ] Paste the output into an Aider prompt and verify Aider takes the
      context into account.
- [ ] If Aider's plugin API is stable enough on the version under test:
      try the Path B integration as well.

### Local runners (Ollama, LM Studio, vLLM)

For each runner that's part of the operator's stack:

- [ ] Configure the runner with `mnemos-bridge-openai` tool definitions
      (the runner is OpenAI-compat).
- [ ] Send a tool-using prompt to the runner.
- [ ] Verify it calls MNEMOS through the bridge and incorporates the
      result.

---

## Post-release announcements

- [ ] Update `gitlab.com/ncz-os/mnemos/-/releases/<vx.y.z>` with the
      CHANGELOG entry as the release notes (gitlab pulls these from
      tag annotations by default; double-check formatting).
- [ ] Same for `github.com/ncz-os/mnemos/releases/<vx.y.z>`.
- [ ] If the release introduces a new dimension, surface, or API,
      cross-link it from `README.md` (the "Works with" or "Bridge family"
      section).
- [ ] If the release closes a 🔵 roadmap item, flip it to ✅ in
      `ROADMAP.md` in the same commit.

---

## Where this lives

- This file: `docs/RELEASE_CHECKLIST.md` in the `ncz-os/mnemos` repo.
- Per-bridge releases follow the same pattern, scoped to that bridge's
  surface — no separate copy needed.
- The fleet helper script for tier-2 cron lives at
  `/usr/local/bin/bridge-tier2-nightly.sh` on pg-host. Source of truth
  is `ops/bridge-tier2-nightly.sh` in this repo. Refresh the deployed
  copy with:
  ```bash
  scp ops/bridge-tier2-nightly.sh <user>@<host>:/tmp/
  ssh <user>@<host> 'sudo install -m 755 /tmp/bridge-tier2-nightly.sh /usr/local/bin/bridge-tier2-nightly.sh'
  ```
- The published `ghcr.io/ncz-os/mnemos-enterprise` image (the one and
  only package published by `release-images.yml`, 2026-09-13+) is built
  from three chained Dockerfiles via Docker Buildx Bake, passed between
  stages as in-memory named contexts so only the final image is pushed:
  `Dockerfile.core` (architecture-specific native extension build, no
  emulation) → `Dockerfile.everything` (all optional extras — morpheus,
  persephone, pantheon, kronos, knossos, apollo, artemis, nats, edge —
  plus the 4 split-out add-on wheels charon/graeae/knemon/pantheon,
  pinned per-overlay in `.github/addons.lock.json`) → `Dockerfile.enterprise`
  (final layer). `Dockerfile.full` and the base `Dockerfile` still exist
  in the repo but are not what the release pipeline builds or the fleet
  runs — do not update `Dockerfile.full` expecting it to affect a release.

*Last updated: 2026-09-13*
