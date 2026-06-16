from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemos.domain.pantheon import pricing
from mnemos.domain.pantheon.pricing import PricingRecord


def test_parse_openrouter_models_normalizes_vendor_prices_and_context():
    records = pricing.parse_openrouter_models(
        {
            "data": [
                {
                    "id": "openai/gpt-4o-mini",
                    "pricing": {"prompt": "0.00000015", "completion": "0.00000060"},
                    "context_length": 128000,
                    "top_provider": {"max_completion_tokens": 16384},
                    "architecture": {"input_modalities": ["text", "image"]},
                    "supported_parameters": ["tools", "response_format"],
                }
            ]
        },
        fetched_at="2026-06-14T00:00:00Z",
    )

    assert records == [
        PricingRecord(
            provider="openai",
            model="gpt-4o-mini",
            input_cost_per_mtok=0.15,
            output_cost_per_mtok=0.6,
            context_window=128000,
            max_output_tokens=16384,
            capabilities=("chat", "json", "tools", "vision"),
            source="openrouter",
            fetched_at="2026-06-14T00:00:00Z",
            raw_model_id="openai/gpt-4o-mini",
        )
    ]


def test_parse_litellm_and_models_dev_adapters():
    litellm_records = pricing.parse_litellm_prices(
        {
            "claude-3-5-sonnet-latest": {
                "litellm_provider": "anthropic",
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000015,
                "max_input_tokens": 200000,
                "max_output_tokens": 8192,
            }
        },
        fetched_at="now",
    )
    assert litellm_records[0].provider == "anthropic"
    assert litellm_records[0].input_cost_per_mtok == 3.0
    assert litellm_records[0].output_cost_per_mtok == 15.0
    assert litellm_records[0].context_window == 200000

    models_dev_records = pricing.parse_models_dev(
        {
            "google": {
                "models": {
                    "gemini-2.5-pro": {
                        "cost": {"input": 1.25, "output": 10.0},
                        "limit": {"context": 1048576, "output": 65536},
                    }
                }
            }
        },
        fetched_at="now",
    )
    assert models_dev_records[0].provider == "gemini"
    assert models_dev_records[0].model == "gemini-2.5-pro"
    assert models_dev_records[0].input_cost_per_mtok == 1.25
    assert models_dev_records[0].output_cost_per_mtok == 10.0


def test_primary_pricing_sources_prefer_litellm_and_reserve_tokencost():
    assert [fetcher.name for fetcher in pricing.default_pricing_fetchers()] == [
        "litellm",
        "models.dev",
        "openrouter",
    ]
    assert pricing.SOURCE_PRIORITY["tokencost"] > pricing.SOURCE_PRIORITY["litellm"]
    assert pricing.SOURCE_PRIORITY["litellm"] > pricing.SOURCE_PRIORITY["models.dev"]
    assert pricing.SOURCE_PRIORITY["models.dev"] > pricing.SOURCE_PRIORITY["openrouter"]

    chosen = pricing.pricing_index(
        [
            PricingRecord(
                provider="openai",
                model="gpt-4o-mini",
                input_cost_per_mtok=99.0,
                output_cost_per_mtok=99.0,
                source="models.dev",
            ),
            PricingRecord(
                provider="openai",
                model="gpt-4o-mini",
                input_cost_per_mtok=0.15,
                output_cost_per_mtok=0.6,
                source="litellm",
            ),
        ]
    )[("openai", "gpt-4o-mini")]

    assert chosen.source == "litellm"
    assert chosen.input_cost_per_mtok == 0.15


def test_merge_pricing_into_catalog_aliases_and_marks_source():
    merged = pricing.merge_pricing_into_catalog(
        [
            {
                "id": "gemini-2.5-pro",
                "provider": "gemini",
                "registry_provider": "gemini",
                "capabilities": ["chat"],
                "cost_per_mtok": None,
            }
        ],
        [
            PricingRecord(
                provider="google",
                model="gemini-2.5-pro",
                input_cost_per_mtok=1.25,
                output_cost_per_mtok=10.0,
                context_window=1048576,
                max_output_tokens=65536,
                capabilities=("vision", "reasoning"),
                source="models.dev",
                fetched_at="2026-06-14T00:00:00Z",
                raw_model_id="gemini-2.5-pro",
            )
        ],
    )

    row = merged[0]
    assert row["price_in"] == 1.25
    assert row["price_out"] == 10.0
    assert row["cost_per_mtok"] == 5.625
    assert row["context_window"] == 1048576
    assert row["model_max_ctx"] == 1048576
    assert row["max_output_tokens"] == 65536
    assert row["capabilities"] == ["chat", "reasoning", "vision"]
    assert row["pricing_source"] == "models.dev"
    assert row["pricing_fetched_at"] == "2026-06-14T00:00:00Z"


def test_fetch_pricing_records_tolerates_per_source_failure():
    class GoodFetcher:
        name = "good"

        async def fetch(self, client):
            return [PricingRecord(provider="openai", model="gpt-4o", source="good")]

    class BadFetcher:
        name = "bad"

        async def fetch(self, client):
            raise RuntimeError("boom")

    records, status = asyncio.run(pricing.fetch_pricing_records([BadFetcher(), GoodFetcher()]))
    assert [record.source for record in records] == ["good"]
    assert status[0]["source"] == "bad"
    assert status[0]["ok"] is False
    assert "boom" in status[0]["error"]
    assert status[1]["source"] == "good"
    assert status[1]["ok"] is True


def test_write_json_cache_falls_back_to_user_cache_when_primary_unwritable(monkeypatch, tmp_path: Path):
    primary = tmp_path / "blocked" / "pantheon-catalog.json"
    xdg_home = tmp_path / "xdg"
    fallback = xdg_home / "mnemos" / "pantheon-catalog.json"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_home))

    real_atomic_write = pricing._atomic_write_text

    def fake_atomic_write(path, text):
        if Path(path) == primary:
            raise PermissionError("denied")
        real_atomic_write(path, text)

    monkeypatch.setattr(pricing, "_atomic_write_text", fake_atomic_write)

    pricing.write_json_cache({"schema": "mnemos.pantheon.catalog.v1", "models": [{"id": "cached"}]}, primary)

    assert json.loads(fallback.read_text(encoding="utf-8"))["models"] == [{"id": "cached"}]
    assert pricing.read_json_cache(primary)["models"] == [{"id": "cached"}]


def test_catalog_cache_paths_prefer_pricing_cache_env(monkeypatch, tmp_path: Path):
    from mnemos.domain.pantheon import catalog

    pricing_cache = tmp_path / "pricing-writes-here.json"
    legacy_cache = tmp_path / "legacy-reader.json"
    monkeypatch.setenv("PANTHEON_CATALOG_CACHE", str(pricing_cache))
    monkeypatch.setenv("MNEMOS_PANTHEON_CATALOG_CACHE_PATH", str(legacy_cache))
    monkeypatch.setattr(
        catalog,
        "get_settings",
        lambda: SimpleNamespace(pantheon=SimpleNamespace(catalog_cache_path=None)),
    )

    paths = catalog._catalog_cache_paths()  # noqa: SLF001

    assert paths[0] == pricing_cache
    assert legacy_cache in paths


@pytest.mark.asyncio
async def test_regenerate_catalog_cache_keeps_last_good_on_total_refresh_failure(monkeypatch, tmp_path: Path):
    cache_path = tmp_path / "pantheon-catalog.json"
    last_good = {"schema": "mnemos.pantheon.catalog.v1", "generated_at": "old", "models": [{"id": "old"}]}
    cache_path.write_text(json.dumps(last_good), encoding="utf-8")

    async def fake_list_models(**_kwargs):
        return [{"id": "gpt-4o", "provider": "openai", "registry_provider": "openai"}]

    async def fake_fetch(fetchers=None):
        return [], [{"source": "bad", "ok": False, "error": "offline", "records": 0}]

    monkeypatch.setattr("mnemos.domain.pantheon.catalog.list_models", fake_list_models)
    monkeypatch.setattr(pricing, "fetch_pricing_records", fake_fetch)

    payload = await pricing.regenerate_catalog_cache(json_path=cache_path, sqlite_path=None)

    assert payload["models"] == [{"id": "old"}]
    assert payload["stale"] is True
    assert payload["last_refresh_error"] == "no external pricing records fetched"
    assert json.loads(cache_path.read_text(encoding="utf-8")) == last_good


@pytest.mark.asyncio
async def test_regenerate_catalog_cache_uses_seed_when_feeds_and_cache_missing(monkeypatch, tmp_path: Path):
    cache_path = tmp_path / "pantheon-catalog.json"
    seed_path = tmp_path / "seed.json"
    seed = {
        "schema": "mnemos.pantheon.catalog.v1",
        "generated_at": "seed",
        "sources": [{"source": "seed", "ok": True, "records": 1}],
        "models": [{"id": "seed-model", "provider": "openai", "registry_provider": "openai"}],
    }
    seed_path.write_text(json.dumps(seed), encoding="utf-8")

    async def fake_list_models(**_kwargs):
        return [{"id": "gpt-4o", "provider": "openai", "registry_provider": "openai"}]

    async def fake_fetch(fetchers=None):
        return [], [{"source": "litellm", "ok": False, "error": "offline", "records": 0}]

    monkeypatch.setattr("mnemos.domain.pantheon.catalog.list_models", fake_list_models)
    monkeypatch.setattr(pricing, "fetch_pricing_records", fake_fetch)

    payload = await pricing.regenerate_catalog_cache(json_path=cache_path, sqlite_path=None, seed_path=seed_path)

    assert payload["models"] == seed["models"]
    assert payload["stale"] is True
    assert payload["seed_fallback"] is True
    assert payload["refresh_ok"] is False
    assert payload["last_refresh_error"] == "no external pricing records fetched"
    assert json.loads(cache_path.read_text(encoding="utf-8"))["models"] == seed["models"]


@pytest.mark.asyncio
async def test_live_catalog_reads_synced_json_cache(monkeypatch):
    from mnemos.domain.pantheon import catalog

    cached = {
        "schema": "mnemos.pantheon.catalog.v1",
        "generated_at": "now",
        "sources": [{"source": "litellm", "ok": True, "records": 1}],
        "models": [
            {
                "id": "cached-gpt",
                "object": "model",
                "created": 1,
                "owned_by": "openai",
                "provider": "openai",
                "registry_provider": "openai",
                "display_name": "Cached GPT",
                "capabilities": ["chat"],
                "usage_tier": "budget",
                "cost_per_mtok": 0.3,
                "price_in": 0.1,
                "price_out": 0.5,
                "input_cost_per_mtok": 0.1,
                "output_cost_per_mtok": 0.5,
                "quality_score": 0.9,
                "available": True,
                "deprecated": False,
                "pricing_source": "litellm",
                "health": {},
            }
        ],
    }

    monkeypatch.setattr(pricing, "read_json_cache", lambda *_args, **_kwargs: cached)

    models = await catalog.list_models()
    response = await catalog.models_response()

    cached_model = next(model for model in models if model["id"] == "cached-gpt")
    response_model = next(model for model in response["data"] if model["id"] == "cached-gpt")

    assert cached_model["pricing_source"] == "litellm"
    assert response_model["pricing_source"] == "litellm"


@pytest.mark.asyncio
async def test_catalog_includes_codex_fleet_model_with_synced_cache_pricing(monkeypatch):
    from mnemos.domain.pantheon import catalog

    class _Engine:
        providers = {
            "openai": {
                "url": "https://api.openai.com/v1/chat/completions",
                "model": "gpt-5.5",
                "weight": 0.88,
                "api": "openai",
                "key_name": "openai",
                "input_cost_per_mtok": 5.0,
                "output_cost_per_mtok": 30.0,
            }
        }

        def provider_status(self):
            return {}

    cached = {
        "schema": "mnemos.pantheon.catalog.v1",
        "generated_at": "now",
        "sources": [{"source": "litellm", "ok": True, "records": 8383}],
        "models": [
            {
                "id": "gpt-5.3-codex",
                "provider": "openai",
                "registry_provider": "openai",
                "display_name": "GPT-5.3 Codex",
                "capabilities": ["code", "reasoning", "tools"],
                "price_in": 1.1,
                "price_out": 4.4,
                "input_cost_per_mtok": 1.1,
                "output_cost_per_mtok": 4.4,
                "cost_per_mtok": 2.75,
                "context_window": 200000,
                "max_output_tokens": 100000,
                "pricing_source": "litellm",
                "pricing_raw_model_id": "gpt-5.3-codex",
            }
        ],
    }

    monkeypatch.setattr(catalog, "get_graeae_engine", lambda: _Engine())
    monkeypatch.setattr(catalog._lc, "_pool", None)
    monkeypatch.setattr(catalog, "get_settings", lambda: SimpleNamespace(pantheon=SimpleNamespace(passthrough_provider="nvidia")))
    monkeypatch.setattr(pricing, "read_json_cache", lambda *_args, **_kwargs: cached)

    models = await catalog.list_models()
    by_id = {model["id"]: model for model in models}

    assert "gpt-5.5" in by_id
    assert by_id["gpt-5.3-codex"]["provider"] == "nvidia"
    assert by_id["gpt-5.3-codex"]["model_id"] == "openai/openai/gpt-5.3-codex"
    assert by_id["gpt-5.3-codex"]["price_in"] == 1.1
    assert by_id["gpt-5.3-codex"]["price_out"] == 4.4
    assert by_id["gpt-5.3-codex"]["pricing_source"] == "litellm"
