ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS gateway_provider TEXT, ADD COLUMN IF NOT EXISTS gateway_model TEXT, ADD COLUMN IF NOT EXISTS cost NUMERIC
ADD (
    session_id VARCHAR2(64),
    request_count NUMBER DEFAULT 1,
    plan_window_id VARCHAR2(64)
);

CREATE TABLE subscription_plans (
    provider VARCHAR2(64),
    plan_name VARCHAR2(64),
    monthly_usd NUMBER,
    msg_cap NUMBER,
    msg_window_seconds NUMBER,
    token_cap NUMBER,
    token_window_seconds NUMBER,
    reset_anchor VARCHAR2(64),
    overage_pricing_per_mtok_in NUMBER,
    overage_pricing_per_mtok_out NUMBER,
    notes CLOB
);

-- Seed data
INSERT INTO subscription_plans VALUES ('claude', 'max_200', 100, 900, 450, 5, NULL, '5-hour window', 0.01, 0.015, 'Default Claude plan');
INSERT INTO subscription_plans VALUES ('chatgpt', 'plus', 19.99, 40, 10800, NULL, NULL, '3-hour window', 0.01, 0.02, 'Monthly subscription');
INSERT INTO subscription_plans VALUES ('chatgpt', 'pro', 99.99, -1, NULL, NULL, NULL, 'Unlimited', 0.005, 0.01, '200 additional credits/week');
INSERT INTO subscription_plans VALUES ('ngc', 'integrate', 0, -1, NULL, NULL, NULL, NULL, 0, 0, 'Free tier');
INSERT INTO subscription_plans VALUES ('groq', 'dev_tier', 0, -1, NULL, 1000000, 3600, '1-hour token bucket', 0.001, 0.002, 'Token-based pricing');
INSERT INTO subscription_plans VALUES ('together', 'api', 0, -1, NULL, NULL, NULL, NULL, 0.001, 0.002, 'Token-only API pricing');