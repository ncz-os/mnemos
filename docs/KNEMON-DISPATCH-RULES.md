# KNEMON Dispatch Rules

Audit date: 2026-05-28

## Verdict

The pre-audit `subscription_plans` catalog was not current. It still carried ChatGPT Plus at 40 messages per 3h, ChatGPT Pro as a weekly 200-token-style cap, Claude Max 100 at 450 messages per 5h, and speculative Claude post-2026-06-15 rows. Migration `0039_knemon_dispatch_rule_refresh` refreshes the active rows and keeps Codex separate from ChatGPT so workspace pools can route correctly. OpenAI now publishes both Codex included-message ranges and credit rate-card guidance, so the Codex `msg_cap` rows are conservative local planning caps for KNEMON headroom, not provider-published universal billing units.

The fallback order is correct when implemented as:

1. Free auth.
2. Subscription auth with utilization below `knemon.subscription_preferred_utilization_pct` (default `70.0`).
3. API/token auth with estimated request cost strictly below `knemon.low_priority_api_cost_ceiling_usd` (default `$0.50`).
4. API/token auth at or above the cheap-API ceiling.
5. Other auth methods.

The `$0.50` boundary is intentionally not cheap API. Values `< $0.50` are bucket 2; `$0.50` and higher are bucket 3. The cost is the router's per-request `estimated_cost_usd`, derived from the candidate model's input/output token prices and the request's estimated input/output tokens.

Priority `>= 14` is the G1 escalation path and maps to `quality >= knemon.g1_quality_floor` (default `0.85`). That preserves the intended high-priority quality floor. Priority `>= 10` maps to tier A/B candidates with `quality >= knemon.g2_quality_floor` (default `0.75`).

If a session is burned, the router lowers the economic routing priority but still evaluates the quality floor from the originally requested priority. A burned priority-14 request therefore remains constrained by `g1_quality_floor`.

The session burn threshold aligns with operator practice when it trips at `>= 10` requests in the rolling one-hour window. That is a local guardrail for interactive sessions: normal hand-driven use stays below it, while repeated retries, fanout loops, or automation bursts get downgraded before they drain subscription headroom. The policy is configurable through `MNEMOS_KNEMON_SESSION_BURN_REQUESTS_PER_HOUR` and `MNEMOS_KNEMON_SESSION_BURN_WINDOW_SECONDS`; bulk workers should use separate sessions or raise the threshold explicitly.

## Seeded Plan Rows

These are the rows seeded or refreshed by `0039_knemon_dispatch_rule_refresh`. Rows with future `effective_from` dates are present for the tier flips but are not active until that date.

| Provider | Plan | Cap |
| --- | --- | --- |
| Anthropic | `claude_max_200` | Local planning cap: 900 messages per 5h through 2026-05-31 |
| Anthropic | `claude_max_100` | Local planning cap: 225 messages per 5h from 2026-06-01 |
| OpenAI | `chatgpt_plus` | 160 ChatGPT GPT-5.5 messages per 3h |
| OpenAI | `chatgpt_pro` | Backward-compatible ChatGPT Pro $200 alias: unmetered GPT-5.5 access subject to abuse guardrails |
| OpenAI | `chatgpt_pro_100` | ChatGPT Pro $100 tier: unmetered GPT-5.5 access subject to abuse guardrails; 5x Plus overall Pro tier |
| OpenAI | `chatgpt_pro_200` | ChatGPT Pro $200 tier: unmetered GPT-5.5 access subject to abuse guardrails; 20x Plus overall Pro tier |
| OpenAI | `codex_plus` | Local planning cap: 15 GPT-5.5 messages per 5h |
| OpenAI | `codex_pro_100_10x` | Local planning cap: 160 GPT-5.5 messages per 5h through 2026-05-31 |
| OpenAI | `codex_pro_100_5x` | Local planning cap: 80 GPT-5.5 messages per 5h from 2026-06-01 |
| OpenAI | `codex_pro_200_25x` | Local planning cap: 375 GPT-5.5 messages per 5h through 2026-05-31 |
| OpenAI | `codex_pro_200_20x` | Local planning cap: 300 GPT-5.5 messages per 5h from 2026-06-01 |

OpenAI's public ChatGPT Pro documentation supports both $100 and $200 Pro tiers. The generic `chatgpt_pro` row remains as a backward-compatible $200 operator alias so existing workspace pools keep resolving; new pools should prefer the explicit `chatgpt_pro_100` or `chatgpt_pro_200` rows. Rows with no `msg_cap` or `token_cap` report no utilization percentage in the utilization API and `0%` utilization inside the router. That intentionally treats ChatGPT Pro as operator-owned unmetered subscription capacity for dispatch ranking, still subject to provider abuse guardrails and the local session-burn downgrade.

Anthropic's public Max documentation supports the $100 Max 5x and $200 Max 20x tier relationship and publishes the `225` / `900` message counts as at-least usage estimates. KNEMON treats them as conservative planning caps for utilization accounting, not hard provider caps. The 2026-06-01 switch from `claude_max_200` to `claude_max_100` is a local operator policy encoded in KNEMON, not a provider-published tier migration.

Do not treat Codex `msg_cap` values as the billing source of truth. OpenAI's Codex docs publish included usage ranges for plan headroom and a separate credit/rate-card path for usage beyond included limits and migrated workspaces. KNEMON utilization now reports `msg_cap` rows by request count and `token_cap` rows by token count; a future schema should add an explicit cap unit such as `message`, `token`, or `credit` before moving Codex accounting from local message headroom to credit telemetry.

For the `codex_pro_200_25x` expiry, this audit follows the Codex-specific pricing page's May 31, 2026 date for the temporary 5-hour boost. OpenAI's generic ChatGPT Pro article still contains older/conflicting March 31 wording for that same boost, so keep this row tied to the Codex pricing page unless OpenAI updates the Codex usage-limit table.

The deprecated `claude_max_interactive_post_jun15` and `agent_sdk_credit_pool_post_jun15` rows are expired at 2026-05-31 because no current official source supports that split as an active operator assumption.

The router filters `effective_from` and `effective_until` in Python after fetching rows. The comparison uses the current UTC date. That avoids Oracle-only date syntax in shared router code and prevents expired promo/tier-flip rows from winning on PostgreSQL, Oracle, or Db2 paths. The in-memory SQLite tests exercise the same Python filter, but SQLite migration expansion is outside this plan-row refresh.

## Router Policy

`_apply_priority_ceiling` runs before subscription/API waterfall selection. It receives both the effective priority and the original requested priority:

```text
if requested_priority >= 14:
    keep candidates where quality >= g1_quality_floor
elif effective_priority >= 10:
    keep tier A/B candidates where quality >= g2_quality_floor
else:
    keep tier A/B candidates, ordered by tier then weight
```

`_fallback_bucket` is the fallback-chain order, not the initial winner rule. A subscription at `>= 70%` utilization falls to `other` for fallback ranking unless the main waterfall has already selected it under the high-priority near-cap rules:

```text
free -> subscription under 70% utilization -> API/token below $0.50 -> API/token at or above $0.50 -> other
```

Workspace subscription pools are matched by exact plan aliases, parent plan aliases, provider aliases, and provider-specific family aliases. OpenAI family aliases are intentionally split:

```text
chatgpt_* -> chatgpt_subscription
codex_* -> codex_subscription
```

Both still carry `openai_subscription` for operators that intentionally pool all OpenAI subscriptions.

Codex Pro promo rows use stable parent aliases `codex_pro_100` and `codex_pro_200`. A workspace with only `codex_plus` must not match a Pro row; use `codex_subscription` only when intentionally pooling all Codex tiers.

When no workspace pool is known, the router uses model metadata as a fallback family discriminator. Generic OpenAI `gpt-*` candidates map to ChatGPT subscription rows; OpenAI model metadata containing `codex` maps to Codex rows. Workspace pools remain authoritative when present.

Session burn is scoped to `usage_ledger.session_id` across providers and plans. It sums `request_count`, so retries and fanout attempts count when they are recorded against the same session. The burn window is sliding, not bucketed.

## Config Defaults

The PR adds these operator policy defaults to `config.toml.example`, `.env.example`, and `KnemonSettings`:

| Setting | Default | Role |
| --- | ---: | --- |
| `session_burn_requests_per_hour` | `10` | Burn routing after the tenth recorded ledger request in the rolling window |
| `session_burn_window_seconds` | `3600` | Rolling burn window |
| `subscription_preferred_utilization_pct` | `70.0` | Preferred subscription bucket ceiling; the comparison is strict `< 70` |
| `subscription_near_cap_pct` | `90.0` | High-priority subscription override threshold |
| `low_priority_api_cost_ceiling_usd` | `0.50` | Cheap API bucket ceiling; the comparison is strict `< 0.50` |
| `g1_quality_floor` | `0.85` | Priority `>= 14` quality floor |
| `g2_quality_floor` | `0.75` | Priority `>= 10` quality floor |

## Cross-Check

The audit was cross-checked with a Codex muse review and external GRAEAE consultations. Codex found no blocking issues in the fallback, G1, burn-threshold, or plan-row audit questions, and flagged SQLite migration wording plus stale-window burn coverage as follow-ups; both are addressed here. The parent-alias parity fix is included so exact `codex_pro_100` and `codex_pro_200` pools map to the correct promo/current rows without granting Pro access to `codex_plus`-only workspaces. GRAEAE agreed that Codex Pro promo rows, Anthropic Max planning caps, and the 2026-06-01 tier flip must stay labeled as provider-promo/local-policy assumptions rather than provider billing facts. A later GRAEAE pass also raised policy clarity risks around unmetered Pro utilization, the automatic 2026-06-01 activation, burned-session quality floors, and the `$0.50` cost unit; those are intentional operator policy choices documented above, not code blockers. Direct hive submission to the online Claude worker was rejected because this session registered without a submitter URN, so the external GRAEAE consultation was used as the Claude-provider fallback cross-check. Treat `70%`, `$0.50`, `0.85`, and `10 req/hr` as explicit operator policy thresholds, not external provider facts.

## Sources

- OpenAI ChatGPT GPT-5.5 limits: https://help.openai.com/en/articles/11909943-gpt-55-in-chatgpt
- OpenAI ChatGPT Pro tiers: https://help.openai.com/en/articles/9793128-what-is-chatgpt-pro
- OpenAI Codex pricing and usage limits: https://developers.openai.com/codex/pricing
- OpenAI Codex credit rate card: https://help.openai.com/en/articles/20001106-codex-rate-card
- OpenAI Plus/Pro flexible credits: https://help.openai.com/en/articles/12642688-using-credits-for-flexible-usage-in-chatgpt-free-go-plus-pro
- Anthropic Claude Max plan tiers: https://support.claude.com/en/articles/11049741-what-is-the-max-plan
- Anthropic Claude Max usage estimates: https://support.claude.com/en/articles/11014257-about-claude-s-max-plan-usage
