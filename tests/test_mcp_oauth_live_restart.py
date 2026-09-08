"""Live PostgreSQL persistence test for MCP OAuth service restarts.

The MCP server can persist OAuth clients, authorization codes, and token
artifacts only when `mcp/http.py` wires a real Postgres-backed
`OAuthService`. This test validates that behavior by restarting the
service object while keeping the same backing database tables.
"""
from __future__ import annotations

import json
import os
import secrets
import uuid
from urllib.parse import parse_qs, urlparse

import asyncpg
import pytest
from starlette.datastructures import FormData

from mnemos.mcp import oauth as mcp_oauth


def _resolve_live_oauth_database_url() -> tuple[str | None, str | None]:
    """Resolve the live DSN, with one explicit fallback.

    Preference is an explicitly set env var; if absent, read the operator
    password file from this host and build the canonical URL.
    """
    dsn = os.environ.get("MNEMOS_OAUTH_DATABASE_URL", "").strip()
    if dsn:
        return dsn, None

    pw_file = "/tmp/.mnemos_oauth_pw"
    if not os.path.exists(pw_file):
        return (
            None,
            "set MNEMOS_OAUTH_DATABASE_URL or expose /tmp/.mnemos_oauth_pw for fallback",
        )

    try:
        with open(pw_file, "r", encoding="utf-8") as f:
            password = f.read().strip()
    except OSError as exc:
        return (
            None,
            f"MNEMOS_OAUTH_DATABASE_URL is unset and /tmp/.mnemos_oauth_pw is not readable: {exc}",
        )

    if not password:
        return None, "/tmp/.mnemos_oauth_pw is present but empty"

    return (
        f"postgresql://mnemos_oauth_user:{password}@127.0.0.1:5432/mnemos_oauth",
        None,
    )


OAUTH_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.oauth_mcp_clients (
    client_id                    TEXT PRIMARY KEY,
    client_secret                TEXT,
    redirect_uris                JSONB NOT NULL,
    token_endpoint_auth_method   TEXT NOT NULL,
    created                      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {schema}.oauth_mcp_authorization_codes (
    code                     TEXT PRIMARY KEY,
    client_id                TEXT NOT NULL
        REFERENCES {schema}.oauth_mcp_clients(client_id) ON DELETE CASCADE,
    code_challenge           TEXT NOT NULL,
    code_challenge_method    TEXT NOT NULL,
    redirect_uri             TEXT NOT NULL,
    expires_at               TIMESTAMPTZ NOT NULL,
    used_at                  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS {schema}.oauth_mcp_tokens (
    jti                  TEXT PRIMARY KEY,
    refresh_token_hash   TEXT NOT NULL,
    client_id            TEXT NOT NULL
        REFERENCES {schema}.oauth_mcp_clients(client_id) ON DELETE CASCADE,
    expires_at           TIMESTAMPTZ NOT NULL,
    revoked_at           TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS {schema}.oauth_mcp_signing_keys (
    key_id        TEXT PRIMARY KEY,
    signing_key   TEXT NOT NULL,
    created       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rotated_at    TIMESTAMPTZ
);
"""


async def _create_schema(admin_conn: asyncpg.Connection, schema: str) -> None:
    await admin_conn.execute(OAUTH_SCHEMA_SQL.format(schema=schema))


async def _drop_schema(admin_conn: asyncpg.Connection, schema: str) -> None:
    await admin_conn.execute(f"DROP SCHEMA IF EXISTS \"{schema}\" CASCADE")


class _FormPostRequest:
    """Minimal request stub for authorize_post with form data."""

    def __init__(self, data: dict[str, str], query: dict[str, str] | None = None):
        self._data = data
        self.query_params = query or {}

    async def form(self):
        return FormData(list(self._data.items()))


def _decode_json_response(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


async def _build_service_from_pool(
    pool: asyncpg.Pool,
    *,
    admin_passphrase: str,
    registration_secret: str,
) -> tuple[mcp_oauth.OAuthService, str]:
    """Build an ``OAuthService`` from a live Postgres-backed store."""
    store = mcp_oauth.PostgresOAuthStore(pool)
    signing_key = os.environ.get("MNEMOS_OAUTH_SIGNING_KEY", "").strip() or await store.get_signing_key()

    if not signing_key:
        signing_key = secrets.token_urlsafe(32)
        await store.save_signing_key(key_id="default", signing_key=signing_key)

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
    code_response = await service.authorize_post(_FormPostRequest({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "https://client.example/cb",
        "code_challenge": mcp_oauth.pkce_s256(verifier),
        "code_challenge_method": "S256",
        "state": "restart-live",
        "passphrase": admin_passphrase,
    }))

    assert code_response.status_code == 303, (
        f"authorize_post failed: status={code_response.status_code} body={code_response.body!r}"
    )

    code = parse_qs(urlparse(code_response.headers["location"]).query)["code"][0]
    token_response = await service.token({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": "https://client.example/cb",
        "code_verifier": verifier,
    })

    assert token_response.status_code == 200, (
        f"token exchange failed: status={token_response.status_code} body={token_response.body!r}"
    )
    token_payload = _decode_json_response(token_response)
    return client_id, token_payload["access_token"], token_payload["refresh_token"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oauth_client_and_tokens_persist_across_postgres_service_restart():
    """A process-local service restart should not lose OAuth state."""
    dsn, reason = _resolve_live_oauth_database_url()
    if dsn is None:
        pytest.skip(f"Live MNEMOS OAuth DB not available: {reason}")

    original_signing_key = os.environ.pop("MNEMOS_OAUTH_SIGNING_KEY", None)
    admin_conn: asyncpg.Connection | None = None
    pool_1: asyncpg.Pool | None = None
    pool_2: asyncpg.Pool | None = None
    schema = f"oauth_live_restart_{uuid.uuid4().hex[:12]}"

    admin_passphrase = "live-passphrase-do-not-share"
    registration_secret = "live-registration-secret"

    try:
        try:
            admin_conn = await asyncpg.connect(dsn=dsn)
            await _create_schema(admin_conn, schema)
        except Exception as exc:  # pragma: no cover - env/network dependent
            pytest.skip(f"MNEMOS_OAUTH_DATABASE_URL is set but not reachable: {exc}")

        search_path = f"{schema},public"
        pool_1 = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=2,
            server_settings={"search_path": search_path},
        )
        pool_2 = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=2,
            server_settings={"search_path": search_path},
        )

        service_1, signing_key_1 = await _build_service_from_pool(
            pool_1,
            admin_passphrase=admin_passphrase,
            registration_secret=registration_secret,
        )
        client_id, access_token, refresh_token = await _issue_client_and_tokens(
            service_1,
            admin_passphrase=admin_passphrase,
        )

        service_2, signing_key_2 = await _build_service_from_pool(
            pool_2,
            admin_passphrase=admin_passphrase,
            registration_secret=registration_secret,
        )

        assert signing_key_2 == signing_key_1, (
            "second service must reuse the table-backed signing key when no env key is set"
        )

        claims = service_2.validate_access_token(access_token)
        assert claims["client_id"] == client_id

        refresh_response = await service_2.token({
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        })
        assert refresh_response.status_code == 200, (
            f"refresh failed with second service: {refresh_response.body!r}"
        )
        refreshed_payload = _decode_json_response(refresh_response)
        assert "access_token" in refreshed_payload
        assert refreshed_payload["access_token"] != access_token

        refreshed_claims = service_2.validate_access_token(refreshed_payload["access_token"])
        assert refreshed_claims["client_id"] == client_id
    finally:
        if pool_1 is not None:
            await pool_1.close()
        if pool_2 is not None:
            await pool_2.close()

        if admin_conn is not None:
            try:
                await _drop_schema(admin_conn, schema)
            finally:
                await admin_conn.close()

        if original_signing_key is None:
            os.environ.pop("MNEMOS_OAUTH_SIGNING_KEY", None)
        else:
            os.environ["MNEMOS_OAUTH_SIGNING_KEY"] = original_signing_key
