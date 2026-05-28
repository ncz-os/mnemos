-- migration: 0033_subscription_plans
-- Adds KNEMON subscription-plan catalog used for utilization and amortized cost tracking.

DECLARE
  n NUMBER;
BEGIN
  SELECT COUNT(*) INTO n FROM user_tables WHERE table_name = 'SUBSCRIPTION_PLANS';
  IF n = 0 THEN
    EXECUTE IMMEDIATE '
      CREATE TABLE subscription_plans (
        provider VARCHAR2(128) NOT NULL,
        plan_name VARCHAR2(128) NOT NULL,
        auth_method VARCHAR2(32) DEFAULT ''api'' NOT NULL,
        monthly_usd NUMBER(12,2),
        msg_cap NUMBER,
        msg_window_seconds NUMBER,
        token_cap NUMBER,
        token_window_seconds NUMBER,
        reset_anchor VARCHAR2(32),
        overage_pricing_per_mtok_in NUMBER,
        overage_pricing_per_mtok_out NUMBER,
        notes VARCHAR2(4000),
        CONSTRAINT pk_subscription_plans PRIMARY KEY (provider, plan_name),
        CONSTRAINT ck_subscription_plans_auth_method
          CHECK (auth_method IN (''subscription'', ''api'', ''free'', ''token''))
      )';
  END IF;
END;
/

MERGE INTO subscription_plans dst
USING (
  SELECT 'anthropic' provider, 'claude_max_200' plan_name, 'subscription' auth_method, 200 monthly_usd,
         900 msg_cap, 18000 msg_window_seconds, NULL token_cap, NULL token_window_seconds,
         'rolling' reset_anchor, NULL overage_pricing_per_mtok_in, NULL overage_pricing_per_mtok_out,
         'Claude Max 200: 900 messages per 5h window' notes FROM dual
  UNION ALL SELECT 'anthropic', 'claude_max_100', 'subscription', 100, 450, 18000, NULL, NULL,
         'rolling', NULL, NULL, 'Claude Max 100: 450 messages per 5h window until 2026-06-01' FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_plus', 'subscription', 20, 40, 10800, NULL, NULL,
         'rolling', NULL, NULL, 'ChatGPT Plus: 40 messages per 3h window' FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_pro', 'subscription', 200, NULL, NULL, 200, 604800,
         'weekly', NULL, NULL, 'ChatGPT Pro: unmetered plus 200 GPT-5 messages per week' FROM dual
  UNION ALL SELECT 'nvidia', 'ngc_integrate', 'free', 0, NULL, NULL, NULL, NULL,
         'monthly', 0, 0, 'NVIDIA NGC Integrate free tier' FROM dual
  UNION ALL SELECT 'nvidia', 'ngc_inference', 'free', 0, NULL, NULL, NULL, NULL,
         'monthly', 0, 0, 'NVIDIA NGC Inference free tier' FROM dual
  UNION ALL SELECT 'groq', 'dev_tier', 'token', NULL, NULL, NULL, NULL, NULL,
         'monthly', NULL, NULL, 'Groq developer tier token-based usage' FROM dual
  UNION ALL SELECT 'together', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
         'monthly', NULL, NULL, 'Together API token-based usage' FROM dual
  UNION ALL SELECT 'deepseek-direct', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
         'monthly', NULL, NULL, 'DeepSeek direct API token-based usage' FROM dual
  UNION ALL SELECT 'xai', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
         'monthly', NULL, NULL, 'xAI API token-based usage' FROM dual
  UNION ALL SELECT 'gemini', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
         'monthly', NULL, NULL, 'Gemini API token-based usage' FROM dual
  UNION ALL SELECT 'perplexity', 'api', 'api', NULL, NULL, NULL, NULL, NULL,
         'monthly', NULL, NULL, 'Perplexity API token-based plus search usage' FROM dual
) src
ON (dst.provider = src.provider AND dst.plan_name = src.plan_name)
WHEN NOT MATCHED THEN INSERT (
  provider, plan_name, auth_method, monthly_usd, msg_cap, msg_window_seconds,
  token_cap, token_window_seconds, reset_anchor, overage_pricing_per_mtok_in,
  overage_pricing_per_mtok_out, notes
) VALUES (
  src.provider, src.plan_name, src.auth_method, src.monthly_usd, src.msg_cap, src.msg_window_seconds,
  src.token_cap, src.token_window_seconds, src.reset_anchor, src.overage_pricing_per_mtok_in,
  src.overage_pricing_per_mtok_out, src.notes
);
