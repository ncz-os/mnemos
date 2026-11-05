"""A peer sync interval below the database floor is a validation error, not a 500."""

import pytest
from pydantic import ValidationError

from mnemos.domain.models import FederationPeerCreateRequest, FederationPeerUpdateRequest


def _create(**kw):
    return FederationPeerCreateRequest(name="peer-one", base_url="http://peer.example:5002", auth_token="t", **kw)


@pytest.mark.parametrize("secs", [0, 1, 10, 29])
def test_create_rejects_an_interval_below_the_database_floor(secs):
    with pytest.raises(ValidationError):
        _create(sync_interval_secs=secs)


@pytest.mark.parametrize("secs", [30, 60, 300])
def test_create_accepts_the_floor_and_above(secs):
    assert _create(sync_interval_secs=secs).sync_interval_secs == secs


def test_create_default_interval_is_unchanged():
    assert _create().sync_interval_secs == 300


@pytest.mark.parametrize("secs", [0, 10, 29])
def test_update_rejects_an_interval_below_the_database_floor(secs):
    with pytest.raises(ValidationError):
        FederationPeerUpdateRequest(sync_interval_secs=secs)


def test_update_accepts_the_floor_and_none():
    assert FederationPeerUpdateRequest(sync_interval_secs=30).sync_interval_secs == 30
    assert FederationPeerUpdateRequest().sync_interval_secs is None
