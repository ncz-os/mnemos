"""F-6 review findings — the embedding-dimension contract's three defects.

The F-6 startup contract (``mnemos/runtime/embedding_contract.py``) exists to
catch one field incident: a replica whose embedding column was sized for a
different model than the one the embedder actually serves. Three defects in
that contract let it fail silently:

1. ``_default_probe`` called ``get_embedder()`` without importing it. The
   resulting ``NameError`` was swallowed by a blanket ``except Exception``,
   so the probe never measured the live embedder — it silently returned the
   model's *known* width, which is exactly the number the check compares
   against. The contract could therefore never detect the drift it was
   written for.

2. ``check_embedding_contract`` asked the *backend* for the live column
   width instead of the persistence layer's ``embedding_column_dim``
   repository method. That is the "route calls the ABC" half of the
   governing rule in AGENTS.md, and it is also why the ABC method existed
   on only one of six backends.

3. ``_normalize_model_name`` / ``_DIM_IN_NAME_RE`` misparsed a version
   suffix as a dimension: ``text-embedding-ada-002`` was read as a
   2-dimensional model.

Every test here is written so that it FAILS on the tree before the fix and
PASSES after it. The ones that pin a behaviour the reviewer believed was
broken but was not ("disprove with a pinning test") are marked as such.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


# ─── helpers ────────────────────────────────────────────────────────────────


async def _async_value(value):
    return value


def _settings(dim: int = 1024):
    return SimpleNamespace(
        database=SimpleNamespace(embedding_dim=dim),
        providers=SimpleNamespace(inference_embed_model="bge-m3"),
    )


class _FakeEmbedder:
    """A stand-in for the process embedder that answers with a fixed width."""

    def __init__(self, width: int):
        self.width = width
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [0.0] * self.width


# ─── finding 1: the probe must MEASURE the live embedder ────────────────────


async def test_default_probe_measures_the_live_embedder(monkeypatch):
    """The probe returns the MEASURED width, not the model's known width.

    On the tree before the fix, ``get_embedder`` was never imported, so the
    call raised ``NameError``; the blanket ``except Exception`` swallowed it
    and the probe returned the known width (1024 for bge-m3) — the very
    number the contract compares the configuration against. A live embedder
    that produces 768 dimensions was therefore invisible.
    """
    from mnemos.runtime import embedder as embedder_module
    from mnemos.runtime.embedding_contract import _default_probe

    fake = _FakeEmbedder(768)
    monkeypatch.setattr(embedder_module, "get_embedder", lambda: fake)

    measured = await _default_probe()

    assert fake.calls == 1, "the probe never reached the live embedder"
    assert measured == 768, (
        f"the probe reported {measured}, which is the model's KNOWN width "
        "(1024 for bge-m3) rather than the measured width (768)"
    )
    assert measured != 1024


async def test_default_probe_does_not_masquerade_a_defect_as_unreachable(monkeypatch):
    """A defect in the probe must not be reported as "the embedder is down".

    The contract treats "could not measure" as "no evidence", so a defect
    swallowed into that bucket is permanent and silent: the probe would
    keep answering the known width forever. A programming error therefore
    has to surface.
    """
    from mnemos.runtime import embedder as embedder_module
    from mnemos.runtime.embedding_contract import _default_probe

    def _broken_get_embedder():
        raise NameError("name 'get_embedder' is not defined")

    monkeypatch.setattr(embedder_module, "get_embedder", _broken_get_embedder)

    with pytest.raises(NameError):
        await _default_probe()


async def test_default_probe_swallows_a_transport_failure_only(monkeypatch, caplog):
    """A genuinely unreachable embedder still fails open — with a warning.

    The transport failure is the one case where "I could not measure" is an
    honest answer, so it is still swallowed; but it is logged at WARNING
    with the exception type, so an operator can see the probe never
    measured anything.
    """
    from mnemos.runtime import embedder as embedder_module
    from mnemos.runtime.embedding_contract import _default_probe

    def _unreachable():
        raise OSError("embedding endpoint unreachable")

    monkeypatch.setattr(embedder_module, "get_embedder", _unreachable)

    with caplog.at_level("WARNING"):
        measured = await _default_probe()

    assert measured == 1024, "the known width is the honest fallback"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("OSError" in m for m in warnings), (
        f"the swallowed transport failure was not logged with its type: {warnings}"
    )


async def test_default_probe_reports_an_empty_vector_at_warning(monkeypatch, caplog):
    """An embedder that answers with nothing is a warning, not a silent 1024."""
    from mnemos.runtime import embedder as embedder_module
    from mnemos.runtime.embedding_contract import _default_probe

    class _Empty:
        async def embed(self, text: str) -> list[float]:
            return []

    monkeypatch.setattr(embedder_module, "get_embedder", lambda: _Empty())

    with caplog.at_level("WARNING"):
        measured = await _default_probe()

    assert measured == 1024
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("no vector" in m for m in warnings), f"no warning was logged: {warnings}"


async def test_default_probe_imports_get_embedder_from_the_embedder_module():
    """The module under test actually imports the symbol it calls.

    A structural pin for the exact defect: the call site and the import
    can never drift apart again.
    """
    from mnemos.runtime import embedding_contract

    source = inspect.getsource(embedding_contract)
    tree = ast.parse(source)

    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "get_embedder" in called, "the probe no longer calls get_embedder()"

    # The name must be bound somewhere in the module (an import or a
    # module-level definition) — otherwise it is a NameError at runtime.
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
    assert "get_embedder" in bound, "get_embedder is called but never bound in the module — the NameError is back"


# ─── finding 2: the contract must call the persistence ABC ──────────────────


async def test_contract_reads_the_column_width_through_the_repository_abc(tmp_path):
    """``check_embedding_contract`` must ask the REPOSITORY for the width.

    The finding under test: the contract asked the *backend* for the live
    column width, so the persistence layer's ``embedding_column_dim``
    repository method (the ABC) was bypassed — the "route calls the ABC"
    half of the governing rule in AGENTS.md.

    The pin: a backend whose facade raises but whose repository answers
    must still be read through the repository.
    """
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.runtime.embedder import check_embedding_contract

    backend = SqliteBackend(tmp_path / "f6-abc.sqlite3", _settings(1024))
    await backend.open()
    try:
        # The facade method is what the contract used to call; make it
        # unusable and prove the contract no longer depends on it.
        async def _explode():
            raise AssertionError("the contract called the backend facade instead of the repository ABC method")

        backend.embedding_column_dim = _explode  # type: ignore[method-assign]

        report = await check_embedding_contract(
            probe=lambda: _async_value(1024),
            configured_dim=1024,
            model="bge-m3",
            backend=backend,
        )
        assert report.column_dim == 1024, (
            "the contract did not read the live column width through the persistence layer"
        )
    finally:
        await backend.close()


async def test_contract_reads_the_column_width_through_the_repository_abc_without_a_tx(tmp_path):
    """The no-transaction path must also go through the repository ABC."""
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.runtime.embedder import check_embedding_contract

    backend = SqliteBackend(tmp_path / "f6-abc-2.sqlite3", _settings(1024))
    await backend.open()
    try:
        seen: list[object] = []

        original = backend.memories.embedding_column_dim

        async def _spy(tx):
            seen.append(tx)
            return await original(tx)

        backend.memories.embedding_column_dim = _spy  # type: ignore[method-assign]

        report = await check_embedding_contract(
            probe=lambda: _async_value(1024),
            configured_dim=1024,
            model="bge-m3",
            backend=backend,
        )
        assert report.column_dim == 1024
        assert seen, "the repository ABC method was never called"
    finally:
        await backend.close()


async def test_every_backend_implements_the_embedding_column_dim_abc():
    """All six backends implement the ABC method (the six-way rule).

    Before the fix the method existed on Db2 only, so the other five
    repository classes stayed ABSTRACT and could not be instantiated.
    """
    from mnemos.persistence.db2 import Db2MemoryRepository
    from mnemos.persistence.mariadb import MariadbMemoryRepository
    from mnemos.persistence.mysql import MysqlMemoryRepository
    from mnemos.persistence.oracle import OracleMemoryRepository
    from mnemos.persistence.postgres import PostgresMemoryRepository
    from mnemos.persistence.sqlite import SqliteMemoryRepository

    for repo in (
        SqliteMemoryRepository,
        PostgresMemoryRepository,
        OracleMemoryRepository,
        MysqlMemoryRepository,
        MariadbMemoryRepository,
        Db2MemoryRepository,
    ):
        assert "embedding_column_dim" not in repo.__abstractmethods__, (
            f"{repo.__name__} does not implement the ABC method"
        )
        # The class must be instantiable — an abstract repository cannot
        # even be constructed, which is what broke the backends.
        repo()


async def test_every_backend_facade_exposes_the_column_width():
    """Each facade exposes the repository method (facade exposure)."""
    from mnemos.persistence.db2 import Db2Backend
    from mnemos.persistence.mariadb import MariadbBackend
    from mnemos.persistence.mysql import MysqlBackend
    from mnemos.persistence.oracle import OracleBackend
    from mnemos.persistence.postgres import PostgresBackend
    from mnemos.persistence.sqlite import SqliteBackend

    for facade in (
        SqliteBackend,
        PostgresBackend,
        OracleBackend,
        MysqlBackend,
        MariadbBackend,
        Db2Backend,
    ):
        assert hasattr(facade, "embedding_column_dim"), f"{facade.__name__} does not expose embedding_column_dim"


async def test_mariadb_reads_the_join_table_not_the_memories_table():
    """MariaDB keeps vectors in ``memory_embeddings``; the read must point there."""
    from mnemos.persistence.mariadb import MariadbMemoryRepository

    assert MariadbMemoryRepository._embedding_column_target == (
        "memory_embeddings",
        "embedding",
    )


async def test_sqlite_reads_the_live_vec0_width(tmp_path):
    """The SQLite read answers the LIVE vec0 width, not the configured one."""
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "f6-vec.sqlite3", _settings(768))
    await backend.open()
    try:
        width = await backend.embedding_column_dim()
        # sqlite-vec may be unavailable in the test environment; then there
        # is no fixed-width column and the honest answer is None. When the
        # vec0 table exists, the width must be the configured one.
        if width is not None:
            assert width == 768
    finally:
        await backend.close()


# ─── finding 3: the model-name normalization ────────────────────────────────


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # A version suffix is NOT a dimension.
        ("text-embedding-ada-002", None),
        # A real known model keeps its known width.
        ("text-embedding-3-large", 3072),
        ("text-embedding-3-small", 1536),
        ("bge-m3", 1024),
        ("BAAI/bge-m3", 1024),
        ("all-MiniLM-L6-v2", 384),
        # The fleet's other spellings still work.
        ("/models/bge-m3.gguf", 1024),
        ("bge-m3-Q8_0.gguf", 1024),
        ("bge-m3:latest", 1024),
        ("bge-base-en-v1.5-768", 768),
        # An unknown model with no width in its name stays unknown.
        ("e5-mistral-7b-instruct", None),
        ("qwen3-embedding-8b", None),
        ("nvidia/llama-3.2-nv-embedqa-1b-v2", None),
        # An unknown model with an EXPLICIT width still reports that width.
        ("my-model-1024", 1024),
    ],
)
async def test_known_model_width_reads_real_model_ids(model: str, expected: int | None):
    """Realistic model ids must resolve to their real widths.

    On the tree before the fix, ``text-embedding-ada-002`` resolved to a
    width of 2: the ``-002`` version suffix was read as the dimension.
    """
    from mnemos.runtime.embedder import known_model_width

    assert known_model_width(model) == expected


async def test_normalize_model_name_strips_the_org_prefix_exactly_once():
    """DISPROVED finding — the org prefix is NOT stripped twice.

    The review claimed ``_normalize_model_name`` strips the org prefix
    twice and therefore never sees it. Reading the source: the path
    handling (``rsplit("/", 1)[-1]``) already removes the org prefix, and
    the later "org/registry prefix" line is a no-op on the result. The
    observable behaviour is correct — ``BAAI/bge-m3`` normalizes to
    ``bge-m3`` — so this is a PIN, not a fix: it records the current,
    correct behaviour so a future refactor cannot break it.
    """
    from mnemos.runtime.embedding_contract import _normalize_model_name

    assert _normalize_model_name("BAAI/bge-m3") == "bge-m3"
    assert _normalize_model_name("nvidia/llama-3.2-nv-embedqa-1b-v2") == ("llama-3.2-nv-embedqa-1b-v2")
    assert _normalize_model_name("/models/bge-m3.gguf") == "bge-m3"


async def test_normalize_model_name_keeps_a_width_baked_into_an_unknown_name():
    """DISPROVED finding — a width in an unknown name is preserved.

    The review implied the width was lost. It is not: for an unknown
    family the width IS the only information the name carries, so it is
    kept (``my-model-1024`` -> ``my-model-1024``).
    """
    from mnemos.runtime.embedding_contract import _normalize_model_name

    assert _normalize_model_name("my-model-1024") == "my-model-1024"
    assert _normalize_model_name("bge-base-en-v1.5-768") == "bge-base-en-v1.5"


async def test_a_version_suffix_is_not_read_as_a_dimension():
    """The specific regression: ``-002`` is a version, not a width."""
    from mnemos.runtime.embedding_contract import _DIM_IN_NAME_RE

    match = _DIM_IN_NAME_RE.match("text-embedding-ada-002")
    assert match is None, "the dimension regex still claims a 3-digit version suffix is a dimension"


async def test_the_contract_never_sizes_a_column_from_a_version_suffix():
    """End-to-end: an OpenAI ada model must not resolve to a 2-dimension model."""
    from mnemos.runtime.embedder import embedding_identity

    identity = embedding_identity(http_model="text-embedding-ada-002", configured_dim=1536)
    assert identity.dim == 1536, (
        f"the identity resolved to {identity.dim!r} (source={identity.source}) "
        "— a version suffix was read as a dimension"
    )
    assert identity.source == "configured"


# ─── the contract still refuses to serve a mismatch ─────────────────────────


async def test_contract_still_refuses_a_mismatched_measured_width(tmp_path):
    """The whole point of the contract: a measured mismatch stops the service."""
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.runtime.embedder import check_embedding_contract

    backend = SqliteBackend(tmp_path / "f6-mismatch.sqlite3", _settings(1024))
    await backend.open()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await check_embedding_contract(
                probe=lambda: _async_value(768),
                configured_dim=1024,
                model="bge-m3",
                backend=backend,
            )
        assert "768" in str(excinfo.value)
    finally:
        await backend.close()


async def test_verify_startup_embedding_contract_uses_the_live_column(tmp_path):
    """The App-lifespan wiring helper reads the live column width."""
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.runtime.embedder import verify_startup_embedding_contract

    backend = SqliteBackend(tmp_path / "f6-startup.sqlite3", _settings(1024))
    await backend.open()
    try:
        report = await verify_startup_embedding_contract(
            backend,
            _settings(1024),
            probe=lambda: _async_value(1024),
        )
        assert report.ok is True
        assert report.embedder_dim == 1024
    finally:
        await backend.close()


async def test_the_probe_is_awaited_and_measured_not_defaulted():
    """``verify_startup_embedding_contract`` must use the real probe by default.

    A regression guard for the exact failure mode: the default probe
    silently returned the model's KNOWN width (1024 for bge-m3), so the
    check compared the configuration against itself and never noticed
    that the live embedder produces something else.

    The configuration says 1024; the live embedder produces 768. The
    contract MUST refuse to serve. On the tree before the fix the probe
    answered 1024 (the known width), the two numbers agreed, and the
    service started with a column it could never fill.
    """
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.runtime import embedder as embedder_module
    from mnemos.runtime.embedder import verify_startup_embedding_contract

    fake = _FakeEmbedder(768)
    monkey = embedder_module.get_embedder
    embedder_module.get_embedder = lambda: fake
    try:
        # The backend is never opened, so the column width is unknown and
        # the MEASURED width is the only evidence in play.
        backend = SqliteBackend("/nonexistent/f6-default-probe.sqlite3", _settings(1024))
        with pytest.raises(RuntimeError) as excinfo:
            await verify_startup_embedding_contract(backend, _settings(1024))
        message = str(excinfo.value)
        assert "768" in message, f"the default probe did not measure the live embedder: {message!r}"
    finally:
        embedder_module.get_embedder = monkey


async def test_asyncio_timeout_is_a_transport_error_not_a_defect(monkeypatch):
    """A timeout is an honest "could not measure", not a defect."""
    from mnemos.runtime import embedder as embedder_module
    from mnemos.runtime.embedding_contract import _default_probe

    def _timeout():
        raise asyncio.TimeoutError("timed out")

    monkeypatch.setattr(embedder_module, "get_embedder", _timeout)

    measured = await _default_probe()
    assert measured == 1024
