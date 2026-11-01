"""F-6 — the embedding-dimension contract, cross-checked at startup.

Field problem the directory already knows about: the PROTEUS replica carried
24,986 NULL embeddings in a ``vector(768)`` column while the fleet standard is
bge-m3 at 1024 dimensions. Three separate silent paths made that possible:

* the shipped defaults disagreed — ``MNEMOS_EMBEDDING_DIM`` defaulted to 768
  while the HTTP embedding model defaulted to ``bge-m3`` (1024-D);
* nothing compared the embedder's *discovered* output width with the
  configured width, so a wrong model simply made every write fail the
  dimension guard and every search fall back to FTS;
* a row whose vector came from a different model was indistinguishable from
  a row that was never embedded, because ``embedding_model`` was never
  recorded at write time.

This module pins the fixes:

1. the default pair agrees (1024 == bge-m3's width);
2. ``embedding_identity()`` resolves the model name and width every write /
   backfill / startup check must use, and never reports a width the model
   cannot produce;
3. ``check_embedding_contract()`` fails loud with one actionable message
   when the embedder's discovered width, the configured width, or the live
   column width disagree.

Everything here runs on the SQLite backend or on pure functions, so no
server is required.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# ─── 1. the shipped default pair ────────────────────────────────────────────


def test_default_embedding_dim_matches_the_default_http_model_width():
    """MNEMOS_EMBEDDING_DIM and MNEMOS_EMBED_HTTP_MODEL must agree by default.

    On the base commit this is 768 vs ``bge-m3`` (1024-D), which is the exact
    pair that produced the PROTEUS field incident.
    """
    from mnemos.core.config import Settings
    from mnemos.runtime.embedder import known_model_width

    width = known_model_width()

    default_dim = Settings().database.embedding_dim

    assert width == 1024, "the fleet standard embedding model is bge-m3 at 1024 dimensions"
    assert default_dim == width, (
        f"MNEMOS_EMBEDDING_DIM defaults to {default_dim} but the default HTTP embed "
        f"model is a {width}-dimensional model; a fresh deployment would silently "
        "put every vector out of the column's reach"
    )


def test_embedding_dim_env_default_follows_the_model_too(monkeypatch):
    """The env-only reader (:func:`embedding_dim_env`) must not disagree either."""
    from mnemos.core.config import embedding_dim_env
    from mnemos.runtime.embedder import known_model_width

    monkeypatch.delenv("MNEMOS_EMBEDDING_DIM", raising=False)
    monkeypatch.delenv("PG_EMBEDDING_DIM", raising=False)
    monkeypatch.delenv("MNEMOS_EMBED_HTTP_MODEL", raising=False)
    assert embedding_dim_env() == known_model_width() == 1024


@pytest.mark.parametrize(
    "model",
    ["bge-m3", "BAAI/bge-m3", "/models/bge-m3.gguf", "bge-m3-Q8_0.gguf", "bge-m3:latest"],
)
def test_known_model_width_recognises_every_bge_m3_spelling(model: str):
    """The fleet writes bge-m3 four different ways; each is 1024-dimensional."""
    from mnemos.runtime.embedder import known_model_width

    assert known_model_width(model) == 1024


@pytest.mark.parametrize(
    ("model", "width"),
    [("nomic-embed-text", 768), ("BAAI/bge-base-en-v1.5", 768), ("bge-small-zh-v1.5", 512)],
)
def test_known_model_width_keeps_the_other_known_models(model: str, width: int):
    from mnemos.runtime.embedder import known_model_width

    assert known_model_width(model) == width


def test_known_model_width_is_none_for_an_unknown_model():
    from mnemos.runtime.embedder import known_model_width

    assert known_model_width("some-operator-local-model") is None
    assert known_model_width("") is None


# ─── 2. the resolved identity ───────────────────────────────────────────────


def test_embedding_identity_uses_the_http_model_and_its_width():
    """The identity is (model name, width) from the same source, never mixed."""
    from mnemos.runtime.embedder import embedding_identity

    identity = embedding_identity(http_model="bge-m3", configured_dim=1024)
    assert identity.model == "bge-m3"
    assert identity.dim == 1024
    assert identity.source == "http_model"


def test_embedding_identity_never_claims_the_configured_width_for_a_known_model():
    """A configured width the model cannot produce is reported as the model's."""
    from mnemos.runtime.embedder import embedding_identity

    identity = embedding_identity(http_model="bge-m3", configured_dim=768)
    assert identity.dim == 1024
    assert identity.source == "http_model"


def test_embedding_identity_from_settings_reads_the_live_settings():
    from mnemos.core.config import Settings
    from mnemos.runtime.embedder import embedding_identity_from_settings

    identity = embedding_identity_from_settings(Settings())
    assert identity.dim == Settings().database.embedding_dim
    assert identity.model


# ─── 3. the startup cross-check ─────────────────────────────────────────────


async def test_contract_refuses_a_discovered_width_that_disagrees():
    from mnemos.runtime.embedder import check_embedding_contract

    with pytest.raises(RuntimeError) as excinfo:
        await check_embedding_contract(
            probe=lambda: _async_value(768),
            configured_dim=1024,
            model="bge-m3",
            column_dim=1024,
        )
    message = str(excinfo.value)
    assert "768" in message and "1024" in message
    assert "MNEMOS_EMBEDDING_DIM" in message
    # Actionable: names the knob and the way out.
    assert "re-embed" in message.lower() or "backfill" in message.lower()


async def test_contract_refuses_a_configured_width_the_embedder_contradicts():
    """Even when the column agrees with the config, the embedder is the truth."""
    from mnemos.runtime.embedder import check_embedding_contract

    with pytest.raises(RuntimeError):
        await check_embedding_contract(
            probe=lambda: _async_value(1024),
            configured_dim=768,
            model="bge-m3",
            column_dim=768,
        )


async def test_contract_refuses_a_stale_column_against_the_embedder():
    from mnemos.runtime.embedder import check_embedding_contract

    with pytest.raises(RuntimeError) as excinfo:
        await check_embedding_contract(
            probe=lambda: _async_value(1024),
            configured_dim=1024,
            model="bge-m3",
            column_dim=768,
        )
    assert "vector(768)" in str(excinfo.value) or "768" in str(excinfo.value)


async def test_contract_accepts_agreement():
    from mnemos.runtime.embedder import check_embedding_contract

    report = await check_embedding_contract(
        probe=lambda: _async_value(1024),
        configured_dim=1024,
        model="bge-m3",
        column_dim=1024,
    )
    assert report.ok is True
    assert report.embedder_dim == 1024
    assert report.configured_dim == 1024
    assert report.column_dim == 1024


async def test_contract_tolerates_an_unknown_probe():
    """A probe that could not answer is not evidence of a mismatch."""
    from mnemos.runtime.embedder import check_embedding_contract

    report = await check_embedding_contract(
        probe=lambda: _async_value(None),
        configured_dim=1024,
        model="bge-m3",
        column_dim=None,
    )
    assert report.ok is True
    assert report.embedder_dim is None


async def test_contract_tolerates_a_failed_probe_but_says_so(caplog):
    from mnemos.runtime.embedder import check_embedding_contract

    async def _boom():
        raise OSError("embedding endpoint unreachable")

    report = await check_embedding_contract(
        probe=_boom,
        configured_dim=1024,
        model="bge-m3",
        column_dim=1024,
    )
    assert report.ok is True
    assert report.embedder_dim is None
    assert "unreachable" in (report.probe_error or "")


async def test_contract_ignores_the_probe_when_disabled(monkeypatch):
    from mnemos.core import config as config_module
    from mnemos.runtime import embedder as embedder_module

    monkeypatch.setattr(config_module, "embedding_dim_check_env", lambda: "off", raising=False)
    called = []

    async def _probe():
        called.append(True)
        return 768

    report = await embedder_module.check_embedding_contract(
        probe=_probe,
        configured_dim=1024,
        model="bge-m3",
        column_dim=None,
    )
    assert report.ok is True
    assert not called


# ─── 4. backend integration (SQLite: column + real probe) ───────────────────


async def test_contract_reads_the_live_sqlite_column(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.runtime.embedder import check_embedding_contract

    backend = SqliteBackend(tmp_path / "f6.sqlite3", _settings(1024))
    await backend.open()
    try:
        report = await check_embedding_contract(
            probe=lambda: _async_value(1024),
            configured_dim=1024,
            model="bge-m3",
            backend=backend,
        )
        assert report.ok is True
        assert report.column_dim == 1024
    finally:
        await backend.close()


async def test_contract_raises_before_serving_on_a_mismatched_embedder(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.runtime.embedder import check_embedding_contract

    backend = SqliteBackend(tmp_path / "f6b.sqlite3", _settings(768))
    await backend.open()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await check_embedding_contract(
                probe=lambda: _async_value(1024),
                configured_dim=768,
                model="bge-m3",
                backend=backend,
            )
        message = str(excinfo.value)
        assert "1024" in message and "768" in message
    finally:
        await backend.close()


async def test_backend_embedding_column_dim_reports_the_live_width(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "f6c.sqlite3", _settings(512))
    await backend.open()
    try:
        width = await backend.embedding_column_dim()
        assert width == 512
    finally:
        await backend.close()


async def test_startup_check_hook_is_wired_and_fails_loud(tmp_path, monkeypatch):
    """The App-lifespan wiring helper refuses to serve a mismatched embedder."""
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.runtime.embedder import verify_startup_embedding_contract

    backend = SqliteBackend(tmp_path / "f6d.sqlite3", _settings(768))
    await backend.open()
    try:
        with pytest.raises(RuntimeError):
            await verify_startup_embedding_contract(
                backend,
                _settings(768),
                probe=lambda: _async_value(1024),
            )
    finally:
        await backend.close()


# ─── helpers ────────────────────────────────────────────────────────────────


async def _async_value(value):
    return value


def _settings(dim: int):
    from mnemos.core.config import Settings

    settings = Settings()
    # ``explicit_fields`` is what the fence decider and the migration sizer
    # read; assigning the field alone would leave it looking unconfigured.
    settings.database.embedding_dim = dim
    settings._explicit_fields = {"database": {"embedding_dim"}}
    return settings
