# Archive: Background Workers

**Source**: `mnemos-production.git.broken` / feature/background-embedding-job (Feb 2026)
**Status**: NOT WIRED — production distillation_worker.py handles compression but not NULL-embedding backfill
**Integration effort**: MEDIUM

## Files

### `background_embedding_job.py` (413 lines)
Async background thread that continuously backfills NULL-embedding memories.
- Queries: `SELECT id, content FROM memories WHERE embedding IS NULL LIMIT {batch_size}`
- For content >10KB: pre-compresses via inference backend (`compress_content_sync()`) before embedding
- Embeds via local Ollama (same endpoint as production distillation_worker)
- Configurable batch size, sleep interval, retry logic
- **Key pattern worth extracting**: `compress_content_sync()` — synchronous inference backend call
  for large memories. Currently production only compresses async for distillation; this covers
  the case where large memories land with no compression pass before embedding.

### `git_distillation_job.py` (230 lines)
Reads git commit history from local repos, extracts architectural facts.
- Categorizes commits: `feat` / `fix` / `refactor` / `test` / `docs`
- Extracts patterns: what changed, why, what files
- Outputs structured JSON per commit
- **Wire into**: periodic cron or `tools/` script to auto-archive MNEMOS git history

### `git_to_mnemos.py` (75 lines)
Bridge: runs `git_distillation_job.py` and POST each fact to `/memories`.
- Handles authentication header
- Idempotent (uses content hash to skip duplicates via search-before-insert)
- **Wire into**: `tools/` directory; run as `python git_to_mnemos.py /path/to/repo`

## Notes

- `background_embedding_job.py` has hardcoded inference backend URL ($INFERENCE_BACKEND_URL or localhost:8000) — already
  matches infrastructure. Needs API key wired from config.
- `git_to_mnemos.py` has hardcoded MNEMOS URL (localhost:5000) — update to 5002
- `llmlingua2_integration.py` was intentionally NOT copied — requires 600MB BERT download,
  no meaningful advantage over extractive token filter+SENTENCE at current scale
