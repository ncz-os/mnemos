-- migration: 0035_subscription_plans_date_aware
-- Adds date-aware KNEMON plan transitions and ledger path classification.

DECLARE
  n NUMBER;
BEGIN
  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'SUBSCRIPTION_PLANS' AND column_name = 'EFFECTIVE_FROM';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE subscription_plans ADD effective_from DATE DEFAULT DATE ''2026-01-01'' NOT NULL';
  END IF;

  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'SUBSCRIPTION_PLANS' AND column_name = 'EFFECTIVE_UNTIL';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE subscription_plans ADD effective_until DATE NULL';
  END IF;

  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'SUBSCRIPTION_PLANS' AND column_name = 'PATH_KIND';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE subscription_plans ADD path_kind VARCHAR2(64) DEFAULT ''api'' NOT NULL CHECK (path_kind IN (''interactive'', ''sdk_credit_pool'', ''api'', ''unmetered'', ''free''))';
  END IF;

  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'SUBSCRIPTION_PLANS' AND column_name = 'PARENT_PLAN_ID';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE subscription_plans ADD parent_plan_id VARCHAR2(128) NULL';
  END IF;

  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'USAGE_LEDGER' AND column_name = 'PATH_KIND';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE usage_ledger ADD path_kind VARCHAR2(64) DEFAULT ''api'' NOT NULL';
  END IF;
END;
/

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

MERGE INTO subscription_plans dst
USING (
  SELECT 'anthropic' provider, 'claude_max_100' plan_name, 'subscription' auth_method,
         100 monthly_usd, 225 msg_cap, 18000 msg_window_seconds,
         NULL token_cap, NULL token_window_seconds, 'rolling' reset_anchor,
         NULL overage_pricing_per_mtok_in, NULL overage_pricing_per_mtok_out,
         'Claude Max 100 (5x): at least 225 messages per 5h window until 2026-06-14' notes,
         DATE '2026-06-01' effective_from, DATE '2026-06-14' effective_until,
         'interactive' path_kind, 'claude_max_200' parent_plan_id FROM dual
  UNION ALL SELECT 'anthropic', 'claude_max_interactive_post_jun15', 'subscription',
         100, 225, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'Claude Max 100 (5x) interactive plan after 2026-06-15',
         DATE '2026-06-15', NULL, 'interactive', 'claude_max_100' FROM dual
  UNION ALL SELECT 'anthropic', 'agent_sdk_credit_pool_post_jun15', 'subscription',
         0, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL,
         'Claude Max Agent SDK credit pool after 2026-06-15',
         DATE '2026-06-15', NULL, 'sdk_credit_pool', 'claude_max_100' FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_pro_100_codex_promo', 'subscription',
         100, 160, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'ChatGPT Codex Pro $100 launch promo: conservative GPT-5.5 local-message floor per 5h through 2026-05-31',
         DATE '2026-05-01', DATE '2026-05-31', 'interactive', 'chatgpt_pro_100_codex' FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_pro_100_codex', 'subscription',
         100, 80, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'ChatGPT Codex Pro $100: conservative GPT-5.5 local-message floor per 5h from 2026-06-01',
         DATE '2026-06-01', NULL, 'interactive', 'chatgpt_pro' FROM dual
  UNION ALL SELECT 'openai', 'chatgpt_pro_200_codex', 'subscription',
         200, 300, 18000, NULL, NULL, 'rolling', NULL, NULL,
         'ChatGPT Codex Pro $200: conservative GPT-5.5 local-message floor per 5h from 2026-06-01',
         DATE '2026-06-01', NULL, 'interactive', 'chatgpt_pro' FROM dual
  UNION ALL SELECT 'xai', 'supergrok', 'subscription',
         30, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL,
         'xAI SuperGrok interactive subscription',
         DATE '2026-05-19', NULL, 'interactive', NULL FROM dual
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
