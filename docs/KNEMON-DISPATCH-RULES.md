# KNEMON Dispatch Rules

Verified: 2026-05-28

This document records the KNEMON router policy for subscription routing,
priority ceilings, fallback ordering, and per-session burn control.

## Source Check

External plan facts were checked against official vendor documentation:

- OpenAI Codex pricing: https://developers.openai.com/codex/pricing
- OpenAI Codex with ChatGPT plans: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- Claude Max plan usage: https://support.claude.com/en/articles/11014257-about-claude-s-max-plan-usage
- Claude Max plan: https://support.claude.com/en/articles/11049741-what-is-the-max-plan
- Claude Code with Pro or Max: https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan

OpenAI publishes model- and surface-specific ranges, not one universal Codex
message cap. KNEMON stores conservative local-message lower bounds using the
most constrained current local model row, GPT-5.5. Claude Max publishes
five-hour lower-bound estimates for the 5x and 20x tiers; KNEMON stores those as
local planning caps for utilization accounting.

## Subscription Plans

`0039_subscription_plan_current_limits` refreshes the active rows used by the
router.

| Provider | Plan row | Monthly | KNEMON cap | Active dates | Notes |
| --- | --- | ---: | ---: | --- | --- |
| openai | `chatgpt_plus` | $20 | 15 / 5h | 2026-05-01 onward | Conservative GPT-5.5 Plus local-message floor. |
| openai | `chatgpt_pro` | $200 | 375 / 5h | 2026-05-01 through 2026-05-31 | Existing Pro row retained for the $200 25x promo window. |
| openai | `chatgpt_pro_100_codex_promo` | $100 | 160 / 5h | 2026-05-01 through 2026-05-31 | $100 Pro 10x promo, derived from the GPT-5.5 80-message 5x floor. |
| openai | `chatgpt_pro_100_codex` | $100 | 80 / 5h | 2026-06-01 onward | Standard $100 Pro 5x floor. |
| openai | `chatgpt_pro_200_codex` | $200 | 300 / 5h | 2026-06-01 onward | Standard $200 Pro 20x floor. |
| anthropic | `claude_max_100` | $100 | 225 / 5h | 2026-05-28 onward | Local planning cap for Max 5x. |
| anthropic | `claude_max_200` | $200 | 900 / 5h | 2026-05-28 onward | Local planning cap for Max 20x. |

No official Anthropic source found for a Claude Max 200-to-100 tier flip on
2026-06-01. Current Claude docs keep Max 5x and Max 20x as active tiers, and
Claude Code usage is shared with the same Pro/Max allocation. The older
post-2026-06-15 Claude split rows are removed by `0039_subscription_plan_current_limits`.

The router filters `effective_from` and `effective_until` in Python after
fetching rows. This keeps Oracle, PostgreSQL, SQLite, and Db2 behavior aligned
and prevents expired promo rows from winning after their window closes.

## Pool Selection

When multiple active plans exist for one provider, the router selects the
highest sorted plan that matches the caller's `hive_agents.subscription_pools`.
Supported pool aliases include exact plan names, parent plan ids, provider
aliases such as `openai_subscription` and `anthropic_subscription`, and product
aliases such as `chatgpt_subscription`, `codex_subscription`, and
`claude_subscription`.

Generic provider aliases choose the highest active monthly plan. Use exact plan
pools when a worker should be constrained to a specific subscription tier.

## Priority Ceilings

Priority filtering is applied before waterfall selection:

- Requested priority `>= 14` is G1 and requires quality `>= 0.85`.
- Effective priority `>= 10` requires tier A or B and quality `>= 0.75`.
- Lower priority keeps tier A/B candidates and sorts tier A ahead of tier B.

Session burn can lower the effective priority for spend and utilization
decisions, but G1 quality is based on the original requested priority. A burned
G1 request therefore still requires quality `>= 0.85`, but it does not receive
the high-priority over-cap subscription exception.

## Fallback Buckets

Fallback chain ranking uses this order:

1. Free plans.
2. Subscription plans under 70% utilization.
3. API/token paths with estimated cost below `$0.50`.
4. Other API/token paths.
5. Other auth methods.

Boundary behavior is intentional: subscription utilization at exactly `70.0%`
is not under 70%, and API/token cost at exactly `$0.50` is not below `$0.50`.

Fallback subscriptions use real plan-window utilization before bucket ranking.
The selected primary route is determined by the waterfall; fallback buckets only
order alternates.

## Session Burn

The default burn threshold is 10 requests in a trailing one-hour window, exposed
as `MNEMOS_KNEMON_SESSION_BURN_REQUESTS_PER_HOUR` and
`runtime.knemon_session_burn_requests_per_hour`. A session is burned once the
ledger already shows `>= 10` requests in that window, so the next request is
downgraded.

Burn downgrade mapping:

- Requested priority `>= 14` uses effective priority `13`.
- Requested priority `>= 10` uses effective priority `9`.
- Lower priorities are unchanged.

This matches operator practice for preventing a hot session from continuing to
escalate spend while preserving the original G1 quality floor.
