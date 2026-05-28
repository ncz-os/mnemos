-- migration: 0037_deepseek_direct_provider_seed
-- PostgreSQL mirror of Oracle 0037_deepseek_direct_provider_seed.

DELETE FROM model_registry
WHERE provider LIKE 'parity_postgres_%';

INSERT INTO model_registry (
  provider, model_id, display_name, family, context_window,
  max_output_tokens, input_cost_per_mtok, output_cost_per_mtok,
  cache_read_per_mtok, capabilities, available, deprecated, arena_score,
  arena_rank, graeae_weight, first_seen, last_seen, last_synced, raw
) VALUES
  (
    'deepseek-direct', 'deepseek-v4-flash', 'DeepSeek V4 Flash',
    'deepseek-v4', 128000, 8192, 0.14, 0.28, 0.0028,
    ARRAY['chat', 'coding']::TEXT[], TRUE, FALSE, NULL, NULL, 0.7,
    NOW(), NOW(), NOW(),
    '{"source":"0037_deepseek_direct_provider_seed","capabilities":{"chat":true,"coding":true,"reasoning":false}}'::JSONB
  ),
  (
    'deepseek-direct', 'deepseek-v4-pro', 'DeepSeek V4 Pro',
    'deepseek-v4', 128000, 8192, 0.435, 0.87, 0.003625,
    ARRAY['chat', 'coding', 'reasoning']::TEXT[], TRUE, FALSE, NULL, NULL, 0.85,
    NOW(), NOW(), NOW(),
    '{"source":"0037_deepseek_direct_provider_seed","pricing_note":"DeepSeek V4 Pro promo pricing effective until 2026-05-31; then input/output costs revert to 1.74/3.48 USD per mtok.","capabilities":{"chat":true,"coding":true,"reasoning":true,"promo_until":"2026-05-31","post_promo_input_cost_per_mtok":1.74,"post_promo_output_cost_per_mtok":3.48}}'::JSONB
  )
ON CONFLICT (provider, model_id) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  family = EXCLUDED.family,
  context_window = EXCLUDED.context_window,
  max_output_tokens = EXCLUDED.max_output_tokens,
  input_cost_per_mtok = EXCLUDED.input_cost_per_mtok,
  output_cost_per_mtok = EXCLUDED.output_cost_per_mtok,
  cache_read_per_mtok = EXCLUDED.cache_read_per_mtok,
  capabilities = EXCLUDED.capabilities,
  available = EXCLUDED.available,
  deprecated = EXCLUDED.deprecated,
  arena_score = EXCLUDED.arena_score,
  arena_rank = EXCLUDED.arena_rank,
  graeae_weight = EXCLUDED.graeae_weight,
  last_seen = NOW(),
  last_synced = NOW(),
  raw = EXCLUDED.raw;
