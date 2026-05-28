-- migration: 0035_subscription_plans_date_aware
-- Db2 mirror of Oracle 0035_subscription_plans_date_aware.

ALTER TABLE subscription_plans ADD COLUMN effective_from DATE NOT NULL DEFAULT '2026-01-01';
ALTER TABLE subscription_plans ADD COLUMN effective_until DATE;
ALTER TABLE subscription_plans ADD COLUMN path_kind VARCHAR(64) NOT NULL DEFAULT 'api';
ALTER TABLE subscription_plans ADD COLUMN parent_plan_id VARCHAR(128);
ALTER TABLE subscription_plans ADD CONSTRAINT ck_subscription_plans_path_kind
  CHECK (path_kind IN ('interactive', 'sdk_credit_pool', 'api', 'unmetered', 'free'));
ALTER TABLE usage_ledger ADD COLUMN path_kind VARCHAR(64) NOT NULL DEFAULT 'api';

UPDATE subscription_plans
SET effective_from = DATE('2026-04-01'),
    effective_until = DATE('2026-05-31'),
    path_kind = 'interactive'
WHERE provider = 'anthropic'
  AND plan_name = 'claude_max_200';

MERGE INTO subscription_plans AS dst
USING (
  VALUES
    ('anthropic', 'claude_max_100', 'subscription', 100, 450, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Claude Max 100: 450 messages per 5h window until 2026-06-14',
     DATE('2026-06-01'), DATE('2026-06-14'), 'interactive', 'claude_max_200'),
    ('anthropic', 'claude_max_interactive_post_jun15', 'subscription', 100, 450, 18000, NULL, NULL,
     'rolling', NULL, NULL, 'Claude Max interactive plan after 2026-06-15',
     DATE('2026-06-15'), NULL, 'interactive', 'claude_max_100'),
    ('anthropic', 'agent_sdk_credit_pool_post_jun15', 'subscription', 0, NULL, NULL, NULL, NULL,
     'monthly', NULL, NULL, 'Claude Max Agent SDK credit pool after 2026-06-15',
     DATE('2026-06-15'), NULL, 'sdk_credit_pool', 'claude_max_100'),
    ('xai', 'supergrok', 'subscription', 30, NULL, NULL, NULL, NULL,
     'monthly', NULL, NULL, 'xAI SuperGrok interactive subscription',
     DATE('2026-05-19'), NULL, 'interactive', NULL)
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
