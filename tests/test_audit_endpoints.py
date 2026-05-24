"""Unit tests for v6.2 M-2.2.1 /v1/audit endpoints."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    monkeypatch.setenv(
        "MNEMOS_AUDIT_ROOT_PRIVKEY",
        base64.b64encode(b"\x42" * 32).decode(),
    )


@pytest.fixture
def fake_user():
    from mnemos.api.dependencies import UserContext

    return UserContext(
        user_id="user-1",
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


def test_audit_pubkey_root_only(monkeypatch, fake_user):
    """Smoke test the route handler in isolation (no FastAPI client)."""
    from mnemos.api.routes import audit as audit_route

    monkeypatch.setattr(
        audit_route,
        "audit_chain_enabled",
        lambda: True,
    )

    import asyncio

    result = asyncio.get_event_loop().run_until_complete(audit_route.audit_pubkey(writer_id=None, user=fake_user))
    assert "root_pubkey" in result
    assert result["algorithm"] == "Ed25519"
    # base64 of 32 bytes = 44 chars
    assert len(result["root_pubkey"]) == 44
    assert "writer_pubkey" not in result


def test_audit_pubkey_with_writer(monkeypatch, fake_user):
    from mnemos.api.routes import audit as audit_route

    monkeypatch.setattr(audit_route, "audit_chain_enabled", lambda: True)

    class _Server:
        session_secret = "test-secret-32-bytes-long-padding-padding"

    class _Settings:
        server = _Server()

    monkeypatch.setattr(
        "mnemos.core.config.get_settings",
        lambda: _Settings(),
    )

    import asyncio

    result = asyncio.get_event_loop().run_until_complete(audit_route.audit_pubkey(writer_id="alice", user=fake_user))
    assert result["writer_id"] == "alice"
    assert "writer_pubkey" in result
    # Deterministic: same writer_id -> same pubkey
    result2 = asyncio.get_event_loop().run_until_complete(audit_route.audit_pubkey(writer_id="alice", user=fake_user))
    assert result["writer_pubkey"] == result2["writer_pubkey"]


def test_audit_pubkey_disabled_when_chain_off(monkeypatch, fake_user):
    from fastapi import HTTPException

    from mnemos.api.routes import audit as audit_route

    monkeypatch.setattr(audit_route, "audit_chain_enabled", lambda: False)

    import asyncio

    with pytest.raises(HTTPException) as exc:
        asyncio.get_event_loop().run_until_complete(audit_route.audit_pubkey(writer_id=None, user=fake_user))
    assert exc.value.status_code == 503


def test_audit_proof_head_404_when_no_entry(monkeypatch, fake_user):
    from fastapi import HTTPException

    from mnemos.api.routes import audit as audit_route

    monkeypatch.setattr(audit_route, "audit_chain_enabled", lambda: True)

    class _AuditChain:
        async def get_latest_audit_entry(self, tx, mid):
            return None

    class _TxCtx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    class _Backend:
        audit_chain = _AuditChain()

        def transactional(self):
            return _TxCtx()

    monkeypatch.setattr(
        "mnemos.api.routes.audit._backend_or_503",
        lambda: _Backend(),
    )

    import asyncio

    with pytest.raises(HTTPException) as exc:
        asyncio.get_event_loop().run_until_complete(
            audit_route.audit_proof_head(
                memory_id_str="mem_no_such",
                user=fake_user,
            )
        )
    assert exc.value.status_code == 404
