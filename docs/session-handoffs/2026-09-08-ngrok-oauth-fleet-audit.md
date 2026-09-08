# Session handoff — 2026-09-08 (ngrok/OAuth MCP + fleet-wide audit)

Long session (Claude Code, `claude-sonnet-5`). Summary for whoever picks
this up next — verify claims below before acting on them, this is a
snapshot, not ground truth.

## 1. Repo topology — corrected this session, important

- **`gitlab.com/ncz-os/mnemos` is authoritative.** Confirmed and now the
  active target for all pushes.
- **ARGONAS `mnemos-production.git` was the wrong base all session** —
  independently diverged from GitLab since an April fork point
  (`d093bc33`): 517 commits unique to it, 1255 unique to GitLab's line,
  GitLab materially more current (last commit 2026-08-21 vs this repo's
  2026-05-18). **Archived** to
  `ARGONAS:/mnt/datapool/git/ARCHIVED/mnemos-production.git.deprecated-20260908`.
- **ARGONAS `mnemos.git`** is the real GitLab mirror (was already wired
  up via `/mnt/datapool/git/gitlab-pull-mirror.sh`, which already listed
  `ncz-os/mnemos` — that script just had **no cron**, now fixed: hourly,
  `17 * * * *`, on ARGONAS root's crontab, logs to
  `/var/log/gitlab-pull-mirror.log`).
- `/opt/mnemos` on PYTHIA (192.168.207.67) now has `argonas` → `mnemos.git`
  and `gitlab` → `git@gitlab.com:ncz-os/mnemos.git` (was pointing at the
  stale renamed `mnemos-os/mnemos` path before tonight).
- **Working GitLab push credential: `$GITLAB_PAT` env var**
  (`~/.zshrc` on STUDIO), NOT `$GITLAB_TOKEN` or `$GITLAB_NCZ_OS_TOKEN` —
  both of those are read-only against the `ncz-os` group and will 401 on
  push. `glab` itself uses `$GITLAB_TOKEN` for reads only.
- **Local `/opt/mnemos` checkout's `master` has its own 517 commits
  (2026-04-20 to 2026-05-18, all Jason Perlow) that exist NOWHERE else**
  — not on ARGONAS's old or new mirror, not on GitLab. Per the operator:
  likely superseded pre-6.x-refactor WIP (the 6.x work all happened on
  GitLab directly), so probably safe to abandon, but not yet confirmed
  by reading the actual diff. Don't delete this local checkout without
  that check.

## 2. Branches pushed to real `gitlab.com/ncz-os/mnemos` tonight (none merged)

- `docs/agents-md-repo-map` — adds `AGENTS.md` at repo root: full map of
  the MNEMOS repo family (which split-out repos are real subsystems vs
  unrelated fleet tooling vs in-tree-only), install extras/bundles,
  audit-scope instructions. Pure docs, zero code risk, safe to merge
  independently of everything else here.
- `feat/oauth-mcp-provider-agnostic` — built off the WRONG base
  (`mnemos-production.git`'s stale `master`, forked well before the
  6.x work). New provider-agnostic OAuth 2.1 authorization server for
  the MCP server (`mnemos/mcp/oauth.py`, `mnemos/mcp/tools/graeae.py`,
  migration `db/migrations_v5_4_0_mcp_oauth.sql`, extensive tests).
  **Do not merge as-is** — `mnemos/mcp/http.py` and
  `mnemos/mcp/tools/__init__.py` exist on GitLab's line too and have
  diverged independently over the 1255-commit gap; this branch's
  versions of those two files will conflict/regress GitLab's real
  current state. The *new* files (oauth.py, graeae.py, migration, tests)
  are confirmed non-conflicting (don't exist on GitLab's line at all).
  **Next step:** rebuild this feature as a fresh branch off GitLab's
  actual current `master`: apply the new files directly, manually
  reconcile the OAuth-related hunks into GitLab's current `http.py` /
  `tools/__init__.py`, re-run the suite there.
- `fix/audit-findings-core` / `fix/audit-findings-mcp-oauth` — fixes for
  7 CONFIRMED findings from an adversarial audit (see §3). Same
  wrong-base caveat as above; same porting approach needed.

Live production status: PYTHIA is currently running the OAuth server
from the (wrong-base) `feat/oauth-mcp-provider-agnostic` branch, fully
verified working end-to-end (real ChatGPT connector registered and
authorized live, see `/etc/mnemos-http-mcp.env` on PYTHIA for config,
admin passphrase there too — do not print it, retrieve directly).
Functionally this is fine to keep running; the "wrong base" problem is
about getting this work landed on the authoritative GitLab line, not
about it being broken in production right now.

## 3. Fleet-wide MNEMOS-ecosystem audit — done, not yet merged anywhere

12 repos audited (Codex `gpt-5.6-terra`), then independently verified
(fresh Opus agents, adversarial — re-read every citation against real
code, reproduced several live) before any fix was attempted:

**80 findings checked, 78 CONFIRMED, 1 REFUTED, 1 UNVERIFIABLE.** All 78
confirmed findings were then fixed (also Opus agents, real tests added,
zoder/MiniMax adversarial review — which caught and required fixes for
real regressions in at least 3 of the 12 passes before merge-readiness).

Findings + verification addenda: ARGONAS repo `mnemos-fleet-audit-findings`
(one `.md` per repo). Fix branches, all named `fix/audit-findings`, one
per repo, pushed to each repo's own ARGONAS bare repo (`origin` remote
there) — **not yet pushed to GitLab, not yet merged anywhere**:
mnemos-bridge-core, mnemos-bridge-openai, mnemos-bridge-anthropic,
mnemos-bridge-gemini, mnemos-bridge-claude-connector, mnemos-bridge-aider,
mnemos-bridge-crewai, mnemos-hot-rs, mnemos-rs, mnemos-stiphos, mnemosctl
(mnemos core's fix landed as the two branches in §2 instead, wrong-base
same as the OAuth work).

Headline findings if you only read one thing per repo:
- **mnemos-bridge-claude-connector**: JWT `sub` claim carried the raw
  upstream MNEMOS API key in plaintext (base64url-decodable) — fixed
  with a server-side opaque-id → key lookup.
- **mnemosctl**: `sync-from` sent the local root Bearer token to any
  user-supplied host, no validation — fixed with a real host allowlist.
- **mnemos-bridge-crewai**: exposed every tool including
  `write_memory`/`delete_memory` with no allowlist, by design (pinned in
  its own tests) — fixed to read-only-by-default, breaking change.
- **mnemos-bridge-aider**: real prompt injection (memory content
  injected as a pre-acknowledged assistant turn) — fixed with the same
  untrusted-data framing mnemos core itself uses.
- **mnemos core** (this repo, on the wrong-base branch): `phase_extract`
  in `mnemos/domain/morpheus/runner.py` had no time window and no LIMIT
  — a bounded `window_hours` run actually processed the entire
  historical backlog synchronously in one HTTP request. **This bug is
  presumably also live on GitLab's current line** since it's a
  logic gap, not something the 6.x refactor obviously would have fixed
  — check `phase_extract` there before assuming it's already handled.

Commit attribution is inconsistent across these 12 fix branches — some
carry the Claude co-author trailer, some don't, depending on whether
that individual agent noticed this session's attribution directive
superseded my (wrong) per-task instruction to omit it. Not yet
standardized.

## 4. Also fixed tonight, unrelated to the above

- **zoder fleet config**: `~/.zoder/config.minimax.toml`'s TYDEUS
  provider blocks only listed the generic alias (`coder`/`reviewer`) in
  `serves`, not the live rotating checkpoint id — broke `-m qwen38`
  style invocations fleet-wide. Fixed and given a real git home for the
  first time: ARGONAS repo `zoder-fleet-config`. Distributed to ARGOS,
  ULTRA, HYDRA, cixmini, PROTEUS, TYDEUS, STUDIO. Verified live on
  ARGOS + STUDIO only; not independently re-verified on the other 5.
- **"MiniMax/zoder is broken fleet-wide" was a false alarm** — multiple
  agents tested MiniMax's native API instead of the OpenAI-compatible
  endpoint zoder actually uses. Full incident + the correct test command
  in `~/.claude/rules/zoder-routing-and-invocation.md` §9-10 and MNEMOS
  memory `mem_1788895828790_e1de7c`.
- ngrok tunnel (`https://articulatory-unintrusive-ashley.ngrok-free.dev`)
  is live on PYTHIA, forwarding to the OAuth-fronted MCP server on
  :5004. `mnemos-mcp-ops` repo on ARGONAS holds the setup scripts.

## 5. Real open items for the next session

1. Port the OAuth branch + the 3 remaining mnemos-core fixes onto
   GitLab's actual current `master` (§2) — the main unfinished piece.
2. Decide the 517 orphaned local-only commits on `/opt/mnemos`'s
   `master` (§1) — read the diff before abandoning.
3. Push the 11 non-core `fix/audit-findings` branches (§3) to each
   repo's real upstream if one exists beyond ARGONAS, and get them
   reviewed/merged.
4. Re-verify the zoder-fleet-config distribution on ULTRA/HYDRA/cixmini/
   PROTEUS/TYDEUS (only ARGOS + STUDIO were actually tested live).
5. Standardize commit attribution across the 12 fix branches if you
   care about consistency (not urgent).
6. `charon.git`, `pantheon.git`, `etlantis.git`, `calliope.git`,
   `clio.git`, `knemon.git` on ARGONAS still need their actual
   relationship to mnemos core confirmed (live-vs-superseded) — flagged
   in `AGENTS.md`'s "unconfirmed relationship" section, not resolved.

## 6. Addendum — REST-surface drift check (found after the above was written)

The new MCP OAuth server is a separate process that proxies to PYTHIA's
LIVE `mnemos-api` container (`ghcr.io/ncz-os/mnemos-enterprise:6.1`), but
was coded by reading the STALE 5.x-era `/opt/mnemos` checkout's route
source — a real risk that the 6.x refactor changed request/response
shapes underneath it. Spot-checked against the live container:

- `memories.*`, `kg.*` routes: structurally identical (same paths,
  methods, response models) despite `memories.py` growing from ~1300 to
  2292+ lines. Low risk.
- `consultations` route: file was renamed/moved (no longer
  `api/routes/consultations.py`, exact new location not found — a sudo
  session expired mid-check), but the route itself is live and accepts
  the same request shape. Currently returns 503
  `"Shared consultation quota storage is unavailable"` — a real,
  **pre-existing, unrelated** infra issue (quota/Redis-shaped backend
  down on PYTHIA), not caused by tonight's work, but worth fixing
  separately.
- **NOT YET CHECKED against the live 6.1 container**: `bulk_create_memories`,
  `branch_memory`/`checkout_memory`/`diff_memory_commits`/`log_memory`
  (DAG/versioning), `list_deletions`, `kronos_anomalies`/`kronos_forecast`,
  `pantheon_list_models`/`pantheon_route_explain`, `recommend_model`,
  `get_stats`. Any of these could have moved/changed shape in the 6.x
  refactor the same way consultations did — don't assume they're fine
  because memories/kg were.
