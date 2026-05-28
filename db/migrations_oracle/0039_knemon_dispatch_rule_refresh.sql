-- migration: 0039_knemon_dispatch_rule_refresh
-- Oracle mirror of PostgreSQL 0039_knemon_dispatch_rule_refresh.
-- Provenance links live in docs/KNEMON-DISPATCH-RULES.md. Rows that
-- start on 2026-06-01 are local operator-policy projections from
-- provider promo end dates or local pool changes, not provider-published
-- migration promises.

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
         'Claude Max $200 / 20x: operator-normalized 900-message 5h capacity; local tier flip ends 2026-05-31' notes,
         DATE '2026-04-01' effective_from, DATE '2026-05-31' effective_until,
         'interactive' path_kind, NULL parent_plan_id FROM dual
  UNION ALL SELECT 'anthropic', 'claude_max_100', 'subscription',
         100, 225, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Claude Max $100 / 5x: operator-normalized 225-message 5h capacity; local tier flip starts 2026-06-01',
         DATE '2026-06-01', NULL, 'interactive', 'claude_max_200' FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_plus', 'subscription',
         20, 160, 10800, NULL, NULL, 'rolling', NULL, NULL,
         'ChatGPT Plus GPT-5.5: 160 messages per 3h window',
         DATE '2026-05-28', NULL, 'interactive', NULL FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_pro', 'subscription',
         200, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL,
         'ChatGPT Pro $200: legacy/default row; unlimited Pro-model access subject to abuse guardrails',
         DATE '2026-05-28', NULL, 'unmetered', NULL FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_pro_100', 'subscription',
         100, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL,
         'ChatGPT Pro $100: unlimited Pro-model access subject to abuse guardrails; 5x Plus overall Pro tier',
         DATE '2026-05-28', NULL, 'unmetered', NULL FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_pro_200', 'subscription',
         200, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL,
         'ChatGPT Pro $200: unlimited Pro-model access subject to abuse guardrails; 20x Plus overall Pro tier',
         DATE '2026-05-28', NULL, 'unmetered', NULL FROM dual
  UNION ALL SELECT 'openai', 'codex_plus', 'subscription',
         20, 15, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Plus GPT-5.5 planning cap: 15 lower bound of published 15-80 messages per 5h',
         DATE '2026-05-28', NULL, 'interactive', NULL FROM dual
  UNION ALL SELECT 'openai', 'codex_pro_100_10x', 'subscription',
         100, 160, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Pro $100 launch promo planning cap: 160 lower bound of 160-800 messages per 5h through 2026-05-31',
         DATE '2026-05-28', DATE '2026-05-31', 'interactive', 'codex_pro_100' FROM dual
  UNION ALL SELECT 'openai', 'codex_pro_100_5x', 'subscription',
         100, 80, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Pro $100 planning cap: GPT-5.5 5x lower bound of 80-400 messages per 5h from 2026-06-01',
         DATE '2026-06-01', NULL, 'interactive', 'codex_pro_100' FROM dual
  UNION ALL SELECT 'openai', 'codex_pro_200_25x', 'subscription',
         200, 375, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Pro $200 temporary 25x planning cap: 375 lower bound of 375-2000 messages per 5h through 2026-05-31',
         DATE '2026-05-28', DATE '2026-05-31', 'interactive', 'codex_pro_200' FROM dual
  UNION ALL SELECT 'openai', 'codex_pro_200_20x', 'subscription',
         200, 300, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Codex Pro $200 planning cap: GPT-5.5 20x lower bound of 300-1600 messages per 5h from 2026-06-01',
         DATE '2026-06-01', NULL, 'interactive', 'codex_pro_200' FROM dual
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

UPDATE usage_ledger
SET path_kind = CASE
  WHEN provider = 'openai' AND tier IN ('chatgpt_pro', 'chatgpt_pro_100', 'chatgpt_pro_200')
    THEN 'unmetered'
  ELSE 'interactive'
END
WHERE path_kind = 'api'
  AND (
    (provider = 'anthropic' AND tier IN ('claude_max_200', 'claude_max_100'))
    OR (provider = 'openai' AND tier IN (
      'chatgpt_plus', 'chatgpt_pro', 'chatgpt_pro_100', 'chatgpt_pro_200',
      'codex_plus', 'codex_pro_100_10x', 'codex_pro_100_5x',
      'codex_pro_200_25x', 'codex_pro_200_20x'
    ))
  );
