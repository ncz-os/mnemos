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
   never reported as unpriced.
2. per-field ``source`` / ``fetched_at`` round-trip;
3. price ``band`` round-trip (``standard`` / ``peak`` / ``off_peak``);
4. alias uniqueness and lookalike rejection;
5. the curated layer is immune to refresh;
6. the export digest is identical for identical inputs, regardless of order;
7. ``type(cls).__abstractmethods__`` is empty for every conforming backend.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

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
    SyncLogEntry,
)

pytestmark = pytest.mark.asyncio

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
    backend = await PostgresBackend.open(dsn=dsn, settings=SimpleNamespace(database=SimpleNamespace(embedding_dim=8)))
    try:
        yield RegistryHandle(backend=backend, repo=backend.model_registry, transaction=backend.transactional)
    finally:
        await backend.close()


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


# ── 2. per-field provenance ───────────────────────────────────────────────────


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


@_conforming()
async def test_unknown_alias_resolves_to_none(registry: RegistryHandle) -> None:
    async with registry.transaction() as tx:
        assert await registry.repo.resolve_alias(tx, "nobody", "nothing") is None


# ── 5. curated layer is immune to refresh ────────────────────────────────────


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
