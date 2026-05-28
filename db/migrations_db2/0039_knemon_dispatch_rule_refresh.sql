-- migration: 0039_knemon_dispatch_rule_refresh
-- Db2 mirror of PostgreSQL 0039_knemon_dispatch_rule_refresh.
-- Provenance links live in docs/KNEMON-DISPATCH-RULES.md. Rows that
-- start on 2026-06-01 are local operator-policy projections from
-- provider promo end dates or local pool changes, not provider-published
-- migration promises.

UPDATE subscription_plans
SET effective_until = DATE('2026-05-31'),
    notes = COALESCE(notes, '') || ' (expired by 0039: speculative post-Jun15 split superseded)'
WHERE provider = 'anthropic'
  AND plan_name IN ('claude_max_interactive_post_jun15', 'agent_sdk_credit_pool_post_jun15')
  AND (effective_until IS NULL OR effective_until > DATE('2026-05-31'));

MERGE INTO subscription_plans AS dst
USING (
  VALUES
    ('anthropic', 'claude_max_200', 'subscription', 200, 900, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Claude Max $200 / 20x: operator-normalized 900-message 5h capacity; local tier flip ends 2026-05-31',
     DATE('2026-04-01'), DATE('2026-05-31'), 'interactive', NULL),
    ('anthropic', 'claude_max_100', 'subscription', 100, 225, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Claude Max $100 / 5x: operator-normalized 225-message 5h capacity; local tier flip starts 2026-06-01',
     DATE('2026-06-01'), NULL, 'interactive', 'claude_max_200'),
    ('openai', 'chatgpt_plus', 'subscription', 20, 160, 10800, NULL, NULL,
     'rolling', NULL, NULL, 'ChatGPT Plus GPT-5.5: 160 messages per 3h window',
     DATE('2026-05-28'), NULL, 'interactive', NULL),
    ('openai', 'chatgpt_pro', 'subscription', 200, NULL, NULL, NULL, NULL,
     'monthly', NULL, NULL, 'ChatGPT Pro $200 GPT-5.5: legacy/default row; unlimited GPT-5.5 access subject to abuse guardrails',
     DATE('2026-05-28'), NULL, 'unmetered', NULL),
    ('openai', 'chatgpt_pro_100', 'subscription', 100, NULL, NULL, NULL, NULL,
     'monthly', NULL, NULL, 'ChatGPT Pro $100 GPT-5.5: unlimited GPT-5.5 access subject to abuse guardrails; 5x Plus overall Pro tier',
     DATE('2026-05-28'), NULL, 'unmetered', NULL),
    ('openai', 'chatgpt_pro_200', 'subscription', 200, NULL, NULL, NULL, NULL,
     'monthly', NULL, NULL, 'ChatGPT Pro $200 GPT-5.5: unlimited GPT-5.5 access subject to abuse guardrails; 20x Plus overall Pro tier',
     DATE('2026-05-28'), NULL, 'unmetered', NULL),
    ('openai', 'codex_plus', 'subscription', 20, 15, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Codex Plus GPT-5.5 local lower-bound: 15 of 15-80 messages per 5h',
     DATE('2026-05-28'), NULL, 'interactive', NULL),
    ('openai', 'codex_pro_100_10x', 'subscription', 100, 160, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Codex Pro $100 launch promo: GPT-5.5 lower-bound, 160 messages per 5h through 2026-05-31',
     DATE('2026-05-28'), DATE('2026-05-31'), 'interactive', 'codex_pro_100'),
    ('openai', 'codex_pro_100_5x', 'subscription', 100, 80, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Codex Pro $100: GPT-5.5 5x lower-bound, 80 of 80-400 messages per 5h from 2026-06-01',
     DATE('2026-06-01'), NULL, 'interactive', 'codex_pro_100'),
    ('openai', 'codex_pro_200_25x', 'subscription', 200, 375, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Codex Pro $200 temporary 25x GPT-5.5 lower-bound, 375 messages per 5h through 2026-05-31',
     DATE('2026-05-28'), DATE('2026-05-31'), 'interactive', 'codex_pro_200'),
    ('openai', 'codex_pro_200_20x', 'subscription', 200, 300, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Codex Pro $200: GPT-5.5 20x lower-bound, 300 of 300-1600 messages per 5h from 2026-06-01',
     DATE('2026-06-01'), NULL, 'interactive', 'codex_pro_200')
) AS src (
  provider, plan_name, auth_method, monthly_usd, msg_cap, msg_window_seconds,
  token_cap, token_window_seconds, reset_anchor, overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out, notes, effective_from, effective_until,
  path_kind, parent_plan_id
)
ON dst.provider = src.provider AND dst.plan_name = src.plan_name
WHEN MATCHED THEN UPDATE SET
  auth_method = src.auth_method,
  monthly_usd = src.monthly_usd,
  msg_cap = src.msg_cap,
  msg_window_seconds = src.msg_window_seconds,
  token_cap = src.token_cap,
  token_window_seconds = src.token_window_seconds,
  reset_anchor = src.reset_anchor,
  overage_pricing_per_mtok_in = src.overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out = src.overage_pricing_per_mtok_out,
  notes = src.notes,
  effective_from = src.effective_from,
  effective_until = src.effective_until,
  path_kind = src.path_kind,
  parent_plan_id = src.parent_plan_id
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
