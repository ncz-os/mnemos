"""Live OAuth persistence, restart and replay checks across every backend."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.datastructures import FormData

from mnemos.mcp import oauth as mcp_oauth
from tests.oauth_backend_helpers import oauth_database as oauth_database


class _FormPostRequest:
    """Minimal request stub for authorize_post with form data."""

    def __init__(self, data: dict[str, str], query: dict[str, str] | None = None):
        self._data = data
        self.query_params = query or {}

    async def form(self):
        return FormData(list(self._data.items()))


def _decode_json_response(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


async def _build_service_from_backend(
    backend,
    *,
    admin_passphrase: str,
    registration_secret: str,
) -> tuple[mcp_oauth.OAuthService, str]:
    """Build an ``OAuthService`` from the configured persistence backend."""
    store = mcp_oauth.PersistenceOAuthStore(backend)
    signing_key = os.environ.get("MNEMOS_OAUTH_SIGNING_KEY", "").strip() or await store.get_signing_key()

    if not signing_key:
        signing_key = secrets.token_urlsafe(32)
        await store.save_signing_key(key_id="default", signing_key=signing_key)
        signing_key = await store.get_signing_key()
        assert signing_key

    service = mcp_oauth.OAuthService(
        base_url="http://testserver",
        signing_key=signing_key,
        store=store,
        registration_secret=registration_secret,
        admin_passphrase=admin_passphrase,
    )
    return service, signing_key


async def _issue_client_and_tokens(
    service: mcp_oauth.OAuthService,
    *,
    admin_passphrase: str,
) -> tuple[str, str, str]:
    """Register a client and run authorize+token to get both tokens."""
    reg = await service.register({"redirect_uris": ["https://client.example/cb"]})
    client_id = reg["client_id"]

    verifier = secrets.token_urlsafe(32)
    code_response = await service.authorize_post(
        _FormPostRequest(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://client.example/cb",
                "code_challenge": mcp_oauth.pkce_s256(verifier),
                "code_challenge_method": "S256",
                "state": "restart-live",
                "passphrase": admin_passphrase,
            }
        )
    )

    assert code_response.status_code == 303, (
        f"authorize_post failed: status={code_response.status_code} body={code_response.body!r}"
    )

    code = parse_qs(urlparse(code_response.headers["location"]).query)["code"][0]
    token_response = await service.token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": "https://client.example/cb",
            "code_verifier": verifier,
        }
    )

    assert token_response.status_code == 200, (
        f"token exchange failed: status={token_response.status_code} body={token_response.body!r}"
    )
    token_payload = _decode_json_response(token_response)
    return client_id, token_payload["access_token"], token_payload["refresh_token"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backend_open_provisions_oauth_store(oauth_database):
    backend = await oauth_database.open()
    store = mcp_oauth.PersistenceOAuthStore(backend)
    await store.save_signing_key(key_id="default", signing_key=secrets.token_urlsafe(32))
    assert await store.get_signing_key()
    assert await store.get_client("missing-client") is None
    assert await store.consume_code("missing-code") is None
    assert await store.rotate_refresh("missing-hash", "missing-client", {}) == "invalid"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oauth_client_and_tokens_persist_across_backend_restart(oauth_database, monkeypatch):
    monkeypatch.delenv("MNEMOS_OAUTH_SIGNING_KEY", raising=False)
    admin_passphrase = "live-passphrase-do-not-share"
    kwargs = dict(admin_passphrase=admin_passphrase, registration_secret="live-registration-secret")
    backend = await oauth_database.open()
    service_1, key_1 = await _build_service_from_backend(backend, **kwargs)
    client_id, access_token, refresh_token = await _issue_client_and_tokens(
        service_1, admin_passphrase=admin_passphrase
    )
    await oauth_database.close(backend)
    backend = await oauth_database.open()
    service_2, key_2 = await _build_service_from_backend(backend, **kwargs)
    assert key_1 == key_2
    assert (await service_2.store.get_client(client_id))["client_id"] == client_id
    assert service_2.validate_access_token(access_token)["client_id"] == client_id
    # Two independently opened backends exercise cross-connection serialization.
    other = await oauth_database.open()
    service_3, key_3 = await _build_service_from_backend(other, **kwargs)
    assert key_3 == key_1
    form = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token}
    responses = await asyncio.gather(service_2.token(form), service_3.token(form))
    assert sorted(response.status_code for response in responses) == [200, 400]
    winner = next(response for response in responses if response.status_code == 200)
    successor = _decode_json_response(winner)["refresh_token"]
    rejected = await service_3.token({**form, "refresh_token": successor})
    assert rejected.status_code == 400, "replay must revoke the active successor family"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_stale_ancestor_race_retains_family_lock(oauth_database):
    if oauth_database.kind != "postgres":
        pytest.skip("PostgreSQL-specific deterministic pg_sleep trigger")
    backend = await oauth_database.open()
    admin_passphrase = "live-passphrase-do-not-share"
    service_2, _key = await _build_service_from_backend(
        backend, admin_passphrase=admin_passphrase, registration_secret="test-registration"
    )
    # Race replay of a stale ancestor against rotation of the current
    # member. A trigger holds the legitimate transaction during successor
    # insertion, deterministically exercising family-wide serialization.
    race_client, _race_access, ancestor = await _issue_client_and_tokens(
        service_2,
        admin_passphrase=admin_passphrase,
    )
    first_rotation = await service_2.token(
        {
            "grant_type": "refresh_token",
            "client_id": race_client,
            "refresh_token": ancestor,
        }
    )
    assert first_rotation.status_code == 200
    current = _decode_json_response(first_rotation)["refresh_token"]

    await oauth_database.admin.execute(f"""
        CREATE OR REPLACE FUNCTION {oauth_database.schema}.pause_refresh_successor()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.parent_jti IS NOT NULL THEN
                PERFORM pg_sleep(0.25);
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER pause_refresh_successor
        BEFORE INSERT ON {oauth_database.schema}.oauth_mcp_tokens
        FOR EACH ROW EXECUTE FUNCTION {oauth_database.schema}.pause_refresh_successor();
    """)

    rotate_current = asyncio.create_task(
        service_2.token(
            {
                "grant_type": "refresh_token",
                "client_id": race_client,
                "refresh_token": current,
            }
        )
    )
    await asyncio.sleep(0.05)
    replay_ancestor = asyncio.create_task(
        service_2.token(
            {
                "grant_type": "refresh_token",
                "client_id": race_client,
                "refresh_token": ancestor,
            }
        )
    )
    rotate_response, replay_response = await asyncio.gather(
        rotate_current,
        replay_ancestor,
    )
    assert rotate_response.status_code == 200
    assert replay_response.status_code == 400
    raced_successor = _decode_json_response(rotate_response)["refresh_token"]
    raced_successor_rejected = await service_2.token(
        {
            "grant_type": "refresh_token",
            "client_id": race_client,
            "refresh_token": raced_successor,
        }
    )
    assert raced_successor_rejected.status_code == 400, (
        "stale-ancestor replay must revoke a concurrently inserted successor"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signing_key_first_writer_wins_across_connections(oauth_database):
    first = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    second = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    existing = await first.get_signing_key()
    keys = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    await asyncio.gather(
        first.save_signing_key(key_id="default", signing_key=keys[0]),
        second.save_signing_key(key_id="default", signing_key=keys[1]),
    )
    winner = await first.get_signing_key()
    assert winner in ([existing] if existing else keys)
    assert await second.get_signing_key() == winner


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authorization_code_is_single_use_across_connections(oauth_database):
    first = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    second = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    client_id = secrets.token_urlsafe(24)
    await first.save_client(
        {
            "client_id": client_id,
            "client_secret": None,
            "redirect_uris": ["https://client.example/cb"],
            "token_endpoint_auth_method": "none",
        }
    )
    code = secrets.token_urlsafe(32)
    await first.save_code(
        {
            "code": code,
            "client_id": client_id,
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
            "redirect_uri": "https://client.example/cb",
            "expires_at": mcp_oauth._now() + timedelta(minutes=5),
            "used_at": None,
        }
    )
    await oauth_database.close(first.backend)
    await oauth_database.close(second.backend)
    first = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    second = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    consumed = await asyncio.gather(first.consume_code(code), second.consume_code(code))
    assert sum(row is not None for row in consumed) == 1
    assert await first.consume_code(code) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_refresh_insert_rolls_back_consumption(oauth_database):
    store = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    client_id = secrets.token_urlsafe(24)
    await store.save_client(
        {
            "client_id": client_id,
            "client_secret": None,
            "redirect_uris": ["https://client.example/cb"],
            "token_endpoint_auth_method": "none",
        }
    )
    root = {
        "jti": secrets.token_urlsafe(24),
        "refresh_token_hash": secrets.token_hex(32),
        "client_id": client_id,
        "expires_at": mcp_oauth._now() + timedelta(days=1),
        "parent_jti": None,
        "revoked_at": None,
        "replaced_by_jti": None,
    }
    root["family_id"] = root["jti"]
    await store.save_token(root)
    duplicate = {**root, "refresh_token_hash": secrets.token_hex(32)}
    with pytest.raises(Exception) as error:
        await store.rotate_refresh(root["refresh_token_hash"], client_id, duplicate)
    assert any(marker in str(error.value).lower() for marker in ("unique", "duplicate", "constraint", "23505"))
    valid = {**root, "jti": secrets.token_urlsafe(24), "refresh_token_hash": secrets.token_hex(32)}
    assert await store.rotate_refresh(root["refresh_token_hash"], client_id, valid) == "rotated"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_codes_and_refresh_tokens_are_rejected(oauth_database):
    store = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    client_id = secrets.token_urlsafe(24)
    await store.save_client(
        {
            "client_id": client_id,
            "client_secret": None,
            "redirect_uris": ["https://client.example/cb"],
            "token_endpoint_auth_method": "none",
        }
    )
    expired = mcp_oauth._now() - timedelta(seconds=10)
    code = secrets.token_urlsafe(32)
    await store.save_code(
        {
            "code": code,
            "client_id": client_id,
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
            "redirect_uri": "https://client.example/cb",
            "expires_at": expired,
            "used_at": None,
        }
    )
    assert await store.consume_code(code) is None
    root = {
        "jti": secrets.token_urlsafe(24),
        "refresh_token_hash": secrets.token_hex(32),
        "client_id": client_id,
        "expires_at": expired,
        "parent_jti": None,
        "revoked_at": None,
        "replaced_by_jti": None,
    }
    root["family_id"] = root["jti"]
    await store.save_token(root)
    successor = {
        **root,
        "jti": secrets.token_urlsafe(24),
        "refresh_token_hash": secrets.token_hex(32),
        "expires_at": mcp_oauth._now() + timedelta(days=1),
    }
    assert await store.rotate_refresh(root["refresh_token_hash"], client_id, successor) == "invalid"
    # A different client cannot consume an otherwise valid refresh token.
    root = {
        **root,
        "jti": secrets.token_urlsafe(24),
        "refresh_token_hash": secrets.token_hex(32),
        "expires_at": mcp_oauth._now() + timedelta(days=1),
    }
    root["family_id"] = root["jti"]
    await store.save_token(root)
    assert await store.rotate_refresh(root["refresh_token_hash"], "wrong-client", successor) == "invalid"
    assert await store.rotate_refresh(root["refresh_token_hash"], client_id, successor) == "rotated"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_persisted_oauth_identifiers_require_exact_matching(oauth_database):
    """Database collation must not weaken opaque OAuth identifier matching."""
    store = mcp_oauth.PersistenceOAuthStore(await oauth_database.open())
    client_id = "Client-" + secrets.token_urlsafe(24)
    await store.save_client(
        {
            "client_id": client_id,
            "client_secret": None,
            "redirect_uris": ["https://client.example/cb"],
            "token_endpoint_auth_method": "none",
        }
    )
    assert await store.get_client(client_id + " ") is None
    assert await store.get_client(client_id.swapcase()) is None
    assert (await store.get_client(client_id))["client_id"] == client_id

    code = "Code-" + secrets.token_urlsafe(32)
    await store.save_code(
        {
            "code": code,
            "client_id": client_id,
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
            "redirect_uri": "https://client.example/cb",
            "expires_at": mcp_oauth._now() + timedelta(minutes=5),
            "used_at": None,
        }
    )
    assert await store.consume_code(code + " ") is None
    assert await store.consume_code(code.swapcase()) is None
    assert (await store.consume_code(code))["client_id"] == client_id
    assert await store.consume_code(code) is None

    root = {
        "jti": secrets.token_urlsafe(24),
        "refresh_token_hash": secrets.token_hex(32),
        "client_id": client_id,
        "expires_at": mcp_oauth._now() + timedelta(days=1),
        "parent_jti": None,
        "revoked_at": None,
        "replaced_by_jti": None,
    }
    root["family_id"] = root["jti"]
    await store.save_token(root)
    successor = {**root, "jti": secrets.token_urlsafe(24), "refresh_token_hash": secrets.token_hex(32)}
    assert await store.rotate_refresh(root["refresh_token_hash"] + " ", client_id, successor) == "invalid"
    assert await store.rotate_refresh(root["refresh_token_hash"], client_id + " ", successor) == "invalid"
    assert await store.rotate_refresh(root["refresh_token_hash"], client_id.swapcase(), successor) == "invalid"
    assert await store.rotate_refresh(root["refresh_token_hash"], client_id, successor) == "rotated"
