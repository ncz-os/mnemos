"""Active-deletion fence prevents writes during the 30-day grace window.

The fence in ``_assert_no_active_deletion`` blocks new writes onto a
(user, namespace) scope while a deletion request is in the
``sweep_verifying`` or ``soft_deleted`` state. Without this fence a
memory written on day 7 of grace would not be soft-deleted by the next
sweep, and the subsequent hard-delete (which only touches rows already
carrying ``deleted_at IS NOT NULL``) would skip it, leaving a "ghost"
memory after the audit log marks the request ``hard_deleted``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_assert_no_active_deletion_rejects_during_grace_window():
    """When an active deletion exists for the (user, namespace) scope,
    ``_assert_no_active_deletion`` must raise 409 and refuse the write.
    """
    from mnemos.api.routes.memories import _assert_no_active_deletion
    from fastapi import HTTPException

    backend = SimpleNamespace()
    active_row = {
        "id": "req-1",
        "status": "soft_deleted",
        "restore_by": datetime.now(timezone.utc) + timedelta(days=20),
    }

    fake_active_deletion = AsyncMock(return_value=active_row)
    with patch(
        "mnemos.persistence.worker_lifecycle.active_deletion_for_scope",
        fake_active_deletion,
    ):
        with pytest.raises(HTTPException) as exc:
            await _assert_no_active_deletion(
                backend, owner_id="u1", namespace="ns1"
            )
    assert exc.value.status_code == 409
    assert "active deletion request" in exc.value.detail


@pytest.mark.asyncio
async def test_assert_no_active_deletion_passes_when_no_active_request():
    from mnemos.api.routes.memories import _assert_no_active_deletion

    backend = SimpleNamespace()
    fake_active_deletion = AsyncMock(return_value=None)
    with patch(
        "mnemos.persistence.worker_lifecycle.active_deletion_for_scope",
        fake_active_deletion,
    ):
        await _assert_no_active_deletion(backend, owner_id="u1", namespace="ns1")
    fake_active_deletion.assert_awaited_once()


@pytest.mark.asyncio
async def test_assert_no_active_deletion_swallows_backend_errors():
    """If the fence probe itself errors (e.g. transient pool hiccup),
    the write is allowed and the worker's resweep+verify loop is the
    second line of defence — it refuses to mark ``hard_deleted`` on
    any live row.
    """
    from mnemos.api.routes.memories import _assert_no_active_deletion

    backend = SimpleNamespace()
    fake_active_deletion = AsyncMock(side_effect=RuntimeError("boom"))
    with patch(
        "mnemos.persistence.worker_lifecycle.active_deletion_for_scope",
        fake_active_deletion,
    ):
        await _assert_no_active_deletion(backend, owner_id="u1", namespace="ns1")
