-- 0039_subscription_plan_current_limits.sql
-- SQLite parity for KNEMON subscription-plan rows and usage ledger tables.

CREATE TABLE IF NOT EXISTS subscription_plans (
  provider TEXT NOT NULL,
  plan_name TEXT NOT NULL,
  auth_method TEXT NOT NULL DEFAULT 'api',
  monthly_usd NUMERIC,
  msg_cap NUMERIC,
  msg_window_seconds NUMERIC,
  token_cap NUMERIC,
  token_window_seconds NUMERIC,
  reset_anchor TEXT,
  overage_pricing_per_mtok_in NUMERIC,
  overage_pricing_per_mtok_out NUMERIC,
  notes TEXT,
  effective_from TEXT NOT NULL DEFAULT '2026-01-01',
  effective_until TEXT,
  path_kind TEXT NOT NULL DEFAULT 'api',
  parent_plan_id TEXT,
  PRIMARY KEY (provider, plan_name),
  CHECK (auth_method IN ('subscription','api','free','token')),
  CHECK (path_kind IN ('interactive','sdk_credit_pool','api','unmetered','free'))
);

CREATE TABLE IF NOT EXISTS usage_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  task_kind TEXT NOT NULL,
  tokens_in INTEGER NOT NULL CHECK (tokens_in >= 0),
  tokens_out INTEGER NOT NULL CHECK (tokens_out >= 0),
  tokens_reasoning INTEGER NOT NULL DEFAULT 0 CHECK (tokens_reasoning >= 0),
  est_cost_usd NUMERIC NOT NULL CHECK (est_cost_usd >= 0),
  latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
  outcome TEXT NOT NULL CHECK (outcome IN ('ok','err','timeout')),
  caller_subsystem TEXT NOT NULL,
  tier TEXT NOT NULL,
  ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  session_id TEXT,
  request_count NUMERIC NOT NULL DEFAULT 1,
  plan_window_id TEXT,
  path_kind TEXT NOT NULL DEFAULT 'api',
  subscription_amortized INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS usage_ledger_ts_idx ON usage_ledger(ts);
CREATE INDEX IF NOT EXISTS usage_ledger_model_idx ON usage_ledger(provider, model);
CREATE INDEX IF NOT EXISTS usage_ledger_session_idx ON usage_ledger(session_id);
CREATE INDEX IF NOT EXISTS usage_ledger_window_idx ON usage_ledger(plan_window_id);

DELETE FROM subscription_plans
WHERE provider = 'anthropic'
  AND plan_name IN ('claude_max_interactive_post_jun15', 'agent_sdk_credit_pool_post_jun15');

INSERT INTO subscription_plans (
  provider, plan_name, auth_method, monthly_usd, msg_cap, msg_window_seconds,
  token_cap, token_window_seconds, reset_anchor, overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out, notes, effective_from, effective_until,
  path_kind, parent_plan_id
) VALUES
  ('anthropic', 'claude_max_200', 'subscription', 200, 900, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'Claude Max 20x $200: at least 900 messages per 5h under current Claude Max guidance.',
   '2026-05-28', NULL, 'interactive', NULL),
  ('anthropic', 'claude_max_100', 'subscription', 100, 225, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'Claude Max 5x $100: at least 225 messages per 5h under current Claude Max guidance.',
   '2026-05-28', NULL, 'interactive', NULL),
  ('openai', 'chatgpt_plus', 'subscription', 20, 15, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'ChatGPT Codex Plus: conservative GPT-5.5 local-message floor per 5h; official limits vary by model and surface',
   '2026-05-01', NULL, 'interactive', NULL),
  ('openai', 'chatgpt_pro', 'subscription', 200, 375, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'ChatGPT Codex Pro $200 promo: conservative GPT-5.5 local-message floor per 5h through 2026-05-31',
   '2026-05-01', '2026-05-31', 'interactive', 'chatgpt_pro_200_codex'),
  ('openai', 'chatgpt_pro_100_codex_promo', 'subscription', 100, 160, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'ChatGPT Codex Pro $100 launch promo: conservative GPT-5.5 local-message floor per 5h through 2026-05-31',
   '2026-05-01', '2026-05-31', 'interactive', 'chatgpt_pro_100_codex'),
  ('openai', 'chatgpt_pro_100_codex', 'subscription', 100, 80, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'ChatGPT Codex Pro $100: conservative GPT-5.5 local-message floor per 5h from 2026-06-01',
   '2026-06-01', NULL, 'interactive', 'chatgpt_pro'),
  ('openai', 'chatgpt_pro_200_codex', 'subscription', 200, 300, 18000, NULL, NULL,
   'rolling', NULL, NULL,
   'ChatGPT Codex Pro $200: conservative GPT-5.5 local-message floor per 5h from 2026-06-01',
   '2026-06-01', NULL, 'interactive', 'chatgpt_pro'),
  ('nvidia', 'ngc_integrate', 'free', 0, NULL, NULL, NULL, NULL,
   'monthly', 0, 0, 'NVIDIA NGC Integrate free tier',
   '2026-01-01', NULL, 'free', NULL),
  ('nvidia', 'ngc_inference', 'free', 0, NULL, NULL, NULL, NULL,
   'monthly', 0, 0, 'NVIDIA NGC Inference free tier',
   '2026-01-01', NULL, 'free', NULL),
  ('groq', 'dev_tier', 'token', NULL, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'Groq developer tier token-based usage',
   '2026-01-01', NULL, 'api', NULL),
  ('together', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'Together API token-based usage',
   '2026-01-01', NULL, 'api', NULL),
  ('deepseek-direct', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'DeepSeek direct API token-based usage',
   '2026-01-01', NULL, 'api', NULL),
  ('xai', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'xAI API token-based usage',
   '2026-01-01', NULL, 'api', NULL),
  ('xai', 'supergrok', 'subscription', 30, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'xAI SuperGrok interactive subscription',
   '2026-05-19', NULL, 'interactive', NULL),
  ('gemini', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'Gemini API token-based usage',
   '2026-01-01', NULL, 'api', NULL),
  ('perplexity', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
   'monthly', NULL, NULL, 'Perplexity API token-based plus search usage',
   '2026-01-01', NULL, 'api', NULL)
ON CONFLICT (provider, plan_name) DO UPDATE SET
  auth_method = excluded.auth_method,
  monthly_usd = excluded.monthly_usd,
  msg_cap = excluded.msg_cap,
  msg_window_seconds = excluded.msg_window_seconds,
  token_cap = excluded.token_cap,
  token_window_seconds = excluded.token_window_seconds,
  reset_anchor = excluded.reset_anchor,
  overage_pricing_per_mtok_in = excluded.overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out = excluded.overage_pricing_per_mtok_out,
  notes = excluded.notes,
  effective_from = excluded.effective_from,
  effective_until = excluded.effective_until,
  path_kind = excluded.path_kind,
  parent_plan_id = excluded.parent_plan_id;
