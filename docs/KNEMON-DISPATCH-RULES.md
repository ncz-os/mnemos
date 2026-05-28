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

The `$0.50` boundary is intentionally not cheap API. Values `< $0.50` are bucket 2; `$0.50` and higher are bucket 3.

Priority `>= 14` is the G1 escalation path and maps to `quality >= knemon.g1_quality_floor` (default `0.85`). That preserves the intended high-priority quality floor. Priority `>= 10` maps to tier A/B candidates with `quality >= knemon.g2_quality_floor` (default `0.75`).

If a session is burned, the router lowers the economic routing priority but still evaluates the quality floor from the originally requested priority. A burned priority-14 request therefore remains constrained by `g1_quality_floor`.

The session burn threshold aligns with operator practice when it trips at `>= 10` requests in the rolling one-hour window. That is a local guardrail for interactive sessions: normal hand-driven use stays below it, while repeated retries, fanout loops, or automation bursts get downgraded before they drain subscription headroom. The policy is configurable through `MNEMOS_KNEMON_SESSION_BURN_REQUESTS_PER_HOUR` and `MNEMOS_KNEMON_SESSION_BURN_WINDOW_SECONDS`; bulk workers should use separate sessions or raise the threshold explicitly.

## Current Plan Rows

These are the current rows after `0039_knemon_dispatch_rule_refresh`.

| Provider | Plan | Cap |
| --- | --- | --- |
| Anthropic | `claude_max_200` | Local planning cap: 900 messages per 5h through 2026-05-31 |
| Anthropic | `claude_max_100` | Local planning cap: 225 messages per 5h from 2026-06-01 |
| OpenAI | `chatgpt_plus` | 160 ChatGPT GPT-5.5 messages per 3h |
| OpenAI | `chatgpt_pro` | Unmetered ChatGPT GPT-5.5 access subject to abuse guardrails |
| OpenAI | `codex_plus` | Local planning cap: 15 GPT-5.5 messages per 5h |
| OpenAI | `codex_pro_100_10x` | Local planning cap: 160 GPT-5.5 messages per 5h through 2026-05-31 |
| OpenAI | `codex_pro_100_5x` | Local planning cap: 80 GPT-5.5 messages per 5h from 2026-06-01 |
| OpenAI | `codex_pro_200_25x` | Local planning cap: 375 GPT-5.5 messages per 5h through 2026-05-31 |
| OpenAI | `codex_pro_200_20x` | Local planning cap: 300 GPT-5.5 messages per 5h from 2026-06-01 |

Anthropic's public Max documentation supports the $100 Max 5x and $200 Max 20x tier relationship. The `900` and `225` message caps are conservative local planning caps for KNEMON utilization accounting. The 2026-06-01 switch from `claude_max_200` to `claude_max_100` is a local operator policy encoded in KNEMON, not a provider-published tier migration.

Do not treat Codex `msg_cap` values as the billing source of truth. OpenAI's Codex docs publish included usage ranges for plan headroom and a separate credit/rate-card path for usage beyond included limits and migrated workspaces. KNEMON utilization now reports `msg_cap` rows by request count and `token_cap` rows by token count; a future schema should add an explicit cap unit such as `message`, `token`, or `credit` before moving Codex accounting from local message headroom to credit telemetry.

For the `codex_pro_200_25x` expiry, this audit follows the Codex-specific pricing page's May 31, 2026 date for the temporary 5-hour boost. OpenAI's generic ChatGPT Pro article still contains older/conflicting March 31 wording for that same boost, so keep this row tied to the Codex pricing page unless OpenAI updates the Codex usage-limit table.

The deprecated `claude_max_interactive_post_jun15` and `agent_sdk_credit_pool_post_jun15` rows are expired at 2026-05-31 because no current official source supports that split as an active operator assumption.

The router filters `effective_from` and `effective_until` in Python after fetching rows. That avoids Oracle-only date syntax in shared router code and prevents expired promo/tier-flip rows from winning on PostgreSQL, SQLite, or Db2 fallback paths.

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

`_fallback_bucket` is the fallback-chain order, not the initial winner rule:

```text
free -> subscription under 70% utilization -> API/token below $0.50 -> API/token at or above $0.50 -> other
```

Workspace subscription pools are matched by exact plan aliases, parent plan aliases, provider aliases, and provider-specific family aliases. OpenAI family aliases are intentionally split:

```text
chatgpt_* -> chatgpt_subscription
codex_* -> codex_subscription
```

Both still carry `openai_subscription` for operators that intentionally pool all OpenAI subscriptions.

When no workspace pool is known, the router uses model metadata as a fallback family discriminator. Generic OpenAI `gpt-*` candidates map to ChatGPT subscription rows; OpenAI model metadata containing `codex` maps to Codex rows. Workspace pools remain authoritative when present.

## Config Defaults

The PR adds these operator policy defaults to `config.toml.example`, `.env.example`, and `KnemonSettings`:

| Setting | Default | Role |
| --- | ---: | --- |
| `session_burn_requests_per_hour` | `10` | Burn a session at the tenth request in the rolling window |
| `session_burn_window_seconds` | `3600` | Rolling burn window |
| `subscription_preferred_utilization_pct` | `70.0` | Preferred subscription bucket ceiling; the comparison is strict `< 70` |
| `subscription_near_cap_pct` | `90.0` | High-priority subscription override threshold |
| `low_priority_api_cost_ceiling_usd` | `0.50` | Cheap API bucket ceiling; the comparison is strict `< 0.50` |
| `g1_quality_floor` | `0.85` | Priority `>= 14` quality floor |
| `g2_quality_floor` | `0.75` | Priority `>= 10` quality floor |

## Cross-Check

The audit was cross-checked with a Codex muse review. Codex confirmed the fallback, G1, and burn rules, verified the targeted router/utilization/config tests, and flagged token-cap utilization plus provider-level OpenAI plan collapse as the main follow-ups; both fixes are included here. The requested Claude cross-check could not complete in this environment: the external GRAEAE consultation timed out, and hive job submission to the online Claude worker was rejected because this session registered without a submitter URN. Provider-limit conclusions are therefore grounded in the official source links below rather than model assertion. Treat `70%`, `$0.50`, `0.85`, and `10 req/hr` as explicit operator policy thresholds, not external provider facts.

## Sources

- OpenAI ChatGPT GPT-5.5 limits: https://help.openai.com/en/articles/11909943-gpt-55-in-chatgpt
- OpenAI ChatGPT Pro tiers: https://help.openai.com/en/articles/9793128-what-is-chatgpt-pro
- OpenAI Codex pricing and usage limits: https://developers.openai.com/codex/pricing
- OpenAI Codex credit rate card: https://help.openai.com/en/articles/20001106-codex-rate-card
- OpenAI Plus/Pro flexible credits: https://help.openai.com/en/articles/12642688-using-credits-for-flexible-usage-in-chatgpt-free-go-plus-pro
- Anthropic Claude Max plan tiers: https://support.claude.com/en/articles/11049741-what-is-the-max-plan
- Anthropic Claude Max usage estimates: https://support.claude.com/en/articles/11014257-about-claude-s-max-plan-usage
