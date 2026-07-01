-- migration: 0035_subscription_plans_date_aware
-- PostgreSQL mirror of Oracle 0035_subscription_plans_date_aware.

ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS effective_from DATE NOT NULL DEFAULT DATE '2026-01-01';
ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS effective_until DATE;
ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS path_kind TEXT NOT NULL DEFAULT 'api';
ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS parent_plan_id TEXT;
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS path_kind TEXT NOT NULL DEFAULT 'api';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_subscription_plans_path_kind'
  ) THEN
    ALTER TABLE subscription_plans
      ADD CONSTRAINT ck_subscription_plans_path_kind
      CHECK (path_kind = ANY (ARRAY['interactive','sdk_credit_pool','api','unmetered','free']::TEXT[]));
  END IF;
END $$;

UPDATE subscription_plans
SET effective_from = DATE '2026-04-01',
    effective_until = DATE '2026-05-31',
    path_kind = 'interactive'
WHERE provider = 'anthropic'
  AND plan_name = 'claude_max_200';

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

INSERT INTO subscription_plans (
  provider, plan_name, auth_method, monthly_usd, msg_cap, msg_window_seconds,
  token_cap, token_window_seconds, reset_anchor, overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out, notes, effective_from, effective_until,
  path_kind, parent_plan_id
) VALUES
  ('anthropic', 'claude_max_100', 'subscription', 100, 225, 18000, NULL, NULL,
   'rolling', NULL, NULL, 'Claude Max 100 (5x): at least 225 messages per 5h window until 2026-06-14',
   DATE '2026-06-01', DATE '2026-06-14', 'interactive', 'claude_max_200'),
  ('anthropic', 'claude_max_interactive_post_jun15', 'subscription', 100, 225, 18000, NULL, NULL,
   'rolling', NULL, NULL, 'Claude Max 100 (5x) interactive plan after 2026-06-15',
   DATE '2026-06-15', NULL, 'interactive', 'claude_max_100'),
  ('anthropic', 'agent_sdk_credit_pool_post_jun15', 'subscription', 0, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'Claude Max Agent SDK credit pool after 2026-06-15',
   DATE '2026-06-15', NULL, 'sdk_credit_pool', 'claude_max_100'),
  ('openai', 'chatgpt_pro_100_codex_promo', 'subscription', 100, 160, 18000, NULL, NULL,
   'rolling', NULL, NULL, 'ChatGPT Codex Pro $100 launch promo: conservative GPT-5.5 local-message floor per 5h through 2026-05-31',
   DATE '2026-05-01', DATE '2026-05-31', 'interactive', 'chatgpt_pro_100_codex'),
  ('openai', 'chatgpt_pro_100_codex', 'subscription', 100, 80, 18000, NULL, NULL,
   'rolling', NULL, NULL, 'ChatGPT Codex Pro $100: conservative GPT-5.5 local-message floor per 5h from 2026-06-01',
   DATE '2026-06-01', NULL, 'interactive', 'chatgpt_pro'),
  ('openai', 'chatgpt_pro_200_codex', 'subscription', 200, 300, 18000, NULL, NULL,
   'rolling', NULL, NULL, 'ChatGPT Codex Pro $200: conservative GPT-5.5 local-message floor per 5h from 2026-06-01',
   DATE '2026-06-01', NULL, 'interactive', 'chatgpt_pro'),
  ('xai', 'supergrok', 'subscription', 30, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'xAI SuperGrok interactive subscription',
   DATE '2026-05-19', NULL, 'interactive', NULL)
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
