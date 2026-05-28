-- migration: 0039_knemon_dispatch_rule_refresh
-- Oracle mirror of PostgreSQL 0039_knemon_dispatch_rule_refresh.

UPDATE subscription_plans
SET effective_until = DATE '2026-05-31',
    notes = COALESCE(notes, '') || ' (expired by 0039: speculative post-Jun15 split superseded)'
WHERE provider = 'anthropic'
  AND plan_name IN ('claude_max_interactive_post_jun15', 'agent_sdk_credit_pool_post_jun15')
  AND (effective_until IS NULL OR effective_until > DATE '2026-05-31');

MERGE INTO subscription_plans dst
USING (
  SELECT 'anthropic' provider, 'claude_max_200' plan_name, 'subscription' auth_method,
         200 monthly_usd, 900 msg_cap, 18000 msg_window_seconds,
         NULL token_cap, NULL token_window_seconds, 'rolling' reset_anchor,
         NULL overage_pricing_per_mtok_in, NULL overage_pricing_per_mtok_out,
         'Claude Max $200 / 20x: at least 900 messages per 5h; local operator tier flip ends 2026-05-31' notes,
         DATE '2026-04-01' effective_from, DATE '2026-05-31' effective_until,
         'interactive' path_kind, NULL parent_plan_id FROM dual
  UNION ALL SELECT 'anthropic', 'claude_max_100', 'subscription',
         100, 225, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Claude Max $100 / 5x: at least 225 messages per 5h; local operator tier flip starts 2026-06-01',
         DATE '2026-06-01', NULL, 'interactive', 'claude_max_200' FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_plus', 'subscription',
         20, 160, 10800, NULL, NULL, 'rolling', NULL, NULL,
         'ChatGPT Plus GPT-5.5: 160 messages per 3h window',
         DATE '2026-05-28', NULL, 'interactive', NULL FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_pro', 'subscription',
         200, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL,
         'ChatGPT Pro GPT-5.5: unlimited access subject to abuse guardrails',
         DATE '2026-05-28', NULL, 'unmetered', NULL FROM dual
  UNION ALL SELECT 'openai', 'codex_plus', 'subscription',
         20, 15, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Plus GPT-5.5 local lower-bound: 15 of 15-80 messages per 5h',
         DATE '2026-05-28', NULL, 'interactive', NULL FROM dual
  UNION ALL SELECT 'openai', 'codex_pro_100_10x', 'subscription',
         100, 160, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Pro $100 launch promo: GPT-5.5 lower-bound, 160 messages per 5h through 2026-05-31',
         DATE '2026-05-28', DATE '2026-05-31', 'interactive', 'codex_plus' FROM dual
  UNION ALL SELECT 'openai', 'codex_pro_100_5x', 'subscription',
         100, 80, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Pro $100: GPT-5.5 5x lower-bound, 80 of 80-400 messages per 5h from 2026-06-01',
         DATE '2026-06-01', NULL, 'interactive', 'codex_plus' FROM dual
  UNION ALL SELECT 'openai', 'codex_pro_200_25x', 'subscription',
         200, 375, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Pro $200 temporary 25x GPT-5.5 lower-bound, 375 messages per 5h through 2026-05-31',
         DATE '2026-05-28', DATE '2026-05-31', 'interactive', 'codex_plus' FROM dual
  UNION ALL SELECT 'openai', 'codex_pro_200_20x', 'subscription',
         200, 300, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Pro $200: GPT-5.5 20x lower-bound, 300 of 300-1600 messages per 5h from 2026-06-01',
         DATE '2026-06-01', NULL, 'interactive', 'codex_plus' FROM dual
) src
ON (dst.provider = src.provider AND dst.plan_name = src.plan_name)
WHEN MATCHED THEN UPDATE SET
  dst.auth_method = src.auth_method,
  dst.monthly_usd = src.monthly_usd,
  dst.msg_cap = src.msg_cap,
  dst.msg_window_seconds = src.msg_window_seconds,
  dst.token_cap = src.token_cap,
  dst.token_window_seconds = src.token_window_seconds,
  dst.reset_anchor = src.reset_anchor,
  dst.overage_pricing_per_mtok_in = src.overage_pricing_per_mtok_in,
  dst.overage_pricing_per_mtok_out = src.overage_pricing_per_mtok_out,
  dst.notes = src.notes,
  dst.effective_from = src.effective_from,
  dst.effective_until = src.effective_until,
  dst.path_kind = src.path_kind,
  dst.parent_plan_id = src.parent_plan_id
WHEN NOT MATCHED THEN INSERT (
  provider, plan_name, auth_method, monthly_usd, msg_cap, msg_window_seconds,
  token_cap, token_window_seconds, reset_anchor, overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out, notes, effective_from, effective_until,
  path_kind, parent_plan_id
) VALUES (
  src.provider, src.plan_name, src.auth_method, src.monthly_usd, src.msg_cap,
  src.msg_window_seconds, src.token_cap, src.token_window_seconds,
  src.reset_anchor, src.overage_pricing_per_mtok_in,
  src.overage_pricing_per_mtok_out, src.notes, src.effective_from,
  src.effective_until, src.path_kind, src.parent_plan_id
);
