"""Unit and integration tests for the MCP OAuth 2.1 authorization server.

The unit tests exercise JWT issuance/validation, PKCE enforcement, and
the single-tenant flow against the in-memory store.  A live PG integration
test is skipped when the host environment cannot reach Postgres, so the
suite is stable on the bare CI runner that ``make test`` uses.
"""

from __future__ import annotations

import asyncio
import json as _json
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import pytest
from starlette.requests import Request

from mnemos.mcp import oauth as mcp_oauth


def _build_service(**overrides):
    base = overrides.pop("base_url", "http://testserver")
    store = overrides.pop("store", mcp_oauth.InMemoryOAuthStore())
    signing_key = overrides.get("signing_key", "test-signing-key-do-not-ship")
    admin_passphrase = overrides.get("admin_passphrase", "test-passphrase")
    registration_secret = overrides.get("registration_secret", "test-reg-secret")
    return mcp_oauth.OAuthService(
        base_url=base,
        signing_key=signing_key,
        store=store,
        registration_secret=registration_secret,
        admin_passphrase=admin_passphrase,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_jwt_round_trip_and_expiry():
    service = _build_service()
    token = service.issue_access_token(client_id="client-1", provider="chatgpt")
    claims = service.validate_access_token(token)
    assert claims["sub"] == mcp_oauth.ADMIN_SUBJECT
    assert claims["client_id"] == "client-1"
    assert claims["provider"] == "chatgpt"
    assert claims["scope"] == "mcp"
    assert claims["iss"] == service.base_url
    assert claims["aud"] == "mnemos-mcp"


def test_jwt_signature_is_verified():
    service = _build_service(signing_key="real-key")
    token = service.issue_access_token(client_id="client-x")
    other = mcp_oauth.OAuthService(
        base_url="http://testserver",
        signing_key="attacker-key",
        store=mcp_oauth.InMemoryOAuthStore(),
        registration_secret="r",
        admin_passphrase="a",
    )
    with pytest.raises(jwt.PyJWTError):
        other.validate_access_token(token)
    # Also ensure a tampered token (different signature) is rejected.
    bad = token[:-2] + ("AB" if not token.endswith("AB") else "CD")
    with pytest.raises(jwt.PyJWTError):
        service.validate_access_token(bad)


def test_jwt_expiry_is_actually_enforced():
    service = _build_service()
    expired = service.issue_access_token(client_id="client-1", lifetime=-5)
    with pytest.raises(jwt.PyJWTError):
        service.validate_access_token(expired)


def test_postgres_store_decodes_jsonb_redirect_uris_for_exact_matching():
    class _Acquire:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def fetchrow(self, *_args):
            return {
                "client_id": "client-1",
                "client_secret": None,
                "redirect_uris": '["https://client.example/callback"]',
                "token_endpoint_auth_method": "none",
            }

    class _Pool:
        def acquire(self):
            return _Acquire()

    client = _run(mcp_oauth.PostgresOAuthStore(_Pool()).get_client("client-1"))
    assert client is not None
    assert client["redirect_uris"] == ["https://client.example/callback"]
    assert "https://client.example" not in client["redirect_uris"]


def test_pkce_s256_helper_matches_rfc7636_example():
    # RFC 7636 §4.6 reference vector.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert mcp_oauth.pkce_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_authorization_code_flow_issues_refreshable_jwt():
    service = _build_service()
    reg = _run(service.register({"redirect_uris": ["https://client.example/cb"]}))
    cid = reg["client_id"]
    verifier = "verifier-32-bytes-1234567890abcdef"
    # Authorize step renders the approval form (no passphrase in GET).
    request = _request(
        {
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": "https://client.example/cb",
            "code_challenge": mcp_oauth.pkce_s256(verifier),
            "code_challenge_method": "S256",
            "state": "abc",
        }
    )
    response = _run(service.authorize(request))
    assert response.status_code == 200
    # Step 2: POST the passphrase in the form body.
    approval = _form_post(
        {
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": "https://client.example/cb",
            "code_challenge": mcp_oauth.pkce_s256(verifier),
            "code_challenge_method": "S256",
            "state": "abc",
            "passphrase": "test-passphrase",
        }
    )
    code_response = _run(service.authorize_post(approval))
    assert code_response.status_code == 303
    location = code_response.headers["location"]
    assert "code=" in location and "state=abc" in location
    code = parse_qs(urlparse(location).query)["code"][0]
    token_response = _run(
        service.token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_verifier": verifier,
            }
        )
    )
    assert token_response.status_code == 200
    assert token_response.headers["cache-control"] == "no-store"
    assert token_response.headers["pragma"] == "no-cache"
    parsed = _json.loads(token_response.body.decode("utf-8"))
    access = parsed["access_token"]
    refresh = parsed["refresh_token"]
    decoded = service.validate_access_token(access)
    assert decoded["client_id"] == cid
    refreshed = _run(service.token({"grant_type": "refresh_token", "client_id": cid, "refresh_token": refresh}))
    assert refreshed.status_code == 200
    assert refreshed.headers["cache-control"] == "no-store"
    parsed2 = _json.loads(refreshed.body.decode("utf-8"))
    assert parsed2["access_token"] != access


def test_refresh_rotation_is_atomic_and_replay_revokes_successor_family():
    service = _build_service()
    reg = _run(service.register({"redirect_uris": ["https://client.example/cb"]}))
    initial = _run(service._tokens(reg["client_id"]))
    refresh = _json.loads(initial.body)["refresh_token"]
    form = {
        "grant_type": "refresh_token",
        "client_id": reg["client_id"],
        "refresh_token": refresh,
    }

    async def race():
        return await asyncio.gather(service.token(form), service.token(form))

    responses = _run(race())
    assert sorted(response.status_code for response in responses) == [200, 400]
    winner = next(response for response in responses if response.status_code == 200)
    successor = _json.loads(winner.body)["refresh_token"]
    rejected = _run(
        service.token(
            {
                "grant_type": "refresh_token",
                "client_id": reg["client_id"],
                "refresh_token": successor,
            }
        )
    )
    assert rejected.status_code == 400


def test_redirect_uri_query_is_preserved_and_fragment_is_rejected():
    service = _build_service()
    redirect_uri = "https://client.example/cb?tenant=one"
    reg = _run(service.register({"redirect_uris": [redirect_uri]}))
    approval = _form_post(
        {
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": redirect_uri,
            "code_challenge": mcp_oauth.pkce_s256("verifier"),
            "code_challenge_method": "S256",
            "state": "opaque",
            "passphrase": "test-passphrase",
        }
    )
    response = _run(service.authorize_post(approval))
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["tenant"] == ["one"]
    assert query["state"] == ["opaque"]
    assert len(query["code"]) == 1

    with pytest.raises(ValueError, match="fragments"):
        _run(
            service.register(
                {
                    "redirect_uris": ["https://client.example/cb#fragment"],
                }
            )
        )
    with pytest.raises(ValueError, match="absolute"):
        _run(service.register({"redirect_uris": ["not-an-absolute-uri"]}))


def test_pkce_is_enforced_not_optional():
    service = _build_service()
    reg = _run(service.register({"redirect_uris": ["https://client.example/cb"]}))
    cid = reg["client_id"]
    request = _request(
        {
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": "https://client.example/cb",
            "code_challenge": "abc",
        }
    )
    bad = _run(service.authorize(request))
    assert bad.status_code == 400
    # Step 2 via POST with passphrase.
    approval = _form_post(
        {
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": "https://client.example/cb",
            "code_challenge": mcp_oauth.pkce_s256("any"),
            "code_challenge_method": "S256",
            "passphrase": "test-passphrase",
        }
    )
    ok = _run(service.authorize_post(approval))
    assert ok.status_code == 303
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    bad_token = _run(
        service.token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_verifier": "WRONG",
            }
        )
    )
    assert bad_token.status_code == 400


def test_passphrase_is_required_and_never_logged(caplog):
    service = _build_service(admin_passphrase="topsecret-1234")
    reg = _run(service.register({"redirect_uris": ["https://client.example/cb"]}))
    cid = reg["client_id"]
    # GET (no passphrase) renders the approval form, which must not contain
    # the real passphrase anywhere.
    missing = _run(
        service.authorize(
            _request(
                {
                    "response_type": "code",
                    "client_id": cid,
                    "redirect_uri": "https://client.example/cb",
                    "code_challenge": mcp_oauth.pkce_s256("vvv"),
                    "code_challenge_method": "S256",
                }
            )
        )
    )
    assert missing.status_code == 200
    assert "topsecret-1234" not in missing.body.decode("utf-8")
    # Wrong passphrase via POST is rejected.
    wrong = _run(
        service.authorize_post(
            _form_post(
                {
                    "response_type": "code",
                    "client_id": cid,
                    "redirect_uri": "https://client.example/cb",
                    "code_challenge": mcp_oauth.pkce_s256("vvv"),
                    "code_challenge_method": "S256",
                    "passphrase": "wrong",
                }
            )
        )
    )
    assert wrong.status_code == 403
    caplog.clear()
    with caplog.at_level("DEBUG"):
        _run(
            service.authorize_post(
                _form_post(
                    {
                        "response_type": "code",
                        "client_id": cid,
                        "redirect_uri": "https://client.example/cb",
                        "code_challenge": mcp_oauth.pkce_s256("vvv"),
                        "code_challenge_method": "S256",
                        "passphrase": "topsecret-1234",
                    }
                )
            )
        )
    for record in caplog.records:
        assert "topsecret-1234" not in record.getMessage()


def test_query_string_passphrase_is_rejected():
    """Defense in depth: passphrase in the URL query string is refused
    even by the GET path. This prevents the secret from ending up in
    access logs, proxy logs, and browser history."""
    service = _build_service(admin_passphrase="topsecret-1234")
    reg = _run(service.register({"redirect_uris": ["https://client.example/cb"]}))
    cid = reg["client_id"]
    # GET with passphrase in the query string must 400.
    leaked = _run(
        service.authorize(
            _request(
                {
                    "response_type": "code",
                    "client_id": cid,
                    "redirect_uri": "https://client.example/cb",
                    "code_challenge": mcp_oauth.pkce_s256("vvv"),
                    "code_challenge_method": "S256",
                    "passphrase": "topsecret-1234",
                }
            )
        )
    )
    assert leaked.status_code == 400
    assert "topsecret-1234" not in leaked.body.decode("utf-8")
    # POST with passphrase in the query string must also 400, even if the
    # body is correct.
    leaked_post = _run(
        service.authorize_post(
            _form_post(
                {
                    "response_type": "code",
                    "client_id": cid,
                    "redirect_uri": "https://client.example/cb",
                    "code_challenge": mcp_oauth.pkce_s256("vvv"),
                    "code_challenge_method": "S256",
                    "passphrase": "topsecret-1234",
                },
                query={"passphrase": "topsecret-1234"},
            )
        )
    )
    assert leaked_post.status_code == 400
    assert "topsecret-1234" not in leaked_post.body.decode("utf-8")


def _request(params: dict):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/oauth/authorize",
        "query_string": urlencode(params).encode("ascii"),
    }
    return Request(scope)


class _FormPostRequest:
    """Hand-rolled Starlette request stub that mimics ``request.form()``
    for the authorize-post code path without depending on httpx."""

    def __init__(self, data: dict[str, str], query: dict[str, str] | None = None):
        self._data = data
        self.query_params = query or {}

    async def form(self):
        from starlette.datastructures import FormData

        # Return a FormData (real Starlette object) so the production
        # code's ``form.items()`` and ``form.multi_items()`` work.
        return FormData(list(self._data.items()))


def _form_post(data: dict[str, str], query: dict[str, str] | None = None):
    return _FormPostRequest(data, query=query)
