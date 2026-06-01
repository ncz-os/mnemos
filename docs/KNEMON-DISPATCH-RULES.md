# KNEMON Dispatch Rules

Verified: 2026-05-28

This document records the KNEMON router policy for subscription routing,
priority ceilings, fallback ordering, plan-window accounting, and per-session
burn control.

## Source Check

External plan facts were checked against official vendor documentation:

- OpenAI Codex pricing: https://developers.openai.com/codex/pricing
- OpenAI Codex with ChatGPT plans: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- Claude Max plan usage: https://support.claude.com/en/articles/11014257-about-claude-s-max-plan-usage
- Claude Max plan: https://support.claude.com/en/articles/11049741-what-is-the-max-plan
- Claude Code with Pro or Max: https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan
- Claude Agent SDK with a Claude plan: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan

OpenAI publishes model- and surface-specific ranges, not one universal Codex
message cap. KNEMON stores conservative local-message lower bounds using the
most constrained current local model row, GPT-5.5. Claude Max publishes
five-hour lower-bound estimates for the 5x and 20x tiers; KNEMON stores those as
local planning caps for utilization accounting.

## Subscription Plans

The merged release keeps the HEAD-only `0039_knemon_dispatch_rule_refresh`
migration, then applies the branch's audited
`0039_subscription_plan_current_limits` migration after it. The latter is the
authoritative correction for current plan caps and active dates.

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
post-2026-06-15 Claude split rows are removed by
`0039_subscription_plan_current_limits`.

Anthropic's separate Agent SDK / `claude -p` monthly credit starts on
2026-06-15, but it is not modeled as a router-visible subscription plan yet.
KNEMON currently has message/token utilization caps, not a credit-denominated
cap with route intent, so keeping the old SDK credit pool row would let a
non-interactive credit path compete in the interactive waterfall.

HEAD-only compatibility rows such as `codex_plus`, `codex_pro_100_10x`,
`codex_pro_100_5x`, `codex_pro_200_25x`, `codex_pro_200_20x`,
`chatgpt_pro_100`, and `chatgpt_pro_200` may still be present on installations
that applied `0039_knemon_dispatch_rule_refresh`. Their aliases remain supported
for existing workspace pools, but the audited current rows above define the
dispatch facts for this release.

## Plan Windows

Known interactive subscription rows use rolling five-hour windows unless the
row itself supplies an explicit window. Free, API, token, and unmetered rows use
monthly accounting. `mnemos.core.plan_windows` is the single shared helper for
API, domain, and persistence code; the router must not import the ledger route
to compute window ids.

The router filters `effective_from` and `effective_until` in Python after
fetching rows. This keeps Oracle, PostgreSQL, SQLite, and Db2 behavior aligned
and prevents expired promo rows from winning after their window closes.

Subscription utilization counts ledger rows by provider, tier, path kind, and
plan window. After the 0035/0039 `path_kind` split, legacy subscription rows may
still carry the old default `path_kind='api'`; KNEMON counts those legacy rows
for matching subscription tiers while new ledger writes use the known plan path
kind.

## Pool Selection

When multiple active plans exist for one provider, the router selects the
highest sorted plan that matches the caller's `hive_agents.subscription_pools`.
Supported pool aliases include exact plan names, parent plan ids, provider
aliases such as `openai_subscription` and `anthropic_subscription`, and product
aliases such as `chatgpt_subscription`, `codex_subscription`, and
`claude_subscription`.

Generic provider aliases choose the highest active monthly plan. Use exact plan
pools when a worker should be constrained to a specific subscription tier.
ChatGPT and Codex family aliases are intentionally split; use
`openai_subscription` only when intentionally pooling both surfaces.

When no workspace pool is known, the router uses model metadata as a fallback
family discriminator. Generic OpenAI `gpt-*` candidates map to ChatGPT
subscription rows; OpenAI model metadata containing `codex` maps to Codex rows.
Workspace pools remain authoritative when present.

## Priority Ceilings

Priority filtering is applied before waterfall selection:

- Requested priority `>= 14` is G1 and requires quality `>= knemon.g1_quality_floor`.
- Effective priority `>= 10` requires tier A or B and quality `>= knemon.g2_quality_floor`.
- Lower priority keeps tier A/B candidates and sorts tier A ahead of tier B.

`quality` is the normalized router quality score. The router prefers
`quality_score`, then `graeae_weight`, then `weight`, normalizing values above
`1.0` as percentages; if none are present, it derives a bounded score from
`arena_score / 1500`. Tier comes from `cost_tier` or `usage_tier` when present;
otherwise quality `>= 0.85` maps to tier A, quality `>= 0.75` maps to tier B,
and the remaining candidates are tier C.

Session burn can lower the effective priority for spend and utilization
decisions, but G1 quality is based on the original requested priority. A burned
G1 request therefore still requires the G1 quality floor, but it does not
receive the high-priority near-cap subscription exception.

## Fallback Buckets

Fallback chain ranking uses this order:

1. Free plans.
2. Subscription plans under `knemon.subscription_preferred_utilization_pct`.
3. API/token paths with estimated cost below `knemon.low_priority_api_cost_ceiling_usd`.
4. Other API/token paths.
5. Other auth methods.

Boundary behavior is intentional: subscription utilization at exactly `70.0%`
is not under 70%, and API/token cost at exactly `$0.50` is not below `$0.50` by
default. Fallback subscriptions use real plan-window utilization before bucket
ranking. The selected primary route is determined by the waterfall; fallback
buckets only order alternates.

## Session Burn

The default burn threshold is 10 requests in a trailing one-hour window, exposed
as `MNEMOS_KNEMON_SESSION_BURN_REQUESTS_PER_HOUR`,
`MNEMOS_KNEMON_SESSION_BURN_WINDOW_SECONDS`, and the `[knemon]` config section.
A session is burned once the ledger already shows `>= 10` requests in that
window, so the next request is downgraded.

Burn downgrade mapping:

- Requested priority `>= 14` uses effective priority `13`.
- Requested priority `>= 10` uses effective priority `9`.
- Lower priorities are unchanged.

This matches operator practice for preventing a hot session from continuing to
escalate spend while preserving the original G1 quality floor.

## Config Defaults

| Setting | Default | Role |
| --- | ---: | --- |
| `session_burn_requests_per_hour` | `10` | Burn routing after the tenth recorded ledger request in the rolling window |
| `session_burn_window_seconds` | `3600` | Rolling burn window |
| `subscription_preferred_utilization_pct` | `70.0` | Preferred subscription bucket ceiling; comparison is strict `< 70` |
| `subscription_near_cap_pct` | `90.0` | High-priority subscription override threshold |
| `low_priority_api_cost_ceiling_usd` | `0.50` | Cheap API bucket ceiling; comparison is strict `< 0.50` |
| `g1_quality_floor` | `0.85` | Requested priority `>= 14` quality floor |
| `g2_quality_floor` | `0.75` | Effective priority `>= 10` quality floor |
