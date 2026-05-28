# KNEMON Dispatch Rules

Audit date: 2026-05-28

## Verdict

The pre-audit `subscription_plans` catalog was not current. It still carried ChatGPT Plus at 40 messages per 3h, ChatGPT Pro as a weekly 200-token-style cap, Claude Max 100 at 450 messages per 5h, and speculative Claude post-2026-06-15 rows. Migration `0039_knemon_dispatch_rule_refresh` refreshes the active rows, keeps Codex separate from ChatGPT so workspace pools can route correctly, and uses conservative current Codex GPT-5.5 lower bounds for generic Codex plan headroom.

The fallback order is correct when implemented as:

1. Free auth.
2. Subscription auth with utilization below `knemon.subscription_preferred_utilization_pct` (default `70.0`).
3. API/token auth with estimated request cost strictly below `knemon.low_priority_api_cost_ceiling_usd` (default `$0.50`).
4. API/token auth at or above the cheap-API ceiling.
5. Other auth methods.

The `$0.50` boundary is intentionally not cheap API. Values `< $0.50` are bucket 2; `$0.50` and higher are bucket 3.

Priority `>= 14` is the G1 escalation path and maps to `quality >= knemon.g1_quality_floor` (default `0.85`). That preserves the intended high-priority quality floor. Priority `>= 10` maps to tier A/B candidates with `quality >= knemon.g2_quality_floor` (default `0.75`).

If a session is burned, the router lowers the economic routing priority but still evaluates the quality floor from the originally requested priority. A burned priority-14 request therefore remains constrained by `g1_quality_floor`.

The session burn threshold aligns with operator practice when it trips at `>= 10` requests in the rolling one-hour window. The policy is configurable through `MNEMOS_KNEMON_SESSION_BURN_REQUESTS_PER_HOUR` and `MNEMOS_KNEMON_SESSION_BURN_WINDOW_SECONDS`.

## Current Plan Rows

These are the current rows after `0039_knemon_dispatch_rule_refresh`.

| Provider | Plan | Cap |
| --- | --- | --- |
| Anthropic | `claude_max_200` | 900 messages per 5h through 2026-05-31 |
| Anthropic | `claude_max_100` | 225 messages per 5h from 2026-06-01 |
| OpenAI | `chatgpt_plus` | 160 ChatGPT GPT-5.5 messages per 3h |
| OpenAI | `chatgpt_pro` | Unmetered ChatGPT GPT-5.5 access subject to abuse guardrails |
| OpenAI | `codex_plus` | 15 GPT-5.5 local messages per 5h, lower bound of 15-80 |
| OpenAI | `codex_pro_100_10x` | 160 GPT-5.5 local messages per 5h through 2026-05-31, doubled Pro 5x lower bound |
| OpenAI | `codex_pro_100_5x` | 80 GPT-5.5 local messages per 5h from 2026-06-01, lower bound of 80-400 |
| OpenAI | `codex_pro_200_25x` | 375 GPT-5.5 local messages per 5h through 2026-05-31, temporary 25x Plus lower bound |
| OpenAI | `codex_pro_200_20x` | 300 GPT-5.5 local messages per 5h from 2026-06-01, lower bound of 300-1600 |

Anthropic's public Max documentation supports the Max 100 and Max 200 tier relationship. The 900/225 message caps and 2026-06-01 switch from `claude_max_200` to `claude_max_100` are local operator-normalized policy values encoded in KNEMON, not provider-published universal message counts.

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

## Cross-Check

The audit was cross-checked with a Codex muse review. Codex confirmed the fallback, G1, and burn rules and flagged the portable effective-date filtering fix that is included here. The requested Claude/GRAEAE consultation path did not produce a Claude result in this environment: one external consultation returned with Claude unavailable, and a shorter retry timed out. Provider-limit conclusions are therefore grounded in the official source links below rather than model assertion. Treat `70%`, `$0.50`, `0.85`, and `10 req/hr` as explicit operator policy thresholds, not external provider facts.

## Sources

- OpenAI ChatGPT GPT-5.5 limits: https://help.openai.com/en/articles/11909943-gpt-55-in-chatgpt
- OpenAI Codex pricing and limits: https://developers.openai.com/codex/pricing
- Anthropic Claude Max usage limits: https://support.claude.com/en/articles/11014257-about-claude-s-max-plan-usage
