"""Shared contract suite for ``ModelRegistryRepository`` (provider/pricing DB 1.5).

Parametrised over every backend that owns a registry repository:

* ``sqlite`` — always runs (tmp-file database, the CI reference implementation);
* ``postgres`` — runs only when ``MNEMOS_TEST_POSTGRES_DSN`` is set, otherwise
  skips with a reason that names the variable;
* ``oracle`` / ``db2`` / ``mysql`` / ``mariadb`` — do not implement the ABC
  yet, so they are skipped *by name* with ``unsupported backend: <name> —
  phase 2``. Nothing silently passes: an unsupported backend fails the run if
  its reason string ever stops matching, and every skipped backend is asserted
  to be in :data:`MODEL_REGISTRY_PHASE2_BACKENDS`.

The cases are the contract, not the implementation:

1. ``NULL`` stays ``NULL`` — unpriced is never ``0``, and a free model is
   never reported as unpriced. A cache-only price (only ``price_cached``
   known) is priced, and counts/filters as such.
2. per-field ``source`` / ``fetched_at`` round-trip;
3. price ``band`` round-trip (``standard`` / ``peak`` / ``off_peak``);
4. alias uniqueness and lookalike rejection;
5. the curated layer is immune to refresh;
6. ``export_snapshot`` is the sorted, stable export document and
   ``snapshot_digest`` is exactly its canonical hash — identical for identical
   inputs, regardless of insertion order;
7. ``type(cls).__abstractmethods__`` is empty for every conforming backend,
   and ``MODEL_REGISTRY_OPERATIONS`` names exactly those abstract methods so
   the advertised operation set cannot drift from the ABC.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from mnemos.persistence.base import (
    MODEL_REGISTRY_OPERATIONS,
    MODEL_REGISTRY_PHASE2_BACKENDS,
    PRICE_BANDS,
    PRICING_MODELS,
    CuratedRouting,
    FieldObservation,
    ModelFilter,
    ModelRecord,
    ModelRegistryNotSupported,
    ModelRegistryRepository,
    PriceBand,
    model_registry_snapshot_digest,
    SyncLogEntry,
)

# No module-wide ``pytestmark``: the suite mixes coroutines with two sync
# structural checks, and a module-wide mark would stamp ``asyncio`` onto the
# sync tests (pytest-asyncio 1.x warns about that). Every async test is marked
# explicitly instead — the parametrised ones through ``_conforming()`` /
# ``_unsupported()`` below, the rest with ``@pytest.mark.asyncio``.

POSTGRES_DSN_ENV = "MNEMOS_TEST_POSTGRES_DSN"

CONFORMING_BACKENDS = ("sqlite", "postgres")
UNSUPPORTED_BACKENDS = MODEL_REGISTRY_PHASE2_BACKENDS

_PHASE2_TESTS = {
    "oracle": ("upsert_model", "list_models", "get_model", "set_alias", "resolve_alias"),
    "db2": ("upsert_model", "list_models", "get_model", "set_alias", "resolve_alias"),
    "mysql": ("upsert_model", "list_models", "get_model", "set_alias", "resolve_alias"),
    "mariadb": ("upsert_model", "list_models", "get_model", "set_alias", "resolve_alias"),
}


# ── fixture plumbing ──────────────────────────────────────────────────────────


@dataclass
class RegistryHandle:
    """A backend instance + the transaction factory the contract uses."""

    backend: Any
    repo: ModelRegistryRepository
    transaction: Callable[[], Any]


async def _sqlite_handle(tmp_path: Any) -> AsyncIterator[RegistryHandle]:
    from mnemos.persistence.sqlite import SqliteBackend

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=8))
    backend = SqliteBackend(tmp_path / "model-registry-contract.db", settings)
    await backend.open()
    try:
        yield RegistryHandle(backend=backend, repo=backend.model_registry, transaction=backend.transactional)
    finally:
        await backend.close()


async def _postgres_handle() -> AsyncIterator[RegistryHandle]:
    import asyncpg  # noqa: F401 — imported for the skip reason to be accurate

    from mnemos.persistence.postgres import PostgresBackend

    dsn = os.environ[POSTGRES_DSN_ENV]
    # The pool is created here, not by ``PostgresBackend.open`` (a lifecycle
    # hook, not a factory): the backend borrows it and ``close`` releases it,
    # but the pool is closed explicitly below too, so no connection leaks
    # across parametrized cases even if the backend is already closed.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    backend = PostgresBackend(pool, SimpleNamespace(database=SimpleNamespace(embedding_dim=8)))
    try:
        await backend.open()
        # The sqlite arm gets a fresh tmp file per test; the shared Postgres
        # test database does not. Truncate the registry tables so each
        # parametrized case starts from an empty registry like sqlite does.
        async with backend.transactional() as tx:
            conn = tx.conn
            # Table names are fixed literals (not interpolated): asyncpg
            # cannot parameterise identifiers, so each DELETE names its
            # table directly instead of formatting a variable into SQL.
            await conn.execute("DELETE FROM model_registry_health")
            await conn.execute("DELETE FROM curated_routing")
            await conn.execute("DELETE FROM model_benchmarks")
            await conn.execute("DELETE FROM model_prices")
            await conn.execute("DELETE FROM model_registry_field_obs")
            await conn.execute("DELETE FROM model_registry_sync_log")
            await conn.execute("DELETE FROM model_registry")
        yield RegistryHandle(backend=backend, repo=backend.model_registry, transaction=backend.transactional)
    finally:
        await backend.close()
        await pool.close()


# async fixtures must use pytest_asyncio.fixture: the repo runs pytest-asyncio
# in its default strict mode (no asyncio_mode config), where a plain
# @pytest.fixture wrapping an async generator is never awaited.
@pytest_asyncio.fixture
async def registry(request: Any, tmp_path: Any) -> AsyncIterator[RegistryHandle]:
    """Yield a live registry handle for the requested backend, or skip by name."""
    backend_name = request.param
    if backend_name == "sqlite":
        async for handle in _sqlite_handle(tmp_path):
            yield handle
        return
    if backend_name == "postgres":
        if not os.environ.get(POSTGRES_DSN_ENV):
            pytest.skip(f"postgres backend not configured — set {POSTGRES_DSN_ENV} to a live database to run this arm")
        async for handle in _postgres_handle():
            yield handle
        return
    phase2 = f"unsupported backend: {backend_name} — phase 2"
    assert backend_name in UNSUPPORTED_BACKENDS, f"{backend_name!r} has no documented non-conformance"
    pytest.skip(phase2)


def _conforming():
    """Parametrize ``registry`` over the conforming backends, as an async test.

    ``pytest_asyncio`` marks are stacked with the parametrization in order, so
    the coroutine is opted into the plugin and the sync structural tests in
    this module never inherit the ``asyncio`` mark.
    """
    return pytest.mark.parametrize(
        "registry",
        [pytest.param("sqlite", id="sqlite"), pytest.param("postgres", id="postgres")],
        indirect=True,
    )


def _unsupported():
    return pytest.mark.parametrize(
        "registry",
        [pytest.param(name, id=name) for name in UNSUPPORTED_BACKENDS],
        indirect=True,
    )


MODEL = ModelRecord(
    provider="openai",
    model_id="gpt-phase15",
    display_name="GPT Phase 1.5",
    family="gpt-5",
    context_window=128000,
    max_output_tokens=16384,
    capabilities=("chat", "vision"),
)


# ── 7. structural conformance ─────────────────────────────────────────────────


def _dummy_call(method: Any) -> tuple[list[Any], dict[str, Any]]:
    """Return placeholder arguments for ``method``, shaped from its signature.

    ``tx`` gets ``None`` (the guard raises before touching it) and every other
    positional-or-keyword parameter gets ``None`` too, so the call reaches the
    operation body instead of failing on arity. Required keyword-only
    parameters are filled by name for the same reason.
    """
    signature = inspect.signature(method)
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            args.append(None)
        elif parameter.kind is parameter.KEYWORD_ONLY:
            kwargs[parameter.name] = None
    return args, kwargs


@pytest.mark.asyncio
@_conforming()
async def test_backend_is_fully_conformant(registry: RegistryHandle) -> None:
    assert isinstance(registry.repo, ModelRegistryRepository)
    assert not type(registry.repo).__abstractmethods__, (
        f"{type(registry.repo).__name__} still has abstract methods: "
        f"{sorted(type(registry.repo).__abstractmethods__)}"
    )
    assert registry.repo.backend_name in CONFORMING_BACKENDS
    for operation in MODEL_REGISTRY_OPERATIONS:
        assert callable(getattr(registry.repo, operation)), operation


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", UNSUPPORTED_BACKENDS)
async def test_unsupported_backends_raise_named_errors(backend_name: str) -> None:
    """Non-conformance is a named, documented raise — never a silent empty result."""
    from mnemos.persistence.model_registry_unsupported import (
        Db2ModelRegistryRepository,
        MariadbModelRegistryRepository,
        MysqlModelRegistryRepository,
        OracleModelRegistryRepository,
    )

    classes = {
        "oracle": OracleModelRegistryRepository,
        "db2": Db2ModelRegistryRepository,
        "mysql": MysqlModelRegistryRepository,
        "mariadb": MariadbModelRegistryRepository,
    }
    repo = classes[backend_name]()
    assert repo.backend_name == backend_name
    # It IS structurally complete (so it can be constructed), but every single
    # operation raises the named error rather than pretending to work.
    assert not type(repo).__abstractmethods__
    for operation in MODEL_REGISTRY_OPERATIONS:
        method = getattr(repo, operation)
        # Build the argument list from the real signature instead of guessing
        # arities: every parameter other than ``tx`` gets a throwaway value.
        # The call must fail on the guard, never on TypeError — a TypeError
        # would mean the operation is not actually reachable.
        args, kwargs = _dummy_call(method)
        with pytest.raises(ModelRegistryNotSupported) as excinfo:
            await method(*args, **kwargs)
        assert str(excinfo.value) == f"unsupported backend: {backend_name} — phase 2 (operation: {operation})"


# ── 1. NULL stays NULL ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@_conforming()
async def test_unpriced_model_keeps_null_prices(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                pricing_model="unknown",
                band="standard",
                price_in=None,
                price_out=None,
                price_cached=None,
            ),
        )
        model = await registry.repo.get_model(tx, MODEL.provider, MODEL.model_id)

    assert model is not None
    assert model.price_in is None, "an unpriced model must not be reported as 0"
    assert model.price_out is None
    assert model.price_cached is None
    assert model.price_in != 0

    async with registry.transaction() as tx:
        health = await registry.repo.health_read(tx)
    assert health.priced_count == 0
    assert health.unpriced_count == health.model_count


@pytest.mark.asyncio
@_conforming()
async def test_free_model_is_zero_not_null(registry: RegistryHandle) -> None:
    """A demonstrably free model is ``0.0`` and counts as PRICED, not unknown."""
    free = ModelRecord(provider="local", model_id="llama-local", pricing_model="local")
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, free)
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider="local",
                model_id="llama-local",
                pricing_model="local",
                band="standard",
                price_in=0.0,
                price_out=0.0,
                price_cached=0.0,
            ),
        )
        model = await registry.repo.get_model(tx, "local", "llama-local")
        health = await registry.repo.health_read(tx)

    assert model is not None
    assert model.price_in == 0.0
    assert model.price_in is not None
    assert health.priced_count == 1
    assert health.unpriced_count == 0


@pytest.mark.asyncio
@_conforming()
async def test_priced_filter_distinguishes_null_from_zero(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.upsert_model(tx, ModelRecord(provider="local", model_id="free-one"))
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider="local",
                model_id="free-one",
                pricing_model="local",
                band="standard",
                price_in=0.0,
                price_out=0.0,
            ),
        )
        unpriced = await registry.repo.list_models(tx, ModelFilter(priced=False))
        priced = await registry.repo.list_models(tx, ModelFilter(priced=True))

    assert {m.model_id for m in unpriced} == {MODEL.model_id}
    assert {m.model_id for m in priced} == {"free-one"}


@pytest.mark.asyncio
@_conforming()
@pytest.mark.parametrize("field", ["price_in", "price_out", "price_cached"])
async def test_each_price_column_counts_as_priced(registry: RegistryHandle, field: str) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                pricing_model="payg",
                band="standard",
                **{field: 0.3},
            ),
        )
        model = await registry.repo.get_model(tx, MODEL.provider, MODEL.model_id)
        health = await registry.repo.health_read(tx)
        priced = await registry.repo.list_models(tx, ModelFilter(priced=True))
        unpriced = await registry.repo.list_models(tx, ModelFilter(priced=False))

    assert model is not None
    assert getattr(model, field) == 0.3
    for other in {"price_in", "price_out", "price_cached"} - {field}:
        assert getattr(model, other) is None
    assert health.model_count == 1
    assert health.priced_count == 1
    assert health.unpriced_count == 0
    assert {m.model_id for m in priced} == {MODEL.model_id}
    assert unpriced == []


# ── 2. per-field provenance ───────────────────────────────────────────────────


@pytest.mark.asyncio
@_conforming()
async def test_field_observations_round_trip_with_provenance(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.record_field_obs(
            tx,
            FieldObservation(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                field="context_window",
                value="200000",
                source="openai-catalog",
                fetched_at="2026-10-01T09:00:00+00:00",
            ),
        )
        await registry.repo.record_field_obs(
            tx,
            FieldObservation(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                field="display_name",
                value="GPT-5 (renamed)",
                source="arena-leaderboard",
                fetched_at="2026-10-02T11:30:00+00:00",
            ),
        )
        merged = await registry.repo.merge_fields(tx, MODEL.provider, MODEL.model_id)

    assert merged is not None
    assert merged.context_window == 200000
    assert merged.display_name == "GPT-5 (renamed)"


@pytest.mark.asyncio
@_conforming()
async def test_merge_fields_restricted_to_one_source(registry: RegistryHandle) -> None:
    """Refreshing one upstream must not restamp a field owned by another."""
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.record_field_obs(
            tx,
            FieldObservation(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                field="context_window",
                value="64000",
                source="upstream-a",
                fetched_at="2026-10-01T00:00:00+00:00",
            ),
        )
        await registry.repo.record_field_obs(
            tx,
            FieldObservation(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                field="display_name",
                value="From B",
                source="upstream-b",
                fetched_at="2026-10-01T00:00:00+00:00",
            ),
        )
        merged = await registry.repo.merge_fields(
            tx, MODEL.provider, MODEL.model_id, authoritative_source="upstream-a"
        )

    assert merged is not None
    assert merged.context_window == 64000
    # upstream-b's display_name was NOT applied.
    assert merged.display_name == MODEL.display_name


@pytest.mark.asyncio
@_conforming()
async def test_merge_fields_never_creates_a_model(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.record_field_obs(
            tx,
            FieldObservation(
                provider="ghost",
                model_id="nope",
                field="context_window",
                value="1",
                source="x",
                fetched_at="2026-10-01T00:00:00+00:00",
            ),
        )
        merged = await registry.repo.merge_fields(tx, "ghost", "nope")
        assert merged is None
        assert await registry.repo.get_model(tx, "ghost", "nope") is None


@pytest.mark.asyncio
@_conforming()
async def test_null_observation_clears_a_field_rather_than_zeroing_it(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.record_field_obs(
            tx,
            FieldObservation(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                field="max_output_tokens",
                value=None,
                source="catalog",
                fetched_at="2026-10-03T00:00:00+00:00",
            ),
        )
        merged = await registry.repo.merge_fields(tx, MODEL.provider, MODEL.model_id)

    assert merged is not None
    assert merged.max_output_tokens is None
    assert merged.max_output_tokens != 0


# ── 3. band round-trip ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@_conforming()
async def test_every_band_round_trips(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        for index, band in enumerate(PRICE_BANDS):
            await registry.repo.upsert_price(
                tx,
                PriceBand(
                    provider=MODEL.provider,
                    model_id=MODEL.model_id,
                    pricing_model="payg",
                    band=band,
                    price_in=1.0 + index,
                    price_out=2.0 + index,
                ),
            )
        # A band must not bleed into a sibling band or another pricing model:
        # write every NON-payg pricing model on the standard band and assert
        # none of them displaces the payg/standard headline price.
        for model in PRICING_MODELS:
            if model == "payg":
                continue
            await registry.repo.upsert_price(
                tx,
                PriceBand(
                    provider=MODEL.provider,
                    model_id=MODEL.model_id,
                    pricing_model=model,
                    band="standard",
                    price_in=None if model == "unknown" else 9.5,
                    price_out=None if model == "unknown" else 19.5,
                ),
            )

    async with registry.transaction() as tx:
        # The standard/payg band is mirrored onto the model row; the other
        # bands and pricing models live only on their own rows and must not
        # overwrite it.
        model = await registry.repo.get_model(tx, MODEL.provider, MODEL.model_id)
    assert model is not None
    assert model.band == "standard"
    assert model.pricing_model == "payg"
    assert model.price_in == 1.0


@pytest.mark.asyncio
@_conforming()
async def test_non_payg_price_never_displaces_another_pricing_models_price(registry: RegistryHandle) -> None:
    """A second non-payg row must not overwrite a price already mirrored.

    Regression: the mirror guard once read ``payg OR pricing_model <> 'payg'
    OR <all prices NULL>``. The middle disjunct is satisfied for *any*
    non-payg row, so a later ``subscription`` price silently replaced a
    ``free_tier`` 0.0 on the model row and made a free model look paid.
    """
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                pricing_model="free_tier",
                band="standard",
                price_in=0.0,
                price_out=0.0,
            ),
        )
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                pricing_model="subscription",
                band="standard",
                price_in=42.0,
                price_out=84.0,
            ),
        )
        model = await registry.repo.get_model(tx, MODEL.provider, MODEL.model_id)

    assert model is not None
    assert model.pricing_model == "free_tier"
    assert model.price_in == 0.0


@pytest.mark.asyncio
@_conforming()
async def test_non_payg_price_fills_an_unpriced_model(registry: RegistryHandle) -> None:
    """The same guard must still let the first non-payg price through."""
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                pricing_model="local",
                band="standard",
                price_in=0.0,
                price_out=0.0,
            ),
        )
        model = await registry.repo.get_model(tx, MODEL.provider, MODEL.model_id)

    assert model is not None
    assert model.pricing_model == "local"
    assert model.price_in == 0.0


@pytest.mark.asyncio
@_conforming()
async def test_payg_price_still_replaces_an_earlier_non_payg_price(registry: RegistryHandle) -> None:
    """payg is authoritative: it overwrites whatever an earlier model mirrored."""
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                pricing_model="free_tier",
                band="standard",
                price_in=0.0,
                price_out=0.0,
            ),
        )
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider=MODEL.provider,
                model_id=MODEL.model_id,
                pricing_model="payg",
                band="standard",
                price_in=3.0,
                price_out=6.0,
            ),
        )
        model = await registry.repo.get_model(tx, MODEL.provider, MODEL.model_id)

    assert model is not None
    assert model.pricing_model == "payg"
    assert model.price_in == 3.0


@pytest.mark.asyncio
@_conforming()
async def test_invalid_band_is_rejected(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        with pytest.raises(ValueError):
            await registry.repo.upsert_price(
                tx,
                PriceBand(provider=MODEL.provider, model_id=MODEL.model_id, pricing_model="payg", band="surge"),
            )
        with pytest.raises(ValueError):
            await registry.repo.upsert_price(
                tx,
                PriceBand(provider=MODEL.provider, model_id=MODEL.model_id, pricing_model="barter", band="standard"),
            )


# ── 4. aliases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@_conforming()
async def test_alias_resolves_and_is_unique(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.set_alias(tx, MODEL.provider, "gpt-latest", MODEL.model_id)
        resolved = await registry.repo.resolve_alias(tx, MODEL.provider, "gpt-latest")
        direct = await registry.repo.resolve_alias(tx, MODEL.provider, MODEL.model_id)
        listed = await registry.repo.list_models(tx, ModelFilter(provider=MODEL.provider))

    assert resolved is not None
    assert resolved.model_id == MODEL.model_id
    assert direct is not None and direct.model_id == MODEL.model_id
    # Alias rows are not models.
    assert [m.model_id for m in listed] == [MODEL.model_id]


@pytest.mark.asyncio
@_conforming()
async def test_alias_lookalikes_and_self_pointers_are_rejected(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        with pytest.raises(ValueError, match="self"):
            await registry.repo.set_alias(tx, MODEL.provider, MODEL.model_id, MODEL.model_id)
        # Case + separator twins of a real id.
        with pytest.raises(ValueError, match="lookalike"):
            await registry.repo.set_alias(tx, MODEL.provider, "GPT_PHASE--15", MODEL.model_id)
        # A real-id collision is refused outright.
        await registry.repo.upsert_model(tx, ModelRecord(provider=MODEL.provider, model_id="other-model"))
        with pytest.raises(ValueError):
            await registry.repo.set_alias(tx, MODEL.provider, "other-model", MODEL.model_id)
        # Aliases resolve one hop only.
        await registry.repo.set_alias(tx, MODEL.provider, "alias-one", MODEL.model_id)
        with pytest.raises(ValueError, match="one hop"):
            await registry.repo.set_alias(tx, MODEL.provider, "alias-two", "alias-one")


@pytest.mark.asyncio
@_conforming()
async def test_unknown_alias_resolves_to_none(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        assert await registry.repo.resolve_alias(tx, "nobody", "nothing") is None


# ── 5. curated layer is immune to refresh ────────────────────────────────────


@pytest.mark.asyncio
@_conforming()
async def test_curated_routing_is_versioned_and_refresh_safe(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        first = await registry.repo.curated_routing_replace(
            tx,
            rules=[{"task_type": "code", "provider": "openai", "model_id": MODEL.model_id}],
            note="initial",
        )
    assert isinstance(first, CuratedRouting)
    assert first.version == 1

    async with registry.transaction() as tx:
        second = await registry.repo.curated_routing_replace(
            tx,
            rules=[{"task_type": "code", "provider": "openai", "model_id": MODEL.model_id, "weight": 0.9}],
            note="adjusted",
        )
    assert second.version == 2

    # A "refresh" is exactly: read the curated layer, apply it, and re-read.
    # It must not bump or mutate the curated version.
    async with registry.transaction() as tx:
        before = await registry.repo.curated_routing_get(tx)
        await registry.repo.upsert_model(tx, ModelRecord(provider="openai", model_id="fresh-from-catalog"))
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider="openai",
                model_id="fresh-from-catalog",
                pricing_model="payg",
                band="standard",
                price_in=3.0,
                price_out=6.0,
            ),
        )
        after = await registry.repo.curated_routing_get(tx)

    assert before is not None and after is not None
    assert before.version == after.version == 2
    assert before.rules == after.rules
    assert after.note == "adjusted"


# ── 6. deterministic export ──────────────────────────────────────────────────


async def _seed_digest_fixture(repo: ModelRegistryRepository, tx: Any, order: list[int]) -> str:
    """Seed the digest fixture on the caller's transaction and return its digest.

    It takes an already-open ``tx`` on purpose: opening a second transaction
    while one is held deadlocks SQLite, and re-entrant transcations are not
    part of the repository contract.
    """
    models = [
        ModelRecord(provider="openai", model_id="m-b", context_window=1000, capabilities=("chat", "vision")),
        ModelRecord(provider="xai", model_id="m-a", context_window=2000, capabilities=("chat",)),
        ModelRecord(provider="local", model_id="m-c", context_window=None),
    ]
    bands = [
        PriceBand(provider="openai", model_id="m-b", pricing_model="payg", band="standard", price_in=1.5, price_out=3.0),
        PriceBand(provider="openai", model_id="m-b", pricing_model="payg", band="off_peak", price_in=0.75, price_out=1.5),
        PriceBand(provider="local", model_id="m-c", pricing_model="local", band="standard", price_in=0.0, price_out=0.0),
    ]
    for index in order:
        await repo.upsert_model(tx, models[index])
    for index in order:
        await repo.upsert_price(tx, bands[index])
    await repo.record_field_obs(
        tx,
        FieldObservation(
            provider="openai",
            model_id="m-b",
            field="context_window",
            value="1000",
            source="catalog",
            fetched_at="2026-10-01T00:00:00+00:00",
        ),
    )
    return await repo.snapshot_digest(tx)


@pytest.mark.asyncio
@_conforming()
async def test_snapshot_digest_is_deterministic(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await _seed_digest_fixture(registry.repo, tx, [0, 1, 2])
        digest_a = await registry.repo.snapshot_digest(tx)
        digest_b = await registry.repo.snapshot_digest(tx)

    assert digest_a == digest_b, "the same registry must hash to the same digest on repeat reads"
    assert len(digest_a) == 64


@pytest.mark.asyncio
async def test_snapshot_digest_ignores_insertion_order(tmp_path: Any) -> None:
    """Two fresh databases with the same logical contents hash identically."""
    from mnemos.persistence.sqlite import SqliteBackend

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=8))
    digests = []
    for name, order in (("forward.db", [0, 1, 2]), ("reverse.db", [2, 1, 0])):
        backend = SqliteBackend(tmp_path / name, settings)
        await backend.open()
        try:
            async with backend.transactional() as tx:
                digests.append(await _seed_digest_fixture(backend.model_registry, tx, order))
        finally:
            await backend.close()

    assert digests[0] == digests[1], "identical logical contents must hash identically regardless of insert order"


@pytest.mark.asyncio
@_conforming()
async def test_snapshot_digest_tracks_content_changes(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        before = await _seed_digest_fixture(registry.repo, tx, [0, 1, 2])
        await registry.repo.upsert_price(
            tx,
            PriceBand(
                provider="openai",
                model_id="m-b",
                pricing_model="payg",
                band="standard",
                price_in=1.51,
                price_out=3.0,
            ),
        )
        after = await registry.repo.snapshot_digest(tx)
    assert before != after


@pytest.mark.asyncio
@_conforming()
async def test_null_and_zero_produce_different_digests(registry: RegistryHandle) -> None:
    """A digest that conflated unpriced with free would be worthless."""
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, ModelRecord(provider="p", model_id="m"))
        await registry.repo.upsert_price(
            tx,
            PriceBand(provider="p", model_id="m", pricing_model="payg", band="standard", price_in=None, price_out=None),
        )
        unpriced = await registry.repo.snapshot_digest(tx)
        await registry.repo.upsert_price(
            tx,
            PriceBand(provider="p", model_id="m", pricing_model="payg", band="standard", price_in=0.0, price_out=0.0),
        )
        free = await registry.repo.snapshot_digest(tx)
    assert unpriced != free


# ── sync log + health ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@_conforming()
async def test_sync_log_append_and_health(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        await registry.repo.sync_log_append(
            tx,
            SyncLogEntry(
                provider=MODEL.provider,
                models_found=1,
                models_added=1,
                models_updated=0,
                models_deprecated=0,
                error=None,
                duration_ms=42,
                synced_at="2026-10-05T12:00:00+00:00",
            ),
        )
        await registry.repo.sync_log_append(
            tx,
            SyncLogEntry(
                provider=MODEL.provider,
                models_found=0,
                models_added=0,
                models_updated=0,
                models_deprecated=0,
                error="upstream 503",
                duration_ms=7,
                synced_at="2026-10-05T13:00:00+00:00",
            ),
        )
        health = await registry.repo.health_read(tx)

    assert health.backend == registry.repo.backend_name
    assert health.model_count == 1
    assert health.last_sync_at is not None


@pytest.mark.asyncio
@_conforming()
async def test_alias_rows_are_not_counted_as_models(registry: RegistryHandle) -> None:
    """An alias is a pointer, not a model: it must not inflate counts or the digest.

    Regression: ``health_read`` and ``snapshot_digest`` once selected every row
    in ``model_registry``, so registering an alias raised ``model_count`` by
    one and changed the export hash even though no model had been added.
    """
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        before = await registry.repo.health_read(tx)
        digest_before = await registry.repo.snapshot_digest(tx)

        await registry.repo.set_alias(tx, MODEL.provider, "gpt-phase15-latest", MODEL.model_id)

        after = await registry.repo.health_read(tx)
        digest_after = await registry.repo.snapshot_digest(tx)
        alias_row = await registry.repo.get_model(tx, MODEL.provider, "gpt-phase15-latest")

    assert after.model_count == before.model_count
    assert after.priced_count == before.priced_count
    assert after.unpriced_count == before.unpriced_count
    assert digest_after == digest_before, "an alias must not change the export digest"
    # The alias row itself carries no price -- it is never NULL-as-zero.
    assert alias_row is not None
    assert alias_row.price_in is None and alias_row.price_out is None


@pytest.mark.asyncio
@_conforming()
async def test_health_write_round_trips(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        snapshot = await registry.repo.health_read(tx)
    async with registry.transaction() as tx:
        await registry.repo.health_write(tx, snapshot)
        reread = await registry.repo.health_read(tx)
    assert reread.model_count == snapshot.model_count
    assert reread.priced_count == snapshot.priced_count


@pytest.mark.asyncio
@_conforming()
async def test_export_snapshot_is_sorted_and_stable(registry: RegistryHandle) -> None:
    """``export_snapshot`` is the export operation: sorted, stable, complete.

    The document itself — not just its hash — is part of the contract, so a
    consumer can compare or ship an export without reaching into a backend.
    """
    async with registry.transaction() as tx:
        await _seed_digest_fixture(registry.repo, tx, [0, 1, 2])
        first = await registry.repo.export_snapshot(tx)
        second = await registry.repo.export_snapshot(tx)

    assert first == second, "the same registry must export identically on repeat reads"
    assert list(first) == ["models", "prices", "benchmarks", "curated_version"]

    model_keys = [(m["provider"], m["model_id"]) for m in first["models"]]
    assert model_keys == sorted(model_keys), "models must be sorted by (provider, model_id)"
    price_keys = [(p["provider"], p["model_id"], p["pricing_model"], p["band"]) for p in first["prices"]]
    assert price_keys == sorted(price_keys), "prices must be sorted by their logical key"
    benchmark_keys = [(b["provider"], b["model_id"], b["benchmark"]) for b in first["benchmarks"]]
    assert benchmark_keys == sorted(benchmark_keys), "benchmarks must be sorted by their logical key"


@pytest.mark.asyncio
@_conforming()
async def test_export_snapshot_is_the_digest_input(registry: RegistryHandle) -> None:
    """``snapshot_digest`` is exactly the canonical hash of ``export_snapshot``.

    Pinning the relationship keeps the two operations from drifting: a backend
    that hashed a payload the export never showed would disagree here.
    """
    async with registry.transaction() as tx:
        await _seed_digest_fixture(registry.repo, tx, [0, 1, 2])
        snapshot = await registry.repo.export_snapshot(tx)
        digest = await registry.repo.snapshot_digest(tx)

    expected = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert digest == expected
    assert digest == model_registry_snapshot_digest(snapshot)


@pytest.mark.asyncio
async def test_export_snapshot_ignores_insertion_order(tmp_path: Any) -> None:
    """Identical logical contents export identically in any insertion order."""
    from mnemos.persistence.sqlite import SqliteBackend

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=8))
    snapshots = []
    for name, order in (("forward.db", [0, 1, 2]), ("reverse.db", [2, 1, 0])):
        backend = SqliteBackend(tmp_path / name, settings)
        await backend.open()
        try:
            async with backend.transactional() as tx:
                await _seed_digest_fixture(backend.model_registry, tx, order)
                snapshots.append(await backend.model_registry.export_snapshot(tx))
        finally:
            await backend.close()

    assert snapshots[0] == snapshots[1]


@pytest.mark.asyncio
@_conforming()
async def test_export_snapshot_keeps_null_distinct_from_zero(registry: RegistryHandle) -> None:
    """An unpriced row and a free row must not export to the same document."""
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, ModelRecord(provider="p", model_id="m"))
        unpriced = await registry.repo.export_snapshot(tx)
        await registry.repo.upsert_price(
            tx,
            PriceBand(provider="p", model_id="m", pricing_model="payg", band="standard", price_in=0.0, price_out=0.0),
        )
        free = await registry.repo.export_snapshot(tx)

    unpriced_model = unpriced["models"][0]
    free_model = free["models"][0]
    # Prices export in canonical numeric string form: ``None`` for unknown, and
    # ``"0"`` for demonstrably free. The two must never converge on one value.
    assert all(unpriced_model[column] is None for column in ("price_in", "price_out", "price_cached"))
    assert unpriced["prices"] == []
    assert free_model["price_in"] == "0"
    assert unpriced_model != free_model


# ── 7b. the operation set IS the ABC ─────────────────────────────────────────


def test_operation_set_matches_the_abc() -> None:
    """``MODEL_REGISTRY_OPERATIONS`` must name exactly the ABC's abstract methods.

    Regression: the constant advertised ``snapshot_digest`` while the map called
    for ``export_snapshot`` and no such method existed, so a backend could
    conform to the constant and still not export anything. The constant, the
    ABC and every implementation's operation set are now checked as one set.
    """
    abc_operations = set(ModelRegistryRepository.__abstractmethods__)
    assert set(MODEL_REGISTRY_OPERATIONS) == abc_operations
    assert len(MODEL_REGISTRY_OPERATIONS) == len(set(MODEL_REGISTRY_OPERATIONS)), "operations must be unique"
    assert "export_snapshot" in MODEL_REGISTRY_OPERATIONS
    assert "set_alias" in MODEL_REGISTRY_OPERATIONS

    from mnemos.persistence.model_registry_unsupported import UnsupportedModelRegistryRepository
    from mnemos.persistence.sqlite import SqliteModelRegistryRepository

    for cls in (UnsupportedModelRegistryRepository, SqliteModelRegistryRepository):
        for operation in MODEL_REGISTRY_OPERATIONS:
            assert callable(getattr(cls, operation, None)), f"{cls.__name__} is missing {operation}()"


def test_export_snapshot_helper_encodes_canonically() -> None:
    """The shared digest helper is key-order and whitespace insensitive."""
    a = {"models": [{"provider": "p", "model_id": "m"}], "prices": [], "benchmarks": [], "curated_version": 0}
    b = {"curated_version": 0, "benchmarks": [], "prices": [], "models": [{"model_id": "m", "provider": "p"}]}
    assert model_registry_snapshot_digest(a) == model_registry_snapshot_digest(b)
    # ``None`` and ``0.0`` are different facts and must hash differently.
    assert model_registry_snapshot_digest({"x": None}) != model_registry_snapshot_digest({"x": 0.0})


def _sql_params(sql: str, args: tuple[Any, ...]) -> dict[int, Any]:
    """Map ``$n`` placeholder positions to bound args.

    asyncpg binds positionally: ``$1`` is ``args[0]``, ``$2`` is ``args[1]``,
    and so on. Parsing the placeholders out of the SQL (rather than slicing
    ``args`` by position or using negative indexing) keeps the mock correct
    even when a WHERE clause gains or reorders parameters.
    """
    placeholders = [int(m) for m in re.findall(r"\$(\d+)", sql)]
    return {
        number: args[number - 1]
        for number in placeholders
        if 1 <= number <= len(args)
    }


def _fake_postgres_tx(
    rows: list[dict[str, Any]],
    prices: list[dict[str, Any]] | None = None,
    benchmarks: list[dict[str, Any]] | None = None,
    curated_version: int = 0,
):
    """A Postgres transaction stub whose ``conn`` honours the registry SQL.

    Lets the Postgres implementation run in CI without a live database. The
    ``fetch`` stub rejects a model query that does not exclude alias rows or
    does not sort, so a future rewrite that drops either property fails here
    rather than silently producing an unstable digest. Write-path stubs
    (``execute``/``fetchrow``/``fetchval``) replay against in-memory tables so
    the phase-2 operations (price bands, benchmarks, curated routing, health
    writes) can exercise repository inputs and outputs without a server.
    """
    from unittest.mock import MagicMock

    from mnemos.persistence.postgres import PostgresTransaction

    price_rows: list[dict[str, Any]] = [dict(p) for p in (prices or [])]
    bench_rows: list[dict[str, Any]] = [dict(b) for b in (benchmarks or [])]
    curated: list[dict[str, Any]] = []
    health: dict[str, Any] = {}
    model_rows: list[dict[str, Any]] = list(rows)
    version = {"current": int(curated_version)}

    def _project(columns: list[str], row: dict[str, Any]) -> dict[str, Any]:
        """Return only the columns named in the SELECT clause.

        asyncpg ``Record`` objects expose exactly what was selected; returning
        the whole stored dict here would let an implementation read a column
        it never selected (e.g. ``arena_score`` after it was dropped from the
        export SELECT) and pass in CI while raising ``KeyError`` on a real
        database. ``row[column]`` (not ``row.get``) reproduces that behaviour:
        a missing stored key fails loudly instead of masking the bug with
        ``None``.
        """
        if not columns:
            return dict(row)
        return {column: row[column] for column in columns}

    def _model_registry_columns(sql: str) -> list[str]:
        """Parse the top-level column list of a ``SELECT ... FROM`` query.

        Case-insensitive and whitespace-tolerant; stops at the first top-level
        ``FROM`` keyword. Splits on top-level commas only — commas inside
        parentheses (``COALESCE(pricing_model, 'unknown')``, ``CAST(...)``)
        do not start a new item — and resolves an explicit
        ``<expr> AS <name>`` alias (or a bare trailing identifier) to the
        output column name, exactly as Postgres does. Simple column
        references resolve to themselves, so the parser works for both the
        production ``COALESCE(...) AS pricing_model`` projection and flat
        ``SELECT a, b FROM t`` queries.
        """
        match = re.search(r"\bselect\b\s+(.*?)\s+from\b", sql, re.IGNORECASE)
        if match is None:
            return []
        items: list[str] = []
        depth = 0
        current = ""
        for char in match.group(1):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                items.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            items.append(current.strip())
        names: list[str] = []
        for item in items:
            if not item:
                continue
            alias = re.search(r"\bas\s+([a-z_][a-z0-9_]*)\s*$", item, re.IGNORECASE)
            if alias:
                names.append(alias.group(1))
            elif re.fullmatch(r"[a-z_][a-z0-9_]*", item, re.IGNORECASE):
                names.append(item)
            else:
                raise AssertionError(f"unparseable SELECT item: {item!r}")
        return names

    obs_rows: list[dict[str, Any]] = []

    def _apply_update(column_values: dict[str, Any], provider: str, model_id: str) -> None:
        for model in model_rows:
            if model.get("provider") == provider and model.get("model_id") == model_id:
                model.update(column_values)

    class _Conn:
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM model_registry_field_obs" in sql:
                rows = [
                    r
                    for r in obs_rows
                    if r.get("provider") == args[0] and r.get("model_id") == args[1]
                ]
                if "AND source =" in sql:
                    rows = [r for r in rows if r.get("source") == args[-1]]
                return rows
            if "FROM model_registry WHERE alias_of IS NULL" in sql:
                assert "ORDER BY provider ASC, model_id ASC" in sql, "export must be sorted"
                # Filter alias rows out of the data, not just the SQL text, so
                # a stub with an alias present cannot feed it back through.
                canonical = [
                    row for row in model_rows if row.get("alias_of") is None
                ]
                columns = _model_registry_columns(sql)
                coerced_rows = [
                    {
                        **dict(row),
                        "pricing_model": row.get("pricing_model") or "unknown",
                        "band": row.get("band") or "standard",
                    }
                    for row in canonical
                ]
                return sorted(
                    (
                        _project(columns, dict(row))
                        for row in coerced_rows
                    ),
                    key=lambda r: (r["provider"], r["model_id"]),
                )
            if "FROM model_prices" in sql:
                return sorted(
                    price_rows,
                    key=lambda r: (r["provider"], r["model_id"], r["pricing_model"], r["band"]),
                )
            if "FROM model_benchmarks" in sql:
                return sorted(
                    bench_rows, key=lambda r: (r["provider"], r["model_id"], r["benchmark"])
                )
            raise AssertionError(f"unexpected fetch: {sql[:120]}")

        async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
            if "FROM curated_routing ORDER BY version DESC" in sql:
                if not curated:
                    return None
                return dict(sorted(curated, key=lambda r: r["version"])[-1])
            if "SELECT field, value FROM model_registry_field_obs" in sql:
                return [
                    {"field": r["field"], "value": r["value"]}
                    for r in obs_rows
                    if r.get("provider") == args[0] and r.get("model_id") == args[1]
                ]
            if "INSERT INTO curated_routing" in sql and "RETURNING version" in sql:
                # The atomic production form is
                # ``INSERT INTO curated_routing (version, rules, note,
                # created_at) SELECT COALESCE(MAX(version), 0) + 1,
                # $1::jsonb, $2, NOW() ... RETURNING version, rules, note,
                # created_at`` — so ``args[0]`` is rules ($1) and ``args[1]``
                # is note ($2). Assert the call shape so a change that
                # shifts the parameter order fails loudly here, not with
                # mislaid data later.
                assert len(args) == 2, (
                    f"curated_routing INSERT must bind exactly rules+note; got {len(args)} args: {sql[:120]}"
                )
                # The atomic form computes COALESCE(MAX(version), 0) + 1 inside
                # the INSERT itself; replay that allocation here.
                next_version = (
                    max([r["version"] for r in curated] + [version["current"]]) + 1
                )
                row = {
                    "version": next_version,
                    "rules": args[0],
                    "note": args[1],
                    "created_at": "2026-11-18T00:00:00+00:00",
                }
                curated.append(row)
                version["current"] = next_version
                return dict(row)
            if re.search(r"select\s+count\(\*\)\s+as\s+total", sql, re.IGNORECASE):
                total = len([r for r in model_rows if r.get("alias_of") is None])
                priced = len(
                    [
                        r
                        for r in model_rows
                        if r.get("alias_of") is None
                        and (
                            r.get("price_in") is not None
                            or r.get("price_out") is not None
                            or r.get("price_cached") is not None
                        )
                    ]
                )
                return {"total": total, "priced": priced}
            if "SELECT 1 FROM model_registry" in sql:
                return {"1": 1} if any(
                    r.get("provider") == args[0] and r.get("model_id") == args[1]
                    for r in model_rows
                ) else None
            # merge_fields final read: the full column projection of the
            # matching model row, so ``_row_to_model`` sees every field it
            # needs (including ones not touched by the update).
            if "SELECT provider, model_id, display_name, family, context_window" in sql:
                matches = [
                    dict(r)
                    for r in model_rows
                    if r.get("provider") == args[0] and r.get("model_id") == args[1]
                ]
                return matches[0] if matches else None
            raise AssertionError(f"unexpected fetchrow: {sql[:120]}")

        async def fetchval(self, sql: str, *args: Any) -> Any:
            if "MAX(version)" in sql:
                # Replay the production SQL exactly: health_read runs
                # ``SELECT COALESCE(MAX(version), 0) FROM curated_routing``,
                # which yields ``0`` (never ``None``) for an empty table.
                # A bare ``MAX(...)`` would be ``None``; branch on the text
                # so the two forms behave like real Postgres.
                versions = [r["version"] for r in curated]
                if version["current"]:
                    versions.append(version["current"])
                max_version = max(versions) if versions else None
                if re.search(r"\bcoalesce\(", sql, re.IGNORECASE):
                    # COALESCE(MAX(version), 0): only fall back on NULL, never
                    # via truthiness — a valid version of 0 must survive intact.
                    return 0 if max_version is None else max_version
                return max_version
            if "MAX(synced_at)" in sql:
                return None
            raise AssertionError(f"unexpected fetchval: {sql[:120]}")

        async def execute(self, sql: str, *args: Any) -> str:
            if sql.startswith("LOCK TABLE curated_routing "):
                return "LOCK TABLE"
            if sql.strip().startswith("INSERT INTO model_prices"):
                provider, model_id, pricing_model, band = args[0], args[1], args[2], args[3]
                row = {
                    "provider": provider,
                    "model_id": model_id,
                    "pricing_model": pricing_model,
                    "band": band,
                    "price_in": args[4],
                    "price_out": args[5],
                    "price_cached": args[6],
                }
                for existing in price_rows:
                    if (
                        existing["provider"] == provider
                        and existing["model_id"] == model_id
                        and existing["pricing_model"] == pricing_model
                        and existing["band"] == band
                    ):
                        existing.update(row)
                        break
                else:
                    price_rows.append(row)
                return "INSERT 0 1"
            if sql.strip().startswith("INSERT INTO model_benchmarks"):
                provider, model_id, benchmark = args[0], args[1], args[2]
                row = {
                    "provider": provider,
                    "model_id": model_id,
                    "benchmark": benchmark,
                    "score": args[3],
                }
                for existing in bench_rows:
                    if (
                        existing["provider"] == provider
                        and existing["model_id"] == model_id
                        and existing["benchmark"] == benchmark
                    ):
                        existing.update(row)
                        break
                else:
                    bench_rows.append(row)
                return "INSERT 0 1"
            if sql.strip().startswith("UPDATE model_registry SET price_in"):
                # Mirror the SQLite guard: payg always applies; any other
                # pricing model only fills a row that carries no price yet.
                # args: price_in, price_out, price_cached, observed_at,
                #       pricing_model, band, provider, model_id, pricing_model.
                payg = args[8] == "payg"
                carries_price = args[0] is not None or args[1] is not None or args[2] is not None
                for model in model_rows:
                    if model["provider"] == args[6] and model["model_id"] == args[7]:
                        if args[5] != "standard" or not carries_price:
                            return "UPDATE 0"
                        unpriced = (
                            model.get("price_in") is None
                            and model.get("price_out") is None
                            and model.get("price_cached") is None
                        )
                        if payg or unpriced:
                            model["price_in"] = args[0]
                            model["price_out"] = args[1]
                            model["price_cached"] = args[2]
                            model["pricing_model"] = args[4]
                            model["band"] = args[5]
                            return "UPDATE 1"
                        return "UPDATE 0"
                return "UPDATE 0"
            if sql.strip().startswith("INSERT INTO model_registry_health"):
                health["row"] = {
                    "backend": args[0],
                    "model_count": args[1],
                    "priced_count": args[2],
                    "unpriced_count": args[3],
                    "curated_version": args[4],
                }
                return "INSERT 0 1"
            if sql.strip().startswith("INSERT INTO curated_routing "):
                version["current"] = int(args[0])
                row = {
                    "version": int(args[0]),
                    "rules": args[1],
                    "note": args[2],
                    "created_at": "2026-11-18T00:00:00+00:00",
                }
                curated.append(row)
                return "INSERT 0 1"
            if "INSERT INTO model_registry_field_obs" in sql:
                # Parse the column list straight from the SQL
                # (``INSERT INTO t (c1, c2, ...) VALUES (...)``) and bind
                # ``args`` positionally to it — never assume a hard-coded
                # argument order.
                col_match = re.search(
                    r"insert\s+into\s+model_registry_field_obs\s*\(([^)]*)\)",
                    sql,
                    re.IGNORECASE,
                )
                if col_match is None:
                    raise AssertionError(
                        f"INSERT field_obs missing column list: {sql[:120]}"
                    )
                columns = [column.strip() for column in col_match.group(1).split(",")]
                if len(columns) != len(args):
                    raise AssertionError(
                        f"INSERT field_obs column/arg mismatch: {columns} vs {len(args)} args"
                    )
                row = dict(zip(columns, args))
                provider, model_id, field = row["provider"], row["model_id"], row["field"]
                for existing in obs_rows:
                    if (
                        existing["provider"] == provider
                        and existing["model_id"] == model_id
                        and existing["field"] == field
                    ):
                        existing.update(row)
                        break
                else:
                    obs_rows.append(row)
                return "INSERT 0 1"
            if sql.strip().startswith("UPDATE model_registry SET "):
                # merge_fields projection: apply the coerced column values to
                # the matching model row(s). The SQL binds SET values and WHERE
                # parameters with explicit ``$n`` placeholders, so resolve each
                # column's value through its own index — never by slicing
                # ``args`` or using negative indexing.
                match = re.match(
                    r"\s*UPDATE\s+model_registry\s+SET\s+(.*?)(?=\s+WHERE\s)",
                    sql,
                    re.IGNORECASE | re.DOTALL,
                )
                if match is None:
                    raise AssertionError(f"unexpected UPDATE shape: {sql[:120]}")
                where_match = re.search(
                    r"\bWHERE\s+provider\s*=\s*\$(\d+)\s+AND\s+model_id\s*=\s*\$(\d+)",
                    sql,
                    re.IGNORECASE,
                )
                if where_match is None:
                    raise AssertionError(f"missing WHERE provider/model_id: {sql[:120]}")
                params = _sql_params(sql, args)
                provider = params.get(int(where_match.group(1)))
                model_id = params.get(int(where_match.group(2)))
                column_values: dict[str, Any] = {}
                for assignment in match.group(1).split(","):
                    column, separator, placeholder = assignment.partition("=")
                    if not separator:
                        raise AssertionError(
                            f"assignment without '=' in UPDATE SET: {assignment!r}"
                        )
                    match_placeholder = re.search(r"\$(\d+)", placeholder)
                    if match_placeholder is None:
                        # Non-placeholder assignment (e.g. ``updated_at =
                        # NOW()``): merge_fields never generates these; fail
                        # loudly instead of silently skipping the column.
                        raise AssertionError(
                            f"expected '$n' placeholder in UPDATE SET assignment: {assignment!r}"
                        )
                    column_values[column.strip()] = params[int(match_placeholder.group(1))]
                _apply_update(column_values, provider, model_id)
                return "UPDATE 1"
            raise AssertionError(f"unexpected execute: {sql[:120]}")

    tx = MagicMock(spec=PostgresTransaction)
    tx.conn = _Conn()
    return tx


def _registry_row(provider: str, model_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "provider": provider,
        "model_id": model_id,
        "display_name": model_id,
        "family": None,
        "context_window": None,
        "max_output_tokens": None,
        "capabilities": None,
        "price_in": None,
        "price_out": None,
        "price_cached": None,
        "pricing_model": "unknown",
        "band": "standard",
        "alias_of": None,
        "price_updated_at": None,
        "arena_score": None,
        "available": True,
        "deprecated": False,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_postgres_export_snapshot_is_concrete_and_sorted() -> None:
    """``export_snapshot`` is implemented on Postgres, not a ``NotSupported``.

    The live Postgres arm needs a DSN; this pins the implementation itself so
    the operation cannot regress to a raiser while CI has no database.
    """
    from mnemos.persistence.postgres import PostgresModelRegistryRepository

    repo = PostgresModelRegistryRepository()
    assert not type(repo).__abstractmethods__
    assert "export_snapshot" not in repo.MISSING_OPERATIONS
    assert "snapshot_digest" not in repo.MISSING_OPERATIONS
    assert repo.MISSING_OPERATIONS == (), "phase 2 closes every named Postgres gap"

    rows = [
        _registry_row("xai", "grok-4"),
        _registry_row("openai", "gpt-5", price_in=1, price_out=2, pricing_model="payg", band="standard"),
        _registry_row("anthropic", "claude-x", price_cached=0.3, pricing_model="local", band="standard"),
    ]
    prices = [
        {"provider": "openai", "model_id": "gpt-5", "pricing_model": "payg", "band": "standard", "price_in": 1, "price_out": 2, "price_cached": None},
        {"provider": "openai", "model_id": "gpt-5", "pricing_model": "payg", "band": "peak", "price_in": 9, "price_out": 9, "price_cached": None},
        {"provider": "anthropic", "model_id": "claude-x", "pricing_model": "local", "band": "standard", "price_in": None, "price_out": None, "price_cached": 0.3},
    ]
    benchmarks = [
        {"provider": "openai", "model_id": "gpt-5", "benchmark": "arena_score", "score": 1300},
    ]
    snapshot = await repo.export_snapshot(_fake_postgres_tx(rows, prices, benchmarks, curated_version=2))

    assert [m["model_id"] for m in snapshot["models"]] == ["claude-x", "gpt-5", "grok-4"]
    # The document shape matches the SQLite reference.
    assert set(snapshot) == {"models", "prices", "benchmarks", "curated_version"}
    assert snapshot["curated_version"] == 2
    # Per-band rows export (peak no longer collapses into the model row).
    assert [(p["model_id"], p["band"]) for p in snapshot["prices"]] == [
        ("claude-x", "standard"),
        ("gpt-5", "peak"),
        ("gpt-5", "standard"),
    ]
    assert next(p for p in snapshot["prices"] if p["model_id"] == "claude-x")["price_cached"] == "0.3"
    # NULL stays NULL: unpriced is never exported as "0".
    grok = next(m for m in snapshot["models"] if m["model_id"] == "grok-4")
    assert grok["price_in"] is None and grok["price_cached"] is None
    assert [(b["model_id"], b["benchmark"]) for b in snapshot["benchmarks"]] == [("gpt-5", "arena_score")]


@pytest.mark.asyncio
async def test_postgres_phase2_operations_behave_like_sqlite() -> None:
    """Exercise phase-2 repository inputs and outputs with an in-memory connection stub."""
    from mnemos.persistence.base import (
        CuratedRouting,
        ModelRegistryHealth,
        PriceBand,
    )
    from mnemos.persistence.postgres import PostgresModelRegistryRepository

    repo = PostgresModelRegistryRepository()
    rows = [_registry_row("openai", "gpt-5")]
    tx = _fake_postgres_tx(rows)

    # A free_tier 0.0 mirrors onto the unpriced row ...
    await repo.upsert_price(
        tx,
        PriceBand(
            provider="openai", model_id="gpt-5", pricing_model="free_tier", band="standard",
            price_in=0.0, price_out=0.0,
        ),
    )
    assert rows[0]["price_in"] == 0.0
    assert rows[0]["pricing_model"] == "free_tier"
    # ... and a later non-payg row must NOT displace it (SQLite guard parity).
    await repo.upsert_price(
        tx,
        PriceBand(
            provider="openai", model_id="gpt-5", pricing_model="subscription", band="standard",
            price_in=42.0, price_out=84.0,
        ),
    )
    assert rows[0]["price_in"] == 0.0, "non-payg must not displace a mirrored price"
    assert rows[0]["pricing_model"] == "free_tier"
    # ... but payg is authoritative and overwrites.
    await repo.upsert_price(
        tx,
        PriceBand(
            provider="openai", model_id="gpt-5", pricing_model="payg", band="standard",
            price_in=1.0, price_out=2.0,
        ),
    )
    assert rows[0]["price_in"] == 1.0
    assert rows[0]["pricing_model"] == "payg"
    # A peak band never touches the model row.
    await repo.upsert_price(
        tx,
        PriceBand(
            provider="openai", model_id="gpt-5", pricing_model="payg", band="peak",
            price_in=9.0, price_out=9.0,
        ),
    )
    assert rows[0]["price_in"] == 1.0
    # Invalid band / pricing model are rejected, not stored.
    with pytest.raises(ValueError):
        await repo.upsert_price(
            tx, PriceBand(provider="openai", model_id="gpt-5", pricing_model="payg", band="surge")
        )
    with pytest.raises(ValueError):
        await repo.upsert_price(
            tx, PriceBand(provider="openai", model_id="gpt-5", pricing_model="barter", band="standard")
        )

    # Benchmarks upsert with provenance.
    await repo.upsert_benchmark(
        tx, provider="openai", model_id="gpt-5", benchmark="arena_score",
        score=1300.5, source="arena", fetched_at="2026-10-01T00:00:00+00:00",
    )
    snapshot = await repo.export_snapshot(tx)
    assert snapshot["benchmarks"] == [
        {"provider": "openai", "model_id": "gpt-5", "benchmark": "arena_score", "score": "1300.5"}
    ]

    # Curated routing: empty at first, then versioned and append-only.
    assert await repo.curated_routing_get(tx) is None
    first = await repo.curated_routing_replace(tx, rules=[{"task_type": "code"}], note="initial")
    second = await repo.curated_routing_replace(tx, rules=[{"task_type": "chat"}], note="adjusted")
    assert isinstance(first, CuratedRouting) and isinstance(second, CuratedRouting)
    assert (first.version, second.version) == (1, 2)
    current = await repo.curated_routing_get(tx)
    assert current is not None and current.version == 2
    assert current.rules == ({"task_type": "chat"},)
    assert current.note == "adjusted"

    # Health writes land without raising.
    await repo.health_write(
        tx,
        ModelRegistryHealth(
            backend="postgres", model_count=1, priced_count=1,
            unpriced_count=0, curated_version=2, last_sync_at=None,
        ),
    )


@pytest.mark.asyncio
async def test_postgres_snapshot_digest_is_the_canonical_export_hash() -> None:
    """``snapshot_digest`` is exactly the shared hash of ``export_snapshot``."""
    from mnemos.persistence.postgres import PostgresModelRegistryRepository

    repo = PostgresModelRegistryRepository()
    rows = [_registry_row("openai", "gpt-5", price_in=1), _registry_row("xai", "grok-4")]
    tx = _fake_postgres_tx(rows)

    snapshot = await repo.export_snapshot(tx)
    digest = await repo.snapshot_digest(tx)
    assert digest == model_registry_snapshot_digest(snapshot)
    # Stable across identical inputs, independent of call order.
    assert digest == await repo.snapshot_digest(_fake_postgres_tx(list(reversed(rows))))
    # A price change that matters must move the digest.
    repriced = [_registry_row("openai", "gpt-5", price_in=9), _registry_row("xai", "grok-4")]
    assert await repo.snapshot_digest(_fake_postgres_tx(repriced)) != digest


@pytest.mark.asyncio
async def test_postgres_export_snapshot_keeps_legacy_prices_and_arena_scores() -> None:
    """``export_snapshot`` never silently empties prices or benchmarks.

    A registry priced through the legacy path (``upsert_model_pricing``) and a
    model carrying only ``arena_score`` must still export values even when the
    ``model_prices`` / ``model_benchmarks`` tables hold no rows for them.
    """
    from mnemos.persistence.postgres import PostgresModelRegistryRepository

    repo = PostgresModelRegistryRepository()
    rows = [
        _registry_row("openai", "gpt-legacy", price_in=5.0, price_out=10.0),
        _registry_row("xai", "grok-arena", arena_score=1299.5),
        _registry_row("openai", "gpt-band", price_in=1.0),
    ]
    prices = [
        {
            "provider": "openai",
            "model_id": "gpt-band",
            "pricing_model": "payg",
            "band": "standard",
            "price_in": 1,
            "price_out": None,
            "price_cached": None,
        },
    ]
    tx = _fake_postgres_tx(rows, prices=prices, benchmarks=[], curated_version=0)
    snapshot = await repo.export_snapshot(tx)

    # The legacy-priced model gets a standard-band fallback entry ...
    legacy = [p for p in snapshot["prices"] if p["model_id"] == "gpt-legacy"]
    assert legacy == [
        {
            "provider": "openai",
            "model_id": "gpt-legacy",
            "pricing_model": "unknown",
            "band": "standard",
            "price_in": "5",
            "price_out": "10",
            "price_cached": None,
        }
    ]
    # The benchmark comes from the legacy arena_score column.
    assert [b["benchmark"] for b in snapshot["benchmarks"]] == ["arena_score"]
    arena = snapshot["benchmarks"][0]
    assert arena["model_id"] == "grok-arena"
    assert arena["score"] == "1299.5"
    # No duplicate fallback for the model that already has a band row.
    band_entries = [p for p in snapshot["prices"] if p["model_id"] == "gpt-band"]
    assert len(band_entries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", ["postgres"], indirect=True)
async def test_postgres_curated_routing_concurrent_writers(registry: RegistryHandle) -> None:
    second_pid = asyncio.get_running_loop().create_future()

    async def replace_second() -> CuratedRouting:
        async with registry.transaction() as tx:
            second_pid.set_result(tx.conn.get_server_pid())
            return await registry.repo.curated_routing_replace(tx, rules=[{"task_type": "chat"}])

    async with asyncio.TaskGroup() as tasks:
        async with registry.transaction() as tx:
            first = await registry.repo.curated_routing_replace(tx, rules=[{"task_type": "code"}])
            second_task = tasks.create_task(replace_second())
            # Let the second writer reach the database while the first write is uncommitted.
            async with asyncio.timeout(5):
                pid = await second_pid
                while not await tx.conn.fetchval(
                    "SELECT pg_backend_pid() = ANY(pg_blocking_pids($1))", pid
                ):
                    await asyncio.sleep(0.01)

    second = second_task.result()
    assert (first.version, second.version) == (1, 2)
    async with registry.transaction() as tx:
        assert await registry.repo.curated_routing_get(tx) == second
        assert await tx.conn.fetchval("SELECT COUNT(*) FROM curated_routing") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", ["postgres"], indirect=True)
async def test_postgres_distinct_aliases_can_share_a_target(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        for alias in ("gpt-latest", "gpt-preview"):
            await registry.repo.set_alias(tx, MODEL.provider, alias, MODEL.model_id)
            resolved = await registry.repo.resolve_alias(tx, MODEL.provider, alias)
            assert resolved is not None and resolved.model_id == MODEL.model_id


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", ["postgres"], indirect=True)
async def test_postgres_legacy_pricing_records_explicit_zero(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        await registry.repo.upsert_model(tx, MODEL)
        updates, old = await registry.backend.consultations_audit.upsert_model_pricing(
            tx, provider=MODEL.provider, model_id=MODEL.model_id,
            price_in=0, price_out=0, price_cached=0,
        )
        assert updates == 1
        assert old == {"price_in": None, "price_out": None, "price_cached": None}
        assert [model.model_id for model in await registry.repo.list_models(tx, ModelFilter(priced=True))] == [
            MODEL.model_id
        ]
        snapshot = await registry.repo.export_snapshot(tx)
        assert snapshot["prices"][0]["price_in"] == "0"


@pytest.mark.asyncio
async def test_postgres_migration_0073_backfills_observed_prices_only() -> None:
    dsn = os.environ.get(POSTGRES_DSN_ENV)
    if not dsn:
        pytest.skip(f"set {POSTGRES_DSN_ENV} to run this migration test")

    import asyncpg

    migrations = Path(__file__).resolve().parents[2] / "mnemos/db_migrations/migrations"
    schema = f"registry_0073_{uuid4().hex}"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}")
        await conn.execute(
            """
            CREATE TABLE model_registry (
                provider VARCHAR(50), model_id TEXT, alias_of TEXT, arena_score NUMERIC,
                PRIMARY KEY (provider, model_id)
            );
            CREATE TABLE model_registry_sync_log (synced_at TIMESTAMPTZ);
            INSERT INTO model_registry (provider, model_id, alias_of) VALUES
                ('example', 'unpriced', NULL), ('example', 'free', NULL),
                ('example', 'alias-one', 'unpriced'), ('example', 'alias-two', 'unpriced');
            """
        )
        await conn.execute((migrations / "0044_model_registry_pricing.sql").read_text())
        await conn.execute(
            """
            UPDATE model_registry
            SET price_updated_at = '2026-01-01T00:00:00Z', arena_score = 1234
            WHERE model_id = 'free'
            """
        )
        migration = (migrations / "0073_model_registry_repository_phase2.sql").read_text()
        await conn.execute(migration)
        await conn.execute(migration)
        await conn.execute(
            "INSERT INTO model_registry (provider, model_id) VALUES ('example', 'new-model')"
        )

        rows = await conn.fetch(
            "SELECT model_id, price_in, price_out, price_cached FROM model_registry ORDER BY model_id"
        )
        assert [tuple(row) for row in rows] == [
            ("alias-one", None, None, None),
            ("alias-two", None, None, None),
            ("free", 0, 0, 0),
            ("new-model", None, None, None),
            ("unpriced", None, None, None),
        ]
        prices = await conn.fetch(
            "SELECT model_id, price_in, observed_at = '2026-01-01T00:00:00Z' AS original_time "
            "FROM model_prices"
        )
        assert [tuple(row) for row in prices] == [("free", 0, True)]
        assert await conn.fetchval("SELECT benchmark FROM model_benchmarks") == "arena_score"
    finally:
        try:
            await conn.execute("ROLLBACK")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


@pytest.mark.asyncio
async def test_postgres_merge_fields_skips_malformed_observations() -> None:
    """One bad observation must not abort the projection of good ones.

    ``context_window = "128k"`` (free-form provider text) is kept as an
    unprojected observation; ``display_name`` is still merged, and no
    ``ValueError`` escapes.
    """
    from mnemos.persistence.base import FieldObservation
    from mnemos.persistence.postgres import PostgresModelRegistryRepository

    repo = PostgresModelRegistryRepository()
    rows = [_registry_row("openai", "gpt-5")]
    tx = _fake_postgres_tx(rows)
    conn = tx.conn

    # Record observations through the replayed INSERT ...
    await repo.record_field_obs(
        tx,
        FieldObservation(
            provider="openai",
            model_id="gpt-5",
            field="context_window",
            value="128k",
            source="provider",
            fetched_at="2026-11-01T00:00:00+00:00",
        ),
    )
    await repo.record_field_obs(
        tx,
        FieldObservation(
            provider="openai",
            model_id="gpt-5",
            field="display_name",
            value="GPT-5",
            source="provider",
            fetched_at="2026-11-01T00:00:00+00:00",
        ),
    )
    merged = await repo.merge_fields(tx, "openai", "gpt-5")
    assert merged is not None
    # The good field was projected; the malformed one was not.
    assert merged.display_name == "GPT-5"
    assert merged.context_window is None
    # The malformed observation survives in the table, unprojected. The stub
    # stores full row dicts (as asyncpg Records would), so filter by the
    # columns we care about rather than comparing whole dicts.
    remaining = await conn.fetch(
        "SELECT field, value FROM model_registry_field_obs WHERE provider = $1 AND model_id = $2",
        "openai",
        "gpt-5",
    )
    fields = [r["field"] for r in remaining]
    assert "context_window" in fields


@pytest.mark.asyncio
async def test_postgres_health_read_reports_existing_curated_version() -> None:
    """``health_read`` reports a non-zero curated version after migration.

    The authoritative table is ``curated_routing``; once the operator layer
    has written versions (or 0073 copied them over), the health snapshot
    must carry them, never reset to 0.
    """
    from mnemos.persistence.base import ModelRegistryHealth
    from mnemos.persistence.postgres import PostgresModelRegistryRepository

    repo = PostgresModelRegistryRepository()
    tx = _fake_postgres_tx([], curated_version=2)
    health = await repo.health_read(tx)
    assert isinstance(health, ModelRegistryHealth)
    assert health.curated_version == 2
    assert health.model_count == 0
    assert health.unpriced_count == 0
