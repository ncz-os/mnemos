-- migration: 0037_deepseek_direct_provider_seed
-- Db2 mirror of Oracle 0037_deepseek_direct_provider_seed.

DELETE FROM model_registry
WHERE provider LIKE 'parity_postgres_%';

MERGE INTO model_registry AS dst
USING (
  VALUES
    (
      'deepseek-direct', 'deepseek-v4-flash', 'DeepSeek V4 Flash',
      'deepseek-v4', 128000, 8192, 0.14, 0.28, 0.0028,
      '{"chat":true,"coding":true,"reasoning":false}',
      1, 0, NULL, NULL, 0.7,
      '{"source":"0037_deepseek_direct_provider_seed"}'
    ),
    (
      'deepseek-direct', 'deepseek-v4-pro', 'DeepSeek V4 Pro',
      'deepseek-v4', 128000, 8192, 0.435, 0.87, 0.003625,
      '{"chat":true,"coding":true,"reasoning":true,"promo_until":"2026-05-31","post_promo_input_cost_per_mtok":1.74,"post_promo_output_cost_per_mtok":3.48}',
      1, 0, NULL, NULL, 0.85,
      '{"source":"0037_deepseek_direct_provider_seed","pricing_note":"DeepSeek V4 Pro promo pricing effective until 2026-05-31; then input/output costs revert to 1.74/3.48 USD per mtok."}'
    )
) AS src (
  provider, model_id, display_name, family, context_window,
  max_output_tokens, input_cost_per_mtok, output_cost_per_mtok,
  cache_read_per_mtok, capabilities, available, deprecated, arena_score,
  arena_rank, graeae_weight, raw_payload
)
ON dst.provider = src.provider AND dst.model_id = src.model_id
WHEN MATCHED THEN UPDATE SET
  display_name = src.display_name,
  family = src.family,
  context_window = src.context_window,
  max_output_tokens = src.max_output_tokens,
  input_cost_per_mtok = src.input_cost_per_mtok,
  output_cost_per_mtok = src.output_cost_per_mtok,
  cache_read_per_mtok = src.cache_read_per_mtok,
  capabilities = src.capabilities,
  available = src.available,
  deprecated = src.deprecated,
  arena_score = src.arena_score,
  arena_rank = src.arena_rank,
  graeae_weight = src.graeae_weight,
  last_seen = CURRENT TIMESTAMP,
  last_synced = CURRENT TIMESTAMP,
  raw_payload = src.raw_payload
WHEN NOT MATCHED THEN INSERT (
  id, provider, model_id, display_name, family, context_window,
  max_output_tokens, input_cost_per_mtok, output_cost_per_mtok,
  cache_read_per_mtok, capabilities, available, deprecated, arena_score,
  arena_rank, graeae_weight, first_seen, last_seen, last_synced, raw_payload
) VALUES (
  HEX(GENERATE_UNIQUE()), src.provider, src.model_id, src.display_name,
  src.family, src.context_window, src.max_output_tokens,
  src.input_cost_per_mtok, src.output_cost_per_mtok, src.cache_read_per_mtok,
  src.capabilities, src.available, src.deprecated, src.arena_score,
  src.arena_rank, src.graeae_weight, CURRENT TIMESTAMP, CURRENT TIMESTAMP,
  CURRENT TIMESTAMP, src.raw_payload
);
