"""v6.2 M-2.2.4 per-category exponential temporal decay.

Applies a per-category multiplier to search scores based on the
memory's age and the category's half-life. Decay parameters live in
the ``memory_category_decay`` table (migration 0031) — 9 default
rows seeded by the schema migration; admin endpoints follow.

Why post-recall instead of in SQL:
1. Table is tiny (<= 20 rows). Process-local TTL cache, 60s refresh.
2. Filtering by score in SQL would prune real hits before rerank.
3. exp() in SQL is fine on PG/Oracle/Db2 but broken on SQLite.
   Keeps the SQL identical across backends.

Public surface::

    from mnemos.domain.search.decay import (
        DecayParams,
        apply_decay,
        load_decay_table,
        invalidate_decay_cache,
    )

Caller pattern (route handler)::

    table = await load_decay_table(backend)
    memories = apply_decay(memories, table, now=datetime.now(tz=UTC))
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)


DEFAULT_CATEGORY = "(default)"
_TTL_SECONDS = 60
_SIGMOID_K_FACTOR = 0.3  # k chosen to give 95% drop within ±half_life * 0.3


@dataclass(frozen=True)
class DecayParams:
    """Per-category decay parameters loaded from memory_category_decay."""

    category: str
    half_life_days: float
    decay_kind: str  # 'exponential' | 'sigmoid' | 'none'
    floor: float

    def multiplier(self, age_days: float) -> float:
        """Compute decay multiplier for ``age_days``.

        Floor-clamped: never falls below ``floor``. ``decay_kind=none``
        returns 1.0 regardless of age (no decay).
        """
        if self.decay_kind == "none":
            return 1.0
        if age_days <= 0:
            return 1.0
        if self.decay_kind == "sigmoid":
            # Old keys are "actively wrong" -- collapse sharply around
            # half_life. k chosen so the curve drops ~95% within
            # +/- half_life * _SIGMOID_K_FACTOR of the threshold.
            k = math.log(19) / (self.half_life_days * _SIGMOID_K_FACTOR)
            raw = 1.0 / (1.0 + math.exp(k * (age_days - self.half_life_days)))
            return max(self.floor, raw)
        # Default: exponential.
        raw = math.exp(-math.log(2) * age_days / self.half_life_days)
        return max(self.floor, raw)


class _DecayCache:
    """Process-local TTL cache for the memory_category_decay table."""

    def __init__(self) -> None:
        self._table: dict[str, DecayParams] | None = None
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._table = None
        self._loaded_at = 0.0

    def get(self) -> dict[str, DecayParams] | None:
        if self._table is None:
            return None
        if time.monotonic() - self._loaded_at >= _TTL_SECONDS:
            return None
        return self._table

    def set(self, table: dict[str, DecayParams]) -> None:
        self._table = table
        self._loaded_at = time.monotonic()


_cache = _DecayCache()


def invalidate_decay_cache() -> None:
    """Public helper for admin endpoints to drop cached parameters."""
    _cache.invalidate()


async def load_decay_table(backend: Any) -> dict[str, DecayParams]:
    """Load + cache the memory_category_decay table.

    Returns a dict keyed by ``category`` name. Cache TTL =
    ``_TTL_SECONDS`` (60s); subsequent calls within window return
    the cached snapshot. On any error returns an empty dict so the
    caller falls through to "no decay applied" rather than 500-ing
    the search path.
    """
    cached = _cache.get()
    if cached is not None:
        return cached

    async with _cache._lock:  # noqa: SLF001 - intentional intra-module access
        cached = _cache.get()
        if cached is not None:
            return cached
        try:
            async with backend.transactional() as tx:
                rows = await _fetch_decay_rows(backend, tx)
        except Exception:
            logger.exception("[decay] load_decay_table failed; returning empty (no-decay) table")
            return {}

        table: dict[str, DecayParams] = {}
        for r in rows:
            try:
                cat = r["category"]
                table[cat] = DecayParams(
                    category=cat,
                    half_life_days=float(r["half_life_days"]),
                    decay_kind=str(r["decay_kind"]),
                    floor=float(r["floor"]),
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("[decay] skipping malformed row: %r", r)
        _cache.set(table)
        logger.info("[decay] loaded %d categories from memory_category_decay", len(table))
        return table


async def _fetch_decay_rows(backend: Any, tx: Any) -> list[Mapping[str, Any]]:
    """Backend-agnostic SELECT * FROM memory_category_decay."""
    # All four backends accept the same simple SELECT — we route via
    # the existing memories repo path is overkill; use the connection
    # accessor each backend exposes.
    conn_attr = getattr(tx, "conn", None)
    sql = "SELECT category, half_life_days, decay_kind, floor FROM memory_category_decay"
    # Postgres asyncpg
    if conn_attr is not None and hasattr(conn_attr, "fetch") and hasattr(conn_attr, "fetchrow"):
        rows = await conn_attr.fetch(sql)
        return [dict(r) for r in rows]
    # SQLite (mnemos wrapper)
    if conn_attr is not None and "sqlite" in type(conn_attr).__module__:
        from mnemos.persistence.sqlite import _fetch_all

        return await _fetch_all(conn_attr, sql, ())
    # Oracle / Db2 (python-oracledb async)
    if conn_attr is not None and hasattr(conn_attr, "cursor"):
        from mnemos.persistence.oracle import _call as _ora_call

        cursor = await _ora_call(conn_attr.cursor)
        try:
            await _ora_call(cursor.execute, sql)
            rows = await _ora_call(cursor.fetchall)
            if not rows:
                return []
            cols = [d[0].lower() for d in cursor.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            await _ora_call(cursor.close)
    raise RuntimeError(f"[decay] unsupported tx shape {type(tx)!r}")


def apply_decay(
    memories: list[Any],
    table: dict[str, DecayParams],
    *,
    now: datetime | None = None,
    overrides: Mapping[str, float] | None = None,
    recency_weight: float = 1.0,
) -> list[Any]:
    """Apply per-category decay to a list of memory items.

    Sorts in-place by descending (base_score * decay_multiplier).
    ``base_score`` is the existing ``quality_rating`` or the recency
    weight from the search route — we read either field defensively.

    ``overrides`` is the per-request ``decay_overrides`` map. Use
    ``"*"`` key to flatten all categories (set multiplier to 1.0).
    Otherwise per-category override replaces the table's half_life.

    ``recency_weight`` rescales all half-lives — passing 0.5 makes
    the chain decay TWICE as fast. Matches the v6.0 search semantics.
    """
    if not memories:
        return memories
    if not table and not overrides:
        return memories

    now_ts = now or datetime.now(tz=timezone.utc)
    decorated: list[tuple[float, Any]] = []
    universal_override = overrides.get("*") if overrides else None

    for m in memories:
        # Extract category + age. The route passes MemoryItem objects.
        cat = _safe_get(m, "category") or DEFAULT_CATEGORY
        created = _safe_get(m, "created") or _safe_get(m, "updated")
        age_days = _age_days(created, now_ts) if created else 0.0
        base = _base_score(m)

        # Override path
        if universal_override is not None:
            mult = float(universal_override)
        elif overrides and cat in overrides:
            mult = float(overrides[cat])
        else:
            params = table.get(cat) or table.get(DEFAULT_CATEGORY)
            if params is None:
                mult = 1.0
            else:
                # Rescale half_life by recency_weight (1.0 = unchanged)
                scaled_hl = max(0.001, params.half_life_days / max(recency_weight, 0.01))
                scaled = DecayParams(
                    category=params.category,
                    half_life_days=scaled_hl,
                    decay_kind=params.decay_kind,
                    floor=params.floor,
                )
                mult = scaled.multiplier(age_days)

        final = base * mult
        # Attach for debugging + telemetry; ignore failure if model is frozen.
        try:
            setattr(m, "decay_multiplier", round(mult, 4))
            setattr(m, "decay_final_score", round(final, 4))
        except Exception:
            pass
        decorated.append((final, m))

    # Stable sort by final score descending; preserves original order
    # among ties.
    decorated.sort(key=lambda kv: kv[0], reverse=True)
    return [m for _, m in decorated]


def _safe_get(obj: Any, key: str) -> Any:
    """Get ``key`` from a dict OR getattr from an object; return None on miss."""
    try:
        if isinstance(obj, Mapping):
            return obj.get(key)
        return getattr(obj, key, None)
    except Exception:
        return None


def _base_score(memory: Any) -> float:
    """Best-effort base score extraction.

    Priority: explicit ``score`` attr (when set by semantic_search) ->
    ``quality_rating`` -> 50.0 default. Always positive so multiplier
    has something to scale against.
    """
    for key in ("score", "quality_rating"):
        v = _safe_get(memory, key)
        if v is not None:
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                continue
    return 50.0


def _age_days(created_value: Any, now: datetime) -> float:
    """Coerce ``created_value`` (datetime or ISO string) to age in days."""
    if created_value is None:
        return 0.0
    if isinstance(created_value, datetime):
        dt = created_value
    elif isinstance(created_value, str):
        try:
            # Accept ISO 8601 with optional trailing Z
            s = created_value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        except Exception:
            return 0.0
    else:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (now - dt).total_seconds() / 86400.0
    return max(0.0, age)


def _decay_params_from_overrides(
    overrides: Mapping[str, float] | None,
    default_kind: str = "exponential",
    default_floor: float = 0.0,
) -> dict[str, DecayParams]:
    """Helper for tests: build a DecayParams dict from a {category: half_life}
    map. ``"*"`` key is honored at apply-time, not here."""
    if not overrides:
        return {}
    return {
        cat: DecayParams(
            category=cat,
            half_life_days=float(hl),
            decay_kind=default_kind,
            floor=default_floor,
        )
        for cat, hl in overrides.items()
        if cat != "*"
    }
