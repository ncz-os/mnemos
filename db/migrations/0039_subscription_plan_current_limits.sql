-- migration: 0039_subscription_plan_current_limits
-- Refreshes KNEMON subscription-plan rows against current Codex and Claude Max limits.
-- Verified from official vendor docs on 2026-05-28.

UPDATE subscription_plans
SET auth_method = 'subscription',
    monthly_usd = 20,
    msg_cap = 15,
    msg_window_seconds = 18000,
    token_cap = NULL,
    token_window_seconds = NULL,
    reset_anchor = 'rolling',
    notes = 'ChatGPT Codex Plus: conservative GPT-5.5 local-message floor per 5h; official limits vary by model and surface',
    effective_from = DATE '2026-05-01',
    effective_until = NULL,
    path_kind = 'interactive',
    parent_plan_id = NULL
WHERE provider = 'openai'
  AND plan_name = 'chatgpt_plus';

UPDATE subscription_plans
SET auth_method = 'subscription',
    monthly_usd = 200,
    msg_cap = 375,
    msg_window_seconds = 18000,
    token_cap = NULL,
    token_window_seconds = NULL,
    reset_anchor = 'rolling',
    notes = 'ChatGPT Codex Pro $200 promo: conservative GPT-5.5 local-message floor per 5h through 2026-05-31',
    effective_from = DATE '2026-05-01',
    effective_until = DATE '2026-05-31',
    path_kind = 'interactive',
    parent_plan_id = 'chatgpt_pro_200_codex'
WHERE provider = 'openai'
  AND plan_name = 'chatgpt_pro';

UPDATE subscription_plans
SET effective_until = DATE '2026-05-27',
    notes = COALESCE(notes, '')
      || CASE WHEN COALESCE(notes, '') = '' THEN '' ELSE ' ' END
      || 'Retired by 0039; current Claude Max docs keep 5x and 20x tiers active without the prior post-Jun15 split.'
WHERE provider = 'anthropic'
  AND plan_name IN ('claude_max_interactive_post_jun15', 'agent_sdk_credit_pool_post_jun15');

INSERT INTO subscription_plans (
  provider, plan_name, auth_method, monthly_usd, msg_cap, msg_window_seconds,
  token_cap, token_window_seconds, reset_anchor, overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out, notes, effective_from, effective_until,
  path_kind, parent_plan_id
) VALUES
  ('openai', 'chatgpt_pro_100_codex_promo', 'subscription', 100, 160, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'ChatGPT Codex Pro $100 launch promo: conservative GPT-5.5 local-message floor per 5h through 2026-05-31',
   DATE '2026-05-01', DATE '2026-05-31', 'interactive', 'chatgpt_pro_100_codex'),
  ('openai', 'chatgpt_pro_100_codex', 'subscription', 100, 80, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'ChatGPT Codex Pro $100: conservative GPT-5.5 local-message floor per 5h from 2026-06-01',
   DATE '2026-06-01', NULL, 'interactive', 'chatgpt_pro'),
  ('openai', 'chatgpt_pro_200_codex', 'subscription', 200, 300, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'ChatGPT Codex Pro $200: conservative GPT-5.5 local-message floor per 5h from 2026-06-01',
   DATE '2026-06-01', NULL, 'interactive', 'chatgpt_pro'),
  ('anthropic', 'claude_max_100', 'subscription', 100, 225, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'Claude Max 5x $100: at least 225 messages per 5h under current Claude Max guidance.',
   DATE '2026-05-28', NULL, 'interactive', NULL),
  ('anthropic', 'claude_max_200', 'subscription', 200, 900, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'Claude Max 20x $200: at least 900 messages per 5h under current Claude Max guidance.',
   DATE '2026-05-28', NULL, 'interactive', NULL)
ON CONFLICT (provider, plan_name) DO UPDATE SET
  auth_method = EXCLUDED.auth_method,
  monthly_usd = EXCLUDED.monthly_usd,
  msg_cap = EXCLUDED.msg_cap,
  msg_window_seconds = EXCLUDED.msg_window_seconds,
  token_cap = EXCLUDED.token_cap,
  token_window_seconds = EXCLUDED.token_window_seconds,
  reset_anchor = EXCLUDED.reset_anchor,
  overage_pricing_per_mtok_in = EXCLUDED.overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out = EXCLUDED.overage_pricing_per_mtok_out,
  notes = EXCLUDED.notes,
  effective_from = EXCLUDED.effective_from,
  effective_until = EXCLUDED.effective_until,
  path_kind = EXCLUDED.path_kind,
  parent_plan_id = EXCLUDED.parent_plan_id;
