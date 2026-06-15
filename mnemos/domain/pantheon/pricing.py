"""External pricing ingest for the PANTHEON catalog data layer.

The refresh job fetches public machine-readable price feeds, normalizes them
to PANTHEON's catalog shape, merges them onto the local base catalog, and
writes a last-good cache consumed by the live catalog/gateway path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

import httpx

from mnemos.core.numeric import safe_float

logger = logging.getLogger(__name__)

DEFAULT_JSON_CACHE = Path(os.environ.get("PANTHEON_CATALOG_CACHE", "/var/lib/mnemos/pantheon-catalog.json"))
DEFAULT_SQLITE_CACHE = Path(os.environ.get("PANTHEON_CATALOG_SQLITE", "/var/lib/mnemos/pantheon-catalog.sqlite3"))
DEFAULT_SEED_CACHE = Path(__file__).with_name("pricing_seed.json")


def _user_cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".cache"
    return base / "mnemos"


def _fallback_cache_path(path: Path) -> Path:
    return _user_cache_dir() / path.name


def _default_json_cache_path(path: Path = DEFAULT_JSON_CACHE) -> Path:
    primary = Path(path).expanduser()
    if primary == DEFAULT_JSON_CACHE.expanduser():
        return Path(os.environ.get("PANTHEON_CATALOG_CACHE", str(DEFAULT_JSON_CACHE))).expanduser()
    return primary


def _default_sqlite_cache_path(path: Path = DEFAULT_SQLITE_CACHE) -> Path:
    primary = Path(path).expanduser()
    if primary == DEFAULT_SQLITE_CACHE.expanduser():
        return Path(os.environ.get("PANTHEON_CATALOG_SQLITE", str(DEFAULT_SQLITE_CACHE))).expanduser()
    return primary


def _cache_candidate_paths(path: Path) -> list[Path]:
    primary = Path(path).expanduser()
    fallback = _fallback_cache_path(primary)
    paths = [primary]
    if fallback != primary:
        paths.append(fallback)
    return paths

# External feeds use their own naming. Normalize the provider half into the
# names PANTHEON/GRAEAE already understands. "NGC-EIH" is represented by the
# local nvidia provider key in existing code paths.
PROVIDER_ALIASES: dict[str, str] = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "google": "gemini",
    "google-ai-studio": "gemini",
    "google-vertex": "gemini",
    "vertex-ai": "gemini",
    "vertex-ai-llm": "gemini",
    "gemini": "gemini",
    "openai": "openai",
    "deepseek": "deepseek-direct",
    "deepseek-direct": "deepseek-direct",
    "deepseek-direct-v3": "deepseek-direct",
    "groq": "groq",
    "x-ai": "xai",
    "xai": "xai",
    "grok": "xai",
    "together": "together",
    "togetherai": "together",
    "together-ai": "together",
    "nvidia": "nvidia",
    "ngc": "nvidia",
    "ngc-eih": "nvidia",
    "openrouter": "openrouter",
}

MODEL_PREFIX_ALIASES: dict[str, str] = {
    "anthropic": "anthropic",
    "google": "gemini",
    "google-ai-studio": "gemini",
    "openai": "openai",
    "deepseek": "deepseek-direct",
    "groq": "groq",
    "x-ai": "xai",
    "xai": "xai",
    "together": "together",
    "nvidia": "nvidia",
    "ngc-eih": "nvidia",
}

SOURCE_PRIORITY: dict[str, int] = {
    "tokencost": 60,
    "litellm": 55,
    "models.dev": 35,
    "openrouter": 30,
    "artificialanalysis": 20,
    "benchlm": 20,
}


@dataclass(frozen=True)
class PricingRecord:
    provider: str
    model: str
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: tuple[str, ...] = ()
    source: str = "unknown"
    fetched_at: str | None = None
    raw_model_id: str | None = None


class PricingFetcher(Protocol):
    name: str

    async def fetch(self, client: httpx.AsyncClient) -> list[PricingRecord]:
        ...


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def canonical_provider(provider: Any) -> str:
    raw = _norm_key(provider)
    return PROVIDER_ALIASES.get(raw, raw)


def split_provider_model(provider: Any, model: Any) -> tuple[str, str]:
    provider_text = _norm_key(provider)
    model_text = str(model or "").strip()
    if "/" in model_text:
        prefix, rest = model_text.split("/", 1)
        mapped = MODEL_PREFIX_ALIASES.get(_norm_key(prefix))
        if mapped:
            return mapped, rest.strip()
    return canonical_provider(provider_text), model_text


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _token_price_to_mtok(value: Any) -> float | None:
    parsed = _float_or_none(value)
    if parsed is None:
        return None
    # OpenRouter and LiteLLM expose USD/token. If a future feed is already in
    # USD/MTok, do not scale obviously per-million values again.
    return parsed * 1_000_000.0 if abs(parsed) < 0.01 else parsed


def _capabilities_from_payload(model_id: str, payload: dict[str, Any]) -> tuple[str, ...]:
    caps: set[str] = {"chat"}
    supported = payload.get("supported_parameters") or payload.get("supported_endpoint_types") or []
    if isinstance(supported, (list, tuple, set)):
        support_text = " ".join(str(item).lower() for item in supported)
        if "tool" in support_text or "function" in support_text:
            caps.add("tools")
        if "response_format" in support_text or "json" in support_text:
            caps.add("json")
    architecture = payload.get("architecture") if isinstance(payload.get("architecture"), dict) else {}
    modalities = architecture.get("input_modalities") or payload.get("modalities") or payload.get("input_modalities")
    if isinstance(modalities, (list, tuple, set)):
        modality_text = " ".join(str(item).lower() for item in modalities)
        if "image" in modality_text or "vision" in modality_text:
            caps.add("vision")
        if "audio" in modality_text:
            caps.add("audio")
    lower_id = model_id.lower()
    if any(token in lower_id for token in ("embed", "embedding")):
        caps = {"embeddings"}
    if any(token in lower_id for token in ("vision", "vl", "4o", "gemini", "claude", "grok")):
        caps.add("vision")
    if any(token in lower_id for token in ("reason", "thinking", "r1", "qwq", "o3", "o4")):
        caps.add("reasoning")
    if any(token in lower_id for token in ("code", "coder", "codestral")):
        caps.add("code")
    return tuple(sorted(caps))


class OpenRouterPricingFetcher:
    name = "openrouter"
    url = "https://openrouter.ai/api/v1/models"

    async def fetch(self, client: httpx.AsyncClient) -> list[PricingRecord]:
        resp = await client.get(self.url)
        resp.raise_for_status()
        return parse_openrouter_models(resp.json(), fetched_at=utc_now_iso())


class LiteLLMPricingFetcher:
    name = "litellm"
    url = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

    async def fetch(self, client: httpx.AsyncClient) -> list[PricingRecord]:
        resp = await client.get(self.url)
        resp.raise_for_status()
        return parse_litellm_prices(resp.json(), fetched_at=utc_now_iso())


class ModelsDevPricingFetcher:
    name = "models.dev"
    url = "https://models.dev/api.json"

    async def fetch(self, client: httpx.AsyncClient) -> list[PricingRecord]:
        resp = await client.get(self.url)
        resp.raise_for_status()
        return parse_models_dev(resp.json(), fetched_at=utc_now_iso())


def parse_openrouter_models(payload: dict[str, Any], *, fetched_at: str) -> list[PricingRecord]:
    records: list[PricingRecord] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return records
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or "").strip()
        if not raw_id:
            continue
        provider, model = split_provider_model("openrouter", raw_id)
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        top_provider = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
        records.append(
            PricingRecord(
                provider=provider,
                model=model,
                input_cost_per_mtok=_token_price_to_mtok(pricing.get("prompt")),
                output_cost_per_mtok=_token_price_to_mtok(pricing.get("completion")),
                context_window=_int_or_none(item.get("context_length") or top_provider.get("context_length")),
                max_output_tokens=_int_or_none(top_provider.get("max_completion_tokens")),
                capabilities=_capabilities_from_payload(raw_id, item),
                source="openrouter",
                fetched_at=fetched_at,
                raw_model_id=raw_id,
            )
        )
    return records


def parse_litellm_prices(payload: dict[str, Any], *, fetched_at: str) -> list[PricingRecord]:
    records: list[PricingRecord] = []
    if not isinstance(payload, dict):
        return records
    for model_id, item in payload.items():
        if not isinstance(item, dict):
            continue
        litellm_provider = item.get("litellm_provider") or item.get("provider") or ""
        provider, model = split_provider_model(litellm_provider, model_id)
        if not provider or provider in {"sample-spec", "default"}:
            continue
        records.append(
            PricingRecord(
                provider=provider,
                model=model,
                input_cost_per_mtok=_token_price_to_mtok(item.get("input_cost_per_token")),
                output_cost_per_mtok=_token_price_to_mtok(item.get("output_cost_per_token")),
                context_window=_int_or_none(item.get("max_input_tokens") or item.get("max_tokens")),
                max_output_tokens=_int_or_none(item.get("max_output_tokens")),
                capabilities=_capabilities_from_payload(str(model_id), item),
                source="litellm",
                fetched_at=fetched_at,
                raw_model_id=str(model_id),
            )
        )
    return records


def _models_dev_cost(cost: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in cost:
            return _float_or_none(cost.get(key))
    return None


def parse_models_dev(payload: dict[str, Any], *, fetched_at: str) -> list[PricingRecord]:
    records: list[PricingRecord] = []
    if not isinstance(payload, dict):
        return records
    for provider_key, provider_payload in payload.items():
        if not isinstance(provider_payload, dict):
            continue
        canonical = canonical_provider(provider_key)
        models = provider_payload.get("models") or provider_payload.get("model") or {}
        iterable: Iterable[tuple[str, Any]]
        if isinstance(models, dict):
            iterable = models.items()
        elif isinstance(models, list):
            iterable = ((str(item.get("id") or item.get("name") or ""), item) for item in models if isinstance(item, dict))
        else:
            continue
        for model_id, item in iterable:
            if not isinstance(item, dict) or not model_id:
                continue
            provider, model = split_provider_model(canonical, model_id)
            cost = item.get("cost") if isinstance(item.get("cost"), dict) else {}
            limit = item.get("limit") if isinstance(item.get("limit"), dict) else {}
            records.append(
                PricingRecord(
                    provider=provider,
                    model=model,
                    input_cost_per_mtok=_models_dev_cost(cost, "input", "prompt"),
                    output_cost_per_mtok=_models_dev_cost(cost, "output", "completion"),
                    context_window=_int_or_none(limit.get("context") or item.get("context") or item.get("context_window")),
                    max_output_tokens=_int_or_none(limit.get("output") or item.get("max_output_tokens")),
                    capabilities=_capabilities_from_payload(str(model_id), item),
                    source="models.dev",
                    fetched_at=fetched_at,
                    raw_model_id=str(model_id),
                )
            )
    return records


async def fetch_pricing_records(
    fetchers: list[PricingFetcher] | None = None,
    *,
    timeout: float = 20.0,
) -> tuple[list[PricingRecord], list[dict[str, Any]]]:
    """Fetch all configured feeds, tolerating per-source failures."""
    records: list[PricingRecord] = []
    source_status: list[dict[str, Any]] = []
    fetchers = fetchers or default_pricing_fetchers()
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for fetcher in fetchers:
            started = time.monotonic()
            try:
                fetched = await fetcher.fetch(client)
                records.extend(fetched)
                source_status.append(
                    {
                        "source": fetcher.name,
                        "ok": True,
                        "records": len(fetched),
                        "fetched_at": utc_now_iso(),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - failure isolation is intentional.
                logger.warning("[PANTHEON] pricing fetcher %s failed: %s", fetcher.name, exc)
                source_status.append(
                    {
                        "source": fetcher.name,
                        "ok": False,
                        "records": 0,
                        "error": str(exc),
                        "fetched_at": utc_now_iso(),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                )
    return records, source_status


def default_pricing_fetchers() -> list[PricingFetcher]:
    """Return pricing feeds in authority order.

    LiteLLM's model_prices_and_context_window.json is the primary bundled
    machine-readable source. models.dev and OpenRouter stay secondary, while
    quality-only feeds are represented in SOURCE_PRIORITY but do not currently
    provide pricing records here.
    """
    return [LiteLLMPricingFetcher(), ModelsDevPricingFetcher(), OpenRouterPricingFetcher()]


def _record_score(record: PricingRecord) -> tuple[int, int]:
    completeness = sum(
        1
        for value in (record.input_cost_per_mtok, record.output_cost_per_mtok, record.context_window, record.max_output_tokens)
        if value is not None
    )
    return SOURCE_PRIORITY.get(record.source, 10), completeness


def pricing_index(records: Iterable[PricingRecord]) -> dict[tuple[str, str], PricingRecord]:
    index: dict[tuple[str, str], PricingRecord] = {}
    for record in records:
        provider = canonical_provider(record.provider)
        model = str(record.model or "").strip()
        if not provider or not model:
            continue
        key = (provider, model.lower())
        current = index.get(key)
        normalized = PricingRecord(**{**asdict(record), "provider": provider, "model": model})
        if current is None or _record_score(normalized) >= _record_score(current):
            index[key] = normalized
    return index


def _merge_caps(existing: Any, extra: Iterable[str]) -> list[str]:
    caps = {str(cap).strip() for cap in (existing or []) if str(cap).strip()} if isinstance(existing, (list, tuple, set)) else set()
    caps.update(str(cap).strip() for cap in extra if str(cap).strip())
    return sorted(caps)


def merge_pricing_into_catalog(base_models: list[dict[str, Any]], records: Iterable[PricingRecord]) -> list[dict[str, Any]]:
    """Overlay external prices/context/capabilities onto base PANTHEON rows."""
    idx = pricing_index(records)
    merged: list[dict[str, Any]] = []
    for model in base_models:
        item = dict(model)
        provider = canonical_provider(item.get("registry_provider") or item.get("provider"))
        model_id = str(item.get("id") or item.get("model_id") or "")
        record = idx.get((provider, model_id.lower()))
        if record is None and "/" in model_id:
            alt_provider, alt_model = split_provider_model(provider, model_id)
            record = idx.get((alt_provider, alt_model.lower()))
        if record is not None:
            if record.input_cost_per_mtok is not None:
                item["input_cost_per_mtok"] = record.input_cost_per_mtok
                item["price_in"] = record.input_cost_per_mtok
            if record.output_cost_per_mtok is not None:
                item["output_cost_per_mtok"] = record.output_cost_per_mtok
                item["price_out"] = record.output_cost_per_mtok
            in_cost = item.get("input_cost_per_mtok")
            out_cost = item.get("output_cost_per_mtok")
            if in_cost is not None and out_cost is not None:
                item["cost_per_mtok"] = (safe_float(in_cost) + safe_float(out_cost)) / 2.0
            if record.context_window is not None:
                item["context_window"] = record.context_window
                item["model_max_ctx"] = record.context_window
            if record.max_output_tokens is not None:
                item["max_output_tokens"] = record.max_output_tokens
            item["capabilities"] = _merge_caps(item.get("capabilities"), record.capabilities)
            item["pricing_source"] = record.source
            item["pricing_fetched_at"] = record.fetched_at
            item["pricing_raw_model_id"] = record.raw_model_id
        merged.append(item)
    return merged


def cache_payload(models: list[dict[str, Any]], source_status: list[dict[str, Any]], *, generated_at: str | None = None) -> dict[str, Any]:
    return {"schema": "mnemos.pantheon.catalog.v1", "generated_at": generated_at or utc_now_iso(), "sources": source_status, "models": models}


def read_json_cache(path: Path = DEFAULT_JSON_CACHE) -> dict[str, Any] | None:
    for candidate in _cache_candidate_paths(_default_json_cache_path(path)):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PANTHEON] could not read pricing cache %s: %s", candidate, exc)
    return None


def read_seed_cache(path: Path = DEFAULT_SEED_CACHE) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PANTHEON] could not read pricing seed cache %s: %s", path, exc)
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def write_json_cache(payload: dict[str, Any], path: Path = DEFAULT_JSON_CACHE) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    errors: list[str] = []
    primary = _default_json_cache_path(path)
    for candidate in _cache_candidate_paths(primary):
        try:
            _atomic_write_text(candidate, text)
            if candidate != primary:
                logger.warning(
                    "[PANTHEON] pricing cache %s unwritable; wrote fallback %s",
                    primary,
                    candidate,
                )
            return
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    logger.warning(
        "[PANTHEON] could not write pricing cache; refresh will continue without cache update (%s)",
        "; ".join(errors),
    )


def _write_sqlite_cache_at(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pantheon_catalog_cache ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), generated_at TEXT NOT NULL, payload_json TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pantheon_catalog_sources ("
            "source TEXT PRIMARY KEY, ok INTEGER NOT NULL, fetched_at TEXT, records INTEGER NOT NULL DEFAULT 0, error TEXT)"
        )
        conn.execute("DELETE FROM pantheon_catalog_sources")
        for source in payload.get("sources") or []:
            if isinstance(source, dict):
                conn.execute(
                    "INSERT OR REPLACE INTO pantheon_catalog_sources(source, ok, fetched_at, records, error) VALUES (?, ?, ?, ?, ?)",
                    (
                        str(source.get("source") or "unknown"),
                        1 if source.get("ok") else 0,
                        source.get("fetched_at"),
                        int(source.get("records") or 0),
                        source.get("error"),
                    ),
                )
        conn.execute(
            "INSERT OR REPLACE INTO pantheon_catalog_cache(id, generated_at, payload_json) VALUES (1, ?, ?)",
            (payload.get("generated_at") or utc_now_iso(), json.dumps(payload, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()


def write_sqlite_cache(payload: dict[str, Any], path: Path = DEFAULT_SQLITE_CACHE) -> None:
    errors: list[str] = []
    primary = _default_sqlite_cache_path(path)
    for candidate in _cache_candidate_paths(primary):
        try:
            _write_sqlite_cache_at(payload, candidate)
            if candidate != primary:
                logger.warning(
                    "[PANTHEON] pricing SQLite cache %s unwritable; wrote fallback %s",
                    primary,
                    candidate,
                )
            return
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {exc}")
    logger.warning(
        "[PANTHEON] could not write pricing SQLite cache; refresh will continue without cache update (%s)",
        "; ".join(errors),
    )


async def regenerate_catalog_cache(
    *,
    json_path: Path = DEFAULT_JSON_CACHE,
    sqlite_path: Path | None = DEFAULT_SQLITE_CACHE,
    seed_path: Path = DEFAULT_SEED_CACHE,
    dry_run: bool = False,
    fetchers: list[PricingFetcher] | None = None,
) -> dict[str, Any]:
    """Refresh external prices and write a last-good catalog cache.

    If every feed fails or yields zero records, the previous cache is returned
    and left untouched. This gives the timer safe staleness semantics: last-good
    survives transient external outages.
    """
    from mnemos.domain.pantheon.catalog import list_models

    base_models = await list_models(use_synced_cache=False)
    records, source_status = await fetch_pricing_records(fetchers=fetchers)
    success_count = sum(1 for status in source_status if status.get("ok"))
    if not records:
        failure_message = "no external pricing records fetched"
        last_good = read_json_cache(json_path)
        if last_good is not None:
            last_good.setdefault("stale", True)
            last_good["last_refresh_error"] = failure_message
            last_good["last_refresh_sources"] = source_status
            return last_good
        seed = read_seed_cache(seed_path)
        if seed is not None:
            payload = dict(seed)
            payload.setdefault("schema", "mnemos.pantheon.catalog.v1")
            payload.setdefault("generated_at", utc_now_iso())
            payload.setdefault(
                "sources",
                [{"source": "bundled-seed", "ok": True, "records": len(payload.get("models") or [])}],
            )
            payload["stale"] = True
            payload["seed_fallback"] = True
            payload["refresh_ok"] = False
            payload["last_refresh_error"] = failure_message
            payload["last_refresh_sources"] = source_status
            if not dry_run:
                write_json_cache(payload, json_path)
                if sqlite_path is not None:
                    write_sqlite_cache(payload, sqlite_path)
            return payload
        raise RuntimeError("no external pricing records fetched and no last-good or seed PANTHEON catalog cache exists")

    merged = merge_pricing_into_catalog(base_models, records)
    payload = cache_payload(merged, source_status)
    payload["refresh_ok"] = success_count > 0
    payload["external_pricing_records"] = len(records)
    if not dry_run:
        write_json_cache(payload, json_path)
        if sqlite_path is not None:
            write_sqlite_cache(payload, sqlite_path)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh PANTHEON external model-cost catalog cache")
    parser.add_argument("--json-cache", default=str(DEFAULT_JSON_CACHE), help="last-good JSON cache path")
    parser.add_argument("--sqlite-cache", default=str(DEFAULT_SQLITE_CACHE), help="SQLite cache path (empty to disable)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and merge without writing cache files")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sqlite_path = Path(args.sqlite_cache) if args.sqlite_cache else None
    try:
        payload = await regenerate_catalog_cache(json_path=Path(args.json_cache), sqlite_path=sqlite_path, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("[PANTHEON] pricing catalog refresh failed: %s", exc, exc_info=True)
        return 1
    ok_sources = sum(1 for source in payload.get("sources") or [] if isinstance(source, dict) and source.get("ok"))
    logger.info(
        "[PANTHEON] pricing catalog refresh complete: models=%s records=%s ok_sources=%s stale=%s",
        len(payload.get("models") or []),
        payload.get("external_pricing_records", 0),
        ok_sources,
        bool(payload.get("stale")),
    )
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
