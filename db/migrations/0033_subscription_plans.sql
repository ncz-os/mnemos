-- migration: 0033_subscription_plans
-- PostgreSQL mirror of Oracle 0033_subscription_plans.

CREATE TABLE IF NOT EXISTS subscription_plans (
  provider TEXT NOT NULL,
  plan_name TEXT NOT NULL,
  auth_method TEXT NOT NULL DEFAULT 'api',
  monthly_usd NUMERIC(12,2),
  msg_cap NUMERIC,
  msg_window_seconds NUMERIC,
  token_cap NUMERIC,
  token_window_seconds NUMERIC,
  reset_anchor TEXT,
  overage_pricing_per_mtok_in NUMERIC,
  overage_pricing_per_mtok_out NUMERIC,
  notes TEXT,
  PRIMARY KEY (provider, plan_name),
  CONSTRAINT ck_subscription_plans_auth_method
    CHECK (auth_method = ANY (ARRAY['subscription','api','free','token']::TEXT[]))
);

INSERT INTO subscription_plans (
  provider, plan_name, auth_method, monthly_usd, msg_cap, msg_window_seconds,
  token_cap, token_window_seconds, reset_anchor, overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out, notes
) VALUES
  ('anthropic', 'claude_max_200', 'subscription', 200, 900, 18000, NULL, NULL, 'rolling', NULL, NULL, 'Claude Max 200: 900 messages per 5h window'),
  ('anthropic', 'claude_max_100', 'subscription', 100, 450, 18000, NULL, NULL, 'rolling', NULL, NULL, 'Claude Max 100: 450 messages per 5h window until 2026-06-01'),
  ('openai', 'chatgpt_plus', 'subscription', 20, 40, 10800, NULL, NULL, 'rolling', NULL, NULL, 'ChatGPT Plus: 40 messages per 3h window'),
  ('openai', 'chatgpt_pro', 'subscription', 200, NULL, NULL, 200, 604800, 'weekly', NULL, NULL, 'ChatGPT Pro: unmetered plus 200 GPT-5 messages per week'),
  ('nvidia', 'ngc_integrate', 'free', 0, NULL, NULL, NULL, NULL, 'monthly', 0, 0, 'NVIDIA NGC Integrate free tier'),
  ('nvidia', 'ngc_inference', 'free', 0, NULL, NULL, NULL, NULL, 'monthly', 0, 0, 'NVIDIA NGC Inference free tier'),
  ('groq', 'dev_tier', 'token', NULL, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL, 'Groq developer tier token-based usage'),
  ('together', 'api', 'api', NULL, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL, 'Together API token-based usage'),
  ('deepseek-direct', 'api', 'api', NULL, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL, 'DeepSeek direct API token-based usage'),
  ('xai', 'api', 'api', NULL, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL, 'xAI API token-based usage'),
  ('gemini', 'api', 'api', NULL, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL, 'Gemini API token-based usage'),
  ('perplexity', 'api', 'api', NULL, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL, 'Perplexity API token-based plus search usage')
ON CONFLICT (provider, plan_name) DO NOTHING;

GRANT SELECT ON subscription_plans TO mnemos_user;
GRANT SELECT ON subscription_plans TO mnemos;
