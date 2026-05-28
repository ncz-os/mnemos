"""Integration tests for v6.2 M-2.2.4 /v1/admin/category_decay endpoints."""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _audit_env(monkeypatch):
    monkeypatch.setenv(
        "MNEMOS_AUDIT_ROOT_PRIVKEY",
        base64.b64encode(b"\x42" * 32).decode(),
    )


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.domain.search.decay import invalidate_decay_cache

    invalidate_decay_cache()

    class _S:
        class database:
            embedding_dim = 1024

    backend = SqliteBackend(tmp_path / "decay-admin.db", _S())
    await backend.open()
    yield backend
    await backend.close()
    invalidate_decay_cache()


@pytest.fixture
def root_user():
    from mnemos.api.dependencies import UserContext

    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


@pytest.mark.asyncio
async def test_list_returns_seeded_table(sqlite_backend, root_user, monkeypatch):
    from mnemos.api.routes import admin_decay as ad

    monkeypatch.setattr(
        "mnemos.api.routes.admin_decay._backend_or_503",
        lambda: sqlite_backend,
    )
    rows = await ad.list_category_decay(_=root_user)
    cats = {r.category for r in rows}
    assert "feedback" in cats
    assert "credentials" in cats
    assert "(default)" in cats


@pytest.mark.asyncio
async def test_update_then_list_reflects(sqlite_backend, root_user, monkeypatch):
    from mnemos.api.routes import admin_decay as ad

    monkeypatch.setattr(
        "mnemos.api.routes.admin_decay._backend_or_503",
        lambda: sqlite_backend,
    )
    req = ad.DecayUpdateRequest(
        half_life_days=1000.0,
        decay_kind="exponential",
        floor=0.9,
    )
    resp = await ad.update_category_decay(
        category="feedback",
        request=req,
        _=root_user,
    )
    assert resp.half_life_days == 1000.0
    assert resp.floor == 0.9

    rows = await ad.list_category_decay(_=root_user)
    fb = next(r for r in rows if r.category == "feedback")
    assert fb.half_life_days == 1000.0
    assert fb.floor == 0.9


@pytest.mark.asyncio
async def test_reseed_resets_table(sqlite_backend, root_user, monkeypatch):
    from mnemos.api.routes import admin_decay as ad

    monkeypatch.setattr(
        "mnemos.api.routes.admin_decay._backend_or_503",
        lambda: sqlite_backend,
    )
    # Override feedback
    await ad.update_category_decay(
        category="feedback",
        request=ad.DecayUpdateRequest(
            half_life_days=9999.0,
            decay_kind="none",
            floor=0.0,
        ),
        _=root_user,
    )
    # Reseed
    rows = await ad.reseed_category_decay(_=root_user)
    fb = next(r for r in rows if r.category == "feedback")
    assert fb.half_life_days == 365  # back to default
    assert fb.floor == 0.5

    # Verify via list (cache invalidated)
    rows_listed = await ad.list_category_decay(_=root_user)
    fb2 = next(r for r in rows_listed if r.category == "feedback")
    assert fb2.half_life_days == 365


@pytest.mark.asyncio
async def test_update_rejects_overlong_category(sqlite_backend, root_user, monkeypatch):
    from fastapi import HTTPException

    from mnemos.api.routes import admin_decay as ad

    monkeypatch.setattr(
        "mnemos.api.routes.admin_decay._backend_or_503",
        lambda: sqlite_backend,
    )
    too_long = "x" * 65
    req = ad.DecayUpdateRequest(half_life_days=10, decay_kind="exponential", floor=0.0)
    with pytest.raises(HTTPException) as exc:
        await ad.update_category_decay(
            category=too_long,
            request=req,
            _=root_user,
        )
    assert exc.value.status_code == 400


def test_update_validator_rejects_bad_kind():
    """Pydantic rejects invalid decay_kind at parse time."""
    from pydantic import ValidationError

    from mnemos.api.routes.admin_decay import DecayUpdateRequest

    with pytest.raises(ValidationError):
        DecayUpdateRequest(
            half_life_days=100,
            decay_kind="bogus",
            floor=0.5,
        )


def test_update_validator_rejects_out_of_range_floor():
    from pydantic import ValidationError

    from mnemos.api.routes.admin_decay import DecayUpdateRequest

    with pytest.raises(ValidationError):
        DecayUpdateRequest(
            half_life_days=100,
            decay_kind="exponential",
            floor=1.5,  # > 1.0
        )


def test_update_validator_rejects_zero_half_life():
    from pydantic import ValidationError

    from mnemos.api.routes.admin_decay import DecayUpdateRequest

    with pytest.raises(ValidationError):
        DecayUpdateRequest(
            half_life_days=0,
            decay_kind="exponential",
            floor=0.0,
        )
