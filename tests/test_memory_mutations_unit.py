from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from fastapi import HTTPException

from mnemos.api import memory_mutations
from mnemos.core import ids
from mnemos.domain import deletion_fence
from mnemos.persistence import nats_events, worker_lifecycle


def test_public_helpers_are_reexported():
    assert memory_mutations.new_memory_id is ids.new_memory_id
    assert memory_mutations.safe_subject_segment is nats_events.safe_subject_segment
    assert memory_mutations.assert_no_active_deletion is deletion_fence.assert_no_active_deletion
    assert memory_mutations.assert_no_active_deletion_for_scopes is deletion_fence.assert_no_active_deletion_for_scopes


def test_new_memory_id_uses_timestamp_and_uuid(monkeypatch):
    monkeypatch.setattr(ids.time, "time", lambda: 1234.567)
    monkeypatch.setattr(ids.uuid, "uuid4", lambda: SimpleNamespace(hex="abcdef123456"))

    assert memory_mutations.new_memory_id() == "mem_1234567_abcdef"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "default"),
        ("", "default"),
        ("alpha.beta", "alpha_beta"),
        ("alpha_beta", "alpha%5Fbeta"),
        ("a%5Fb", "a%255Fb"),
        (".*> /é", "_%2A%3E%20%2F%C3%A9"),
        (42, "42"),
    ],
)
def test_safe_subject_segment(value, expected):
    assert memory_mutations.safe_subject_segment(value) == expected


def test_safe_subject_segment_custom_default():
    assert memory_mutations.safe_subject_segment("", default="fallback") == "fallback"
    assert memory_mutations.safe_subject_segment(None, default="a.b") == "a_b"


@pytest.mark.asyncio
async def test_deletion_fence_allows_scope_without_active_request(monkeypatch):
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(worker_lifecycle, "active_deletion_for_scope", lookup)
    backend = object()

    assert await memory_mutations.assert_no_active_deletion(backend, owner_id="alice", namespace=None) is None
    lookup.assert_awaited_once_with(backend, target_user_id="alice", target_namespace=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [{}, {"restore_by": "2026-12-01"}])
async def test_deletion_fence_rejects_active_request(monkeypatch, active):
    lookup = AsyncMock(return_value=active)
    monkeypatch.setattr(worker_lifecycle, "active_deletion_for_scope", lookup)
    backend = object()

    with pytest.raises(HTTPException) as exc:
        await memory_mutations.assert_no_active_deletion(backend, owner_id="alice", namespace="team")

    assert exc.value.status_code == 409
    assert "active deletion request" in exc.value.detail
    if active:
        assert "Restore deadline: 2026-12-01." in exc.value.detail
    else:
        assert "Restore deadline" not in exc.value.detail
    lookup.assert_awaited_once_with(backend, target_user_id="alice", target_namespace="team")


@pytest.mark.asyncio
async def test_deletion_fence_fails_closed_when_lookup_fails(monkeypatch):
    error = RuntimeError("lookup failed")
    monkeypatch.setattr(worker_lifecycle, "active_deletion_for_scope", AsyncMock(side_effect=error))

    with pytest.raises(deletion_fence.DeletionFenceUnavailable) as exc:
        await memory_mutations.assert_no_active_deletion(object(), owner_id="alice", namespace="team")

    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "1"}
    assert exc.value.__cause__ is error


@pytest.mark.asyncio
async def test_deletion_fence_checks_distinct_sorted_scopes_and_skips(monkeypatch):
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(worker_lifecycle, "active_deletion_for_scope", lookup)
    backend = object()
    scopes = [("bob", "z"), ("alice", "b"), ("alice", None), ("bob", "z"), ("alice", "a")]

    await memory_mutations.assert_no_active_deletion_for_scopes(backend, scopes, skip=[("alice", "a")])

    assert lookup.await_args_list == [
        call(backend, target_user_id="alice", target_namespace=None),
        call(backend, target_user_id="alice", target_namespace="b"),
        call(backend, target_user_id="bob", target_namespace="z"),
    ]


@pytest.mark.asyncio
async def test_deletion_fence_stops_on_first_blocked_scope(monkeypatch):
    lookup = AsyncMock(side_effect=[None, {"restore_by": None}])
    monkeypatch.setattr(worker_lifecycle, "active_deletion_for_scope", lookup)
    backend = object()

    with pytest.raises(HTTPException) as exc:
        await memory_mutations.assert_no_active_deletion_for_scopes(
            backend, [("charlie", "n"), ("bob", "n"), ("alice", "n")]
        )

    assert exc.value.status_code == 409
    assert lookup.await_args_list == [
        call(backend, target_user_id="alice", target_namespace="n"),
        call(backend, target_user_id="bob", target_namespace="n"),
    ]
