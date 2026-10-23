"""OpenAI-compatible gateway repository — ABC-routed (F-06 slice 1).

Historically this module hand-rolled raw ``asyncpg`` queries against
``_lc._pool``: memory context for prompt injection plus four
``model_registry`` lookups. That bypassed the persistence ABC, so it only
worked on a raw asyncpg pool (in practice: Postgres) and silently returned
nothing on every other backend.

It now routes each call through the backend facade:

* ``fetch_memory_context``      -> ``MemoryRepository.fts_search``
  (all six backends; ``VisibilityFilter`` carries the tenancy).
* ``fetch_model_recommendation`` -> ``ConsultationAuditRepository.fetch_recommended_model``
  (all six backends, shared ``choose_recommended_model`` policy).
* ``lookup_provider_for_model``  -> ``ConsultationAuditRepository.lookup_provider_for_model``
  (all six backends).
* ``fetch_available_models``     -> ``ConsultationAuditRepository.fetch_available_models``
  (all six backends).
* ``fetch_model_provider``       -> ``ConsultationAuditRepository.fetch_model_provider``
  (all six backends; Postgres raises ``ModelRegistryUnavailable`` on a
  failed query, preserving the pre-ABC gateway 503 behaviour).

The module no longer references ``_lc._pool``; the single bridge to storage
is one ``transactional()`` per call. Public function signatures are
unchanged so ``mnemos.domain.openai_compat`` needs no changes.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ModelRegistryUnavailable",
    "format_memory_for_system_prompt",
    "fetch_memory_context",
    "fetch_model_recommendation",
    "lookup_provider_for_model",
    "fetch_available_models",
    "fetch_model_provider",
]


from mnemos.persistence.base import ModelRegistryUnavailable  # noqa: F401 — re-export


def format_memory_for_system_prompt(content: Any) -> str:
    """Return one memory as redacted, framed untrusted data.

    This path consumes raw database rows, so apparent MNEMOS frame delimiters
    in content are attacker-controlled data. Never trust is_framed() here:
    always redact first, then defend() so embedded delimiters/instructions are
    escaped and the entire value is wrapped as untrusted DATA.
    """
    from mnemos.core.injection_defense import defend
    from mnemos.core.secret_detection import redact_content

    return defend(redact_content("" if content is None else str(content)))


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        if hasattr(row, "get"):
            return row.get(key, default)
        return default


def _backend():
    import mnemos.core.lifecycle as _lc

    # Read the module global directly (not via get_persistence_backend, which
    # 503s) so a backend-less caller degrades to the null-with-reason contract
    # instead of an exception.
    return _lc._persistence_backend


async def _run_on_persistence(leaf, empty_result):
    """Run ``leaf(tx)`` inside one backend ``transactional()`` scope.

    The only storage bridge this module keeps: a single
    ``transactional()`` entry point instead of a raw asyncpg ``_pool.acquire``.
    With no persistence backend there is nothing to query: report the
    ``empty_result`` with a reason rather than a fabricated value.
    """
    backend = _backend()
    if backend is None:
        logger.debug("[MNEMOS] No persistence backend available")
        return empty_result
    async with backend.transactional() as tx:
        return await leaf(tx)


async def fetch_memory_context(query: str, user: Any, limit: int = 5) -> List[Dict[str, Any]]:
    async def _leaf(tx: Any) -> List[Dict[str, Any]]:
        from mnemos.core.security import is_root
        from mnemos.persistence.visibility import VisibilityFilter

        # The v4.1 read path: full memory rows through the ABC, so every
        # backend (not just a raw asyncpg pool) serves this call. Root gets
        # the cross-tenant read bypass (the vault subtraction still applies);
        # non-root is pinned to its namespace with the READABLE envelope —
        # the same predicate the other memory read surfaces use.
        if is_root(user):
            visibility = VisibilityFilter.for_read(user, namespace=None)
        else:
            visibility = VisibilityFilter.for_read(user, namespace=getattr(user, "namespace", None) or "default")
        try:
            # ``tx`` is the backend-scoped transaction handed in by
            # ``_run_on_persistence``; the repository methods take it as
            # their first argument and derive the connection from it.
            rows = await _backend().memories.fts_search(tx, query=query, limit=limit, visibility=visibility)
        except Exception as e:
            logger.warning("[MNEMOS] Search failed for '%s...': %s", query[:50], e)
            return []
        logger.info("[MNEMOS] Found %s memories for query '%s...'", len(rows), query[:30])
        return [
            {
                "id": _row_get(row, "id"),
                "content": format_memory_for_system_prompt(_row_get(row, "content")),
                "namespace": _row_get(row, "namespace"),
            }
            for row in rows
            if _row_get(row, "namespace") != "vault"
        ]

    return await _run_on_persistence(_leaf, [])


async def fetch_model_recommendation(
    task_type: str,
    cost_budget: float = 10.0,
    quality_floor: float = 0.85,
) -> Optional[Dict[str, Any]]:
    async def _leaf(tx: Any) -> Optional[Dict[str, Any]]:
        try:
            model, _required = await _backend().consultations_audit.fetch_recommended_model(
                tx, task_type, cost_budget, quality_floor
            )
        except Exception as e:
            logger.warning("[OPTIMIZER] Recommendation failed: %s, using default", e)
            return None
        if model is None:
            logger.warning("[OPTIMIZER] No models available, using default gpt-4o")
            return None
        logger.info(
            "[OPTIMIZER] Recommended %s/%s for %s (cost=%s)",
            model.get("provider"),
            model.get("model_id"),
            task_type,
            model.get("cost_per_mtok"),
        )
        return model

    return await _run_on_persistence(_leaf, None)


async def lookup_provider_for_model(model: str) -> Optional[str]:
    async def _leaf(tx: Any) -> Optional[str]:
        try:
            return await _backend().consultations_audit.lookup_provider_for_model(tx, model)
        except Exception as exc:
            logger.warning("[MNEMOS] model_registry lookup failed for model=%s: %s", model, exc)
            return None

    return await _run_on_persistence(_leaf, None)


async def fetch_available_models() -> list[Any]:
    async def _leaf(tx: Any) -> list[Any]:
        try:
            rows = await _backend().consultations_audit.fetch_available_models(tx)
        except Exception as exc:
            logger.warning(
                "[/v1/models] model_registry query failed, " "returning an empty discovery list: %s",
                exc,
            )
            rows = []
        return list(rows)

    return await _run_on_persistence(_leaf, [])


async def fetch_model_provider(model_id: str) -> Optional[str]:
    async def _leaf(tx: Any) -> Optional[str]:
        return await _backend().consultations_audit.fetch_model_provider(tx, model_id)

    return await _run_on_persistence(_leaf, None)
