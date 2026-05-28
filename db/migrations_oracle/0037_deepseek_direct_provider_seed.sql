-- migration: 0037_deepseek_direct_provider_seed
-- Seed DeepSeek direct models and remove parity_postgres test residue.

DELETE FROM model_registry
WHERE provider LIKE 'parity_postgres_%';

MERGE INTO model_registry dst
USING (
  SELECT 'deepseek-direct' provider,
         'deepseek-v4-flash' model_id,
         'DeepSeek V4 Flash' display_name,
         'deepseek-v4' family,
         128000 context_window,
         8192 max_output_tokens,
         0.14 input_cost_per_mtok,
         0.28 output_cost_per_mtok,
         0.0028 cache_read_per_mtok,
         '{"chat":true,"coding":true,"reasoning":false}' capabilities,
         1 available,
         0 deprecated,
         CAST(NULL AS NUMBER) arena_score,
         CAST(NULL AS NUMBER) arena_rank,
         0.7 graeae_weight,
         '{"source":"0037_deepseek_direct_provider_seed"}' raw_payload
  FROM dual
  UNION ALL
  SELECT 'deepseek-direct',
         'deepseek-v4-pro',
         'DeepSeek V4 Pro',
         'deepseek-v4',
         128000,
         8192,
         0.435,
         0.87,
         0.003625,
         '{"chat":true,"coding":true,"reasoning":true,"promo_until":"2026-05-31","post_promo_input_cost_per_mtok":1.74,"post_promo_output_cost_per_mtok":3.48}',
         1,
         0,
         CAST(NULL AS NUMBER),
         CAST(NULL AS NUMBER),
         0.85,
         '{"source":"0037_deepseek_direct_provider_seed","pricing_note":"DeepSeek V4 Pro promo pricing effective until 2026-05-31; then input/output costs revert to 1.74/3.48 USD per mtok."}'
  FROM dual
) src
ON (dst.provider = src.provider AND dst.model_id = src.model_id)
WHEN MATCHED THEN UPDATE SET
  dst.display_name = src.display_name,
  dst.family = src.family,
  dst.context_window = src.context_window,
  dst.max_output_tokens = src.max_output_tokens,
  dst.input_cost_per_mtok = src.input_cost_per_mtok,
  dst.output_cost_per_mtok = src.output_cost_per_mtok,
  dst.cache_read_per_mtok = src.cache_read_per_mtok,
  dst.capabilities = src.capabilities,
  dst.available = src.available,
  dst.deprecated = src.deprecated,
  dst.arena_score = src.arena_score,
  dst.arena_rank = src.arena_rank,
  dst.graeae_weight = src.graeae_weight,
  dst.last_seen = SYSTIMESTAMP,
  dst.last_synced = SYSTIMESTAMP,
  dst.raw_payload = src.raw_payload
WHEN NOT MATCHED THEN INSERT (
  id, provider, model_id, display_name, family, context_window,
  max_output_tokens, input_cost_per_mtok, output_cost_per_mtok,
  cache_read_per_mtok, capabilities, available, deprecated, arena_score,
  arena_rank, graeae_weight, first_seen, last_seen, last_synced, raw_payload
) VALUES (
  LOWER(RAWTOHEX(SYS_GUID())), src.provider, src.model_id, src.display_name, src.family,
  src.context_window, src.max_output_tokens, src.input_cost_per_mtok,
  src.output_cost_per_mtok, src.cache_read_per_mtok, src.capabilities,
  src.available, src.deprecated, src.arena_score, src.arena_rank,
  src.graeae_weight, SYSTIMESTAMP, SYSTIMESTAMP, SYSTIMESTAMP,
  src.raw_payload
);
