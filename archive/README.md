# MNEMOS Archive — Salvaged Code from Pre-Refactor History

**Status**: ARCHIVED — Do not import directly. Review and adapt before use.
**Audited**: 2026-04-12
**Source repos**: `mnemos.git` (2026-02-18 snapshot), `mnemos-production.git.broken` (feature/background-embedding-job, Feb 2026), `graeae.git.old` (Jan–Feb 2026), `argonas/launch-prep` branch

These files were extracted from old repos that pre-date the current architecture.
They are **not wired** into the production codebase. Each subdirectory has a README
explaining what the code does, its integration status, and how to port it if needed.

---

## What was archived (old repos, now at `/mnt/datapool/git/ARCHIVED/`)

| Repo | Age | Why archived |
|------|-----|--------------|
| `mnemos.git` | Feb 2026 single-commit snapshot | Pre-refactor flat layout; superseded by current `api/handlers/` architecture |
| `mnemos-production.git.broken` | Feb 2026 feature branch | Unmerged background-embedding experiment; key pieces extracted here |
| `graeae.git.old` | Jan–Feb 2026 | Standalone GRAEAE repo before consolidation into MNEMOS; superseded |

---

## Salvage priority

| Directory | Priority | Integration effort |
|-----------|----------|--------------------|
| `metrics/` | **HIGH** — zero observability in production | Low — add `/metrics` route to `api_server.py` |
| `caches/` | **HIGH** — no embedding/compression caching today | Medium — wire into `distillation_worker.py` and search path |
| `background_workers/` | **MEDIUM** — NULL-embedding backfill not in prod | Medium — adapt config, integrate with lifecycle |
| `graeae_improvements/` | **MEDIUM** — better provider routing | High — review against current `graeae/engine.py` first |
| `tests/` | **HIGH** — 40+ tests for untested modules | Low — run `pytest archive/tests/` after review |
