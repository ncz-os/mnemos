"""Characterisation tests for ``mnemos/api/routes/journal.py``.

These pin the module's CURRENT observable behaviour — not an ideal one.
If a later change alters an assertion here, that change is behavioural and
should be a deliberate decision, not a side effect.

Scope is the route module only (the persistence SQL lives in the backends and
is out of scope):

* ``JournalCreateRequest`` — required fields and the optional ``date``.
* ``create_journal_entry`` (``POST /journal``) — id minting, per-owner /
  per-namespace scoping, ``?owner_id=`` / ``?namespace=`` override policy,
  the ``date`` parsing branch, the 201 status, the error mapping.
* ``list_journal_entries`` (``GET /journal``) — the ``date`` query alias,
  the ``limit`` bound, filter forwarding, the ``{"entries", "count"}``
  envelope and the error mapping.
* ``delete_journal_entry`` (``DELETE /journal/{entry_id}``) — scoping,
  the 204 status and the 404-on-``False`` branch.
* ``backend_or_503`` behaviour as seen through all three routes.

Everything external is faked: no network, no live database, no clock and no
real ``uuid4`` (ids are pinned through a monkeypatched ``uuid.uuid4``). The
backends are small in-memory fakes that record the exact keyword arguments
the routes pass down, so the assertions are about the route contract, not
about any SQL dialect.

Two run modes, both exercised here:

* ``_run(...)`` / ``_call(...)`` — the handler coroutines are awaited
  directly, which is deterministic and independent of the installed FastAPI
  routing version.
* A real ``TestClient`` over a minimal app mounting the real router, with
  only ``get_current_user`` overridden, to pin the HTTP-level contract
  (status codes, the ``?date=`` alias, ``limit`` bounds, response bodies and
  the 422 produced by request validation). The full ``mnemos.api.main.app``
  is deliberately not booted: its lifespan builds a live persistence backend
  and refuses to start under the auth/bind guard.

One suspected defect is pinned as ``xfail(strict=True)`` — see
``test_delete_failure_is_not_wrapped_as_500``. ``DELETE /journal/{entry_id}``
is the only route in this module without the ``try/except`` that its two
siblings have, so a backend error escapes as an uncontrolled exception.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import get_current_user
from mnemos.api.routes import journal as jr
from mnemos.core.auth_context import UserContext

PINNED_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# ── fakes and fixtures ───────────────────────────────────────────────────


class FakeBackend:
    """In-memory stand-in for the journal slice of a PersistenceBackend.

    Every call is recorded in ``calls`` as ``(method, kwargs)`` with the
    route-supplied ``tx`` dropped, so a test can assert exactly which scope
    and filters the route forwarded. ``results`` / ``errors`` let a test
    install a return value or an exception per method.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.tx_count = 0
        self.results: dict[str, object] = {}
        self.errors: dict[str, BaseException] = {}

    def _resolve(self, method: str, default):
        if method in self.errors:
            raise self.errors[method]
        return self.results.get(method, default)

    async def create_journal_entry(self, tx, **kwargs):
        self.calls.append(("create_journal_entry", kwargs))
        return self._resolve("create_journal_entry", {"id": kwargs["entry_id"]})

    async def list_journal_entries(self, tx, **kwargs):
        self.calls.append(("list_journal_entries", kwargs))
        return self._resolve("list_journal_entries", [])

    async def delete_journal_entry(self, tx, **kwargs):
        self.calls.append(("delete_journal_entry", kwargs))
        return self._resolve("delete_journal_entry", True)

    def transactional(self):
        backend = self

        class _Tx:
            async def __aenter__(self):
                backend.tx_count += 1
                return SimpleNamespace(_fake=True)

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Tx()

    def only_call(self) -> tuple[str, dict]:
        assert len(self.calls) == 1, f"expected exactly one backend call, got {self.calls}"
        return self.calls[0]


def _user(user_id="alice", namespace="default", role="user") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role=role,
        namespace=namespace,
        authenticated=True,
    )


ROOT = UserContext(user_id="root-user", group_ids=[], role="root", namespace="default", authenticated=True)
ALICE = _user()


@pytest.fixture
def backend(monkeypatch):
    """Install an empty FakeBackend as the process-wide persistence backend."""
    fake = FakeBackend()
    monkeypatch.setattr(_lc, "_persistence_backend", fake, raising=False)
    return fake


@pytest.fixture
def pinned_uuid(monkeypatch):
    """Freeze ``uuid.uuid4`` so minted entry ids are deterministic."""

    class _Pinned:
        @staticmethod
        def uuid4():
            return PINNED_UUID

    monkeypatch.setattr(jr, "uuid", _Pinned)
    return str(PINNED_UUID)


def _run(coro):
    return asyncio.run(coro)


def _create(**kwargs):
    """Call the handler with the keyword args the route function declares."""
    return _run(jr.create_journal_entry(**kwargs))


def _list(**kwargs):
    return _run(jr.list_journal_entries(**kwargs))


def _delete(**kwargs):
    return _run(jr.delete_journal_entry(**kwargs))


# ── request model ────────────────────────────────────────────────────────


def test_request_model_requires_topic_and_content():
    with pytest.raises(ValidationError) as exc:
        jr.JournalCreateRequest(content="only content")
    assert "topic" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        jr.JournalCreateRequest(topic="only topic")
    assert "content" in str(exc.value)


def test_request_model_optional_fields_default_to_none():
    req = jr.JournalCreateRequest(topic="t", content="c")
    assert req.date is None
    assert req.metadata is None


@pytest.mark.parametrize("field", ["topic", "content", "date"])
def test_request_model_rejects_non_string_scalars(field):
    # Pydantic v2's default (non-strict) str validation still refuses ints;
    # the route never re-validates, so the model is the only gate.
    payload = {"topic": "t", "content": "c", field: 3}
    with pytest.raises(ValidationError) as exc:
        jr.JournalCreateRequest(**payload)
    assert field in str(exc.value)


def test_request_model_keeps_a_dict_of_metadata():
    req = jr.JournalCreateRequest(topic="t", content="c", metadata={"a": 1})
    assert req.metadata == {"a": 1}


def test_request_model_does_not_validate_the_date_string():
    # The date format check lives in the handler (422), not in the model.
    req = jr.JournalCreateRequest(topic="t", content="c", date="not-a-date")
    assert req.date == "not-a-date"


def test_router_metadata():
    assert jr.router.prefix == ""
    assert jr.router.tags == ["journal"]
    paths = {(r.path, tuple(sorted(r.methods))) for r in jr.router.routes}
    assert paths == {
        ("/journal", ("POST",)),
        ("/journal", ("GET",)),
        ("/journal/{entry_id}", ("DELETE",)),
    }


# ── POST /journal ────────────────────────────────────────────────────────


def test_create_scopes_to_caller_and_defaults_date_to_none(backend, pinned_uuid):
    backend.results["create_journal_entry"] = {"id": pinned_uuid, "topic": "t"}
    req = jr.JournalCreateRequest(topic="t", content="c")

    row = _create(req=req, user=ALICE, owner_id=None, namespace=None)

    assert row == {"id": pinned_uuid, "topic": "t"}
    method, kwargs = backend.only_call()
    assert method == "create_journal_entry"
    assert kwargs["entry_id"] == pinned_uuid
    assert kwargs["owner_id"] == "alice"
    assert kwargs["namespace"] == "default"
    assert kwargs["entry_date"] is None, "omitted date must reach the backend as None (SQL CURRENT_DATE)"
    assert kwargs["topic"] == "t"
    assert kwargs["content"] == "c"
    assert kwargs["metadata"] is None
    assert backend.tx_count == 1


def test_create_forwards_explicit_iso_date_as_date_object(backend, pinned_uuid):
    req = jr.JournalCreateRequest(topic="t", content="c", date="2026-11-05", metadata={"mood": "good"})

    _create(req=req, user=ALICE, owner_id=None, namespace=None)

    _, kwargs = backend.only_call()
    assert kwargs["entry_date"] == date(2026, 11, 5)
    assert isinstance(kwargs["entry_date"], date)
    assert kwargs["metadata"] == {"mood": "good"}


def test_create_rejects_malformed_date_before_touching_the_backend(backend, pinned_uuid):
    req = jr.JournalCreateRequest(topic="t", content="c", date="11/05/2026")

    with pytest.raises(HTTPException) as exc:
        _create(req=req, user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 422
    assert exc.value.detail == "Invalid date format; expected YYYY-MM-DD"
    assert backend.calls == [], "date validation must happen before any write"
    assert backend.tx_count == 0


@pytest.mark.parametrize("bad_date", ["2026-13-01", "2026-02-30", "", "2026-11-05T00:00:00"])
def test_create_rejects_iso_lookalikes(backend, pinned_uuid, bad_date):
    # "" is falsy, so it is treated as omitted rather than invalid — pinned
    # separately below; the others are genuinely rejected by fromisoformat.
    req = jr.JournalCreateRequest(topic="t", content="c", date=bad_date)
    if bad_date == "":
        _create(req=req, user=ALICE, owner_id=None, namespace=None)
        _, kwargs = backend.only_call()
        assert kwargs["entry_date"] is None
        return
    with pytest.raises(HTTPException) as exc:
        _create(req=req, user=ALICE, owner_id=None, namespace=None)
    assert exc.value.status_code == 422
    assert backend.calls == []


def test_create_empty_date_string_is_treated_as_omitted(backend, pinned_uuid):
    req = jr.JournalCreateRequest(topic="t", content="c", date="")

    _create(req=req, user=ALICE, owner_id=None, namespace=None)

    _, kwargs = backend.only_call()
    assert kwargs["entry_date"] is None


def test_create_root_may_target_another_owner_and_namespace(backend, pinned_uuid):
    req = jr.JournalCreateRequest(topic="t", content="c")

    _create(req=req, user=ROOT, owner_id="bob", namespace="vault")

    _, kwargs = backend.only_call()
    assert kwargs["owner_id"] == "bob"
    assert kwargs["namespace"] == "vault"


def test_create_non_root_override_is_403_and_writes_nothing(backend, pinned_uuid):
    req = jr.JournalCreateRequest(topic="t", content="c")

    with pytest.raises(HTTPException) as exc:
        _create(req=req, user=ALICE, owner_id="bob", namespace=None)

    assert exc.value.status_code == 403
    assert backend.calls == []


def test_create_self_owner_override_is_not_a_violation(backend, pinned_uuid):
    # scope_owner only rejects an override that DIFFERS from the caller.
    req = jr.JournalCreateRequest(topic="t", content="c")

    _create(req=req, user=ALICE, owner_id="alice", namespace="default")

    _, kwargs = backend.only_call()
    assert kwargs["owner_id"] == "alice"
    assert kwargs["namespace"] == "default"


def test_create_backend_failure_is_wrapped_as_500(backend, pinned_uuid):
    backend.errors["create_journal_entry"] = RuntimeError("disk on fire")
    req = jr.JournalCreateRequest(topic="t", content="c")

    with pytest.raises(HTTPException) as exc:
        _create(req=req, user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Internal server error"


def test_create_422_from_inside_the_try_is_passed_through_unchanged(backend, pinned_uuid):
    # The 422 raised inside the `try` must survive `except HTTPException: raise`
    # rather than be swallowed by the broad `except Exception`.
    req = jr.JournalCreateRequest(topic="t", content="c", date="nope")

    with pytest.raises(HTTPException) as exc:
        _create(req=req, user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 422


def test_create_empty_string_overrides_fall_back_to_the_callers_scope(backend, pinned_uuid):
    # An empty-string override is falsy, so scope_owner/scope_namespace fall
    # back to the caller's own scope instead of writing to "".
    req = jr.JournalCreateRequest(topic="t", content="c")

    _create(req=req, user=ROOT, owner_id="", namespace="")

    _, kwargs = backend.only_call()
    assert kwargs["owner_id"] == "root-user"
    assert kwargs["namespace"] == "default"


# ── GET /journal ─────────────────────────────────────────────────────────


def test_list_forwards_scope_and_filters_and_wraps_in_envelope(backend):
    rows = [{"id": "a", "topic": "t"}, {"id": "b", "topic": "t"}]
    backend.results["list_journal_entries"] = rows

    out = _list(
        topic="t",
        date_str="2026-11-05",
        search="needle",
        limit=7,
        user=ALICE,
        owner_id=None,
        namespace=None,
    )

    assert out == {"entries": rows, "count": 2}
    _, kwargs = backend.only_call()
    assert kwargs["owner_id"] == "alice"
    assert kwargs["namespace"] == "default"
    assert kwargs["entry_date"] == date(2026, 11, 5)
    assert kwargs["topic"] == "t"
    assert kwargs["search"] == "needle"
    assert kwargs["limit"] == 7


def test_list_empty_result_is_an_empty_envelope(backend):
    out = _list(topic=None, date_str=None, search=None, limit=20, user=ALICE, owner_id=None, namespace=None)
    assert out == {"entries": [], "count": 0}
    _, kwargs = backend.only_call()
    assert kwargs["entry_date"] is None
    assert kwargs["limit"] == 20


def test_list_rejects_malformed_date_before_touching_the_backend(backend):
    with pytest.raises(HTTPException) as exc:
        _list(topic=None, date_str="2026/11/05", search=None, limit=20, user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 422
    assert exc.value.detail == "Invalid date format; expected YYYY-MM-DD"
    assert backend.calls == []


def test_list_root_may_target_another_owner_and_namespace(backend):
    _list(topic=None, date_str=None, search=None, limit=20, user=ROOT, owner_id="bob", namespace="vault")

    _, kwargs = backend.only_call()
    assert kwargs["owner_id"] == "bob"
    assert kwargs["namespace"] == "vault"


def test_list_non_root_cross_namespace_override_is_403(backend):
    with pytest.raises(HTTPException) as exc:
        _list(topic=None, date_str=None, search=None, limit=20, user=ALICE, owner_id=None, namespace="vault")

    assert exc.value.status_code == 403
    assert exc.value.detail == "cross-namespace access requires root"
    assert backend.calls == []


def test_list_backend_failure_is_wrapped_as_500(backend):
    backend.errors["list_journal_entries"] = RuntimeError("boom")

    with pytest.raises(HTTPException) as exc:
        _list(topic=None, date_str=None, search=None, limit=20, user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Internal server error"


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_list_limit_bounds_are_enforced_by_the_query_declaration(backend, limit):
    # `limit: int = Query(20, ge=1, le=200)` is enforced by FastAPI when the
    # query string is parsed — see test_http_get_journal_limit_out_of_range_is_422.
    # The bare coroutine applies no clamp, so an out-of-range value handed
    # straight to the handler is forwarded to the backend verbatim.
    _list(topic=None, date_str=None, search=None, limit=limit, user=ALICE, owner_id=None, namespace=None)

    _, kwargs = backend.only_call()
    assert kwargs["limit"] == limit


# ── DELETE /journal/{entry_id} ───────────────────────────────────────────


def test_delete_returns_none_and_forwards_scope(backend):
    out = _delete(entry_id="abc", user=ALICE, owner_id=None, namespace=None)

    assert out is None, "the 204 route returns no body"
    method, kwargs = backend.only_call()
    assert method == "delete_journal_entry"
    assert kwargs == {"entry_id": "abc", "owner_id": "alice", "namespace": "default"}
    assert backend.tx_count == 1


def test_delete_missing_entry_is_404(backend):
    backend.results["delete_journal_entry"] = False

    with pytest.raises(HTTPException) as exc:
        _delete(entry_id="abc", user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Entry not found"


@pytest.mark.parametrize("falsy", [0, "", None])
def test_delete_treats_any_falsy_return_as_not_found(backend, falsy):
    # The route tests `if not deleted:`, so a backend returning a falsy
    # non-bool (rowcount 0, a driver's None) is a 404, not a success.
    backend.results["delete_journal_entry"] = falsy

    with pytest.raises(HTTPException) as exc:
        _delete(entry_id="abc", user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 404


def test_delete_non_root_cross_namespace_override_is_403(backend):
    with pytest.raises(HTTPException) as exc:
        _delete(entry_id="abc", user=ALICE, owner_id=None, namespace="vault")

    assert exc.value.status_code == 403
    assert backend.calls == []


def test_delete_root_may_target_another_owner(backend):
    _delete(entry_id="abc", user=ROOT, owner_id="bob", namespace=None)

    _, kwargs = backend.only_call()
    assert kwargs["owner_id"] == "bob"
    assert kwargs["namespace"] == "default"


def test_delete_does_not_use_the_backend_entry_id_for_scoping_only(backend):
    # The entry_id is passed verbatim (no UUID validation) — the backend's
    # owner/namespace predicate is what stops a cross-tenant delete.
    _delete(entry_id="not-a-uuid", user=ALICE, owner_id=None, namespace=None)

    _, kwargs = backend.only_call()
    assert kwargs["entry_id"] == "not-a-uuid"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (pinned): DELETE /journal/{entry_id} is the only route in "
        "journal.py without the try/except → 500 mapping its siblings have, "
        "so a backend/transaction failure escapes as a raw exception "
        "(a 500 with no JSON body in production, and an unhandled error in "
        "any direct caller) instead of the module's documented "
        "'Internal server error' contract. Fixing it belongs to production "
        "code, not to this characterisation suite."
    ),
)
def test_delete_failure_is_not_wrapped_as_500(backend):
    backend.errors["delete_journal_entry"] = RuntimeError("connection reset")

    with pytest.raises(HTTPException) as exc:
        _delete(entry_id="abc", user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Internal server error"


@pytest.mark.parametrize("route", ["create", "list", "delete"])
def test_missing_backend_is_503_on_every_route(monkeypatch, pinned_uuid, route):
    monkeypatch.setattr(_lc, "_persistence_backend", None, raising=False)

    with pytest.raises(HTTPException) as exc:
        if route == "create":
            _create(req=jr.JournalCreateRequest(topic="t", content="c"), user=ALICE, owner_id=None, namespace=None)
        elif route == "list":
            _list(topic=None, date_str=None, search=None, limit=20, user=ALICE, owner_id=None, namespace=None)
        else:
            _delete(entry_id="abc", user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 503
    assert exc.value.detail == "Persistence backend not available"


def test_missing_backend_wins_over_a_bad_date(monkeypatch):
    # backend_or_503() is the first statement in each handler, so a missing
    # backend answers 503 even for a request that would otherwise be 422.
    monkeypatch.setattr(_lc, "_persistence_backend", None, raising=False)
    req = jr.JournalCreateRequest(topic="t", content="c", date="nope")

    with pytest.raises(HTTPException) as exc:
        _create(req=req, user=ALICE, owner_id=None, namespace=None)

    assert exc.value.status_code == 503


# ── HTTP-level contract through a real ASGI request/response cycle ───────
#
# The journal router is mounted on a minimal app rather than on
# ``mnemos.api.main.app``: booting the full app runs the lifespan, which
# builds a real persistence backend and refuses to start when auth is
# disabled on a non-loopback bind. Mounting the real router keeps the real
# FastAPI layer — request-body validation, the ``?date=`` query alias,
# ``Query(ge=1, le=200)``, the declared status codes and the JSON error
# envelope — without touching a database or the process environment.


@pytest.fixture
def client(backend):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(jr.router)
    app.dependency_overrides[get_current_user] = lambda: ALICE
    try:
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_http_post_journal_is_201_and_returns_the_row(client, backend, pinned_uuid):
    backend.results["create_journal_entry"] = {"id": pinned_uuid, "topic": "t", "content": "c"}

    resp = client.post("/journal", json={"topic": "t", "content": "c"})

    assert resp.status_code == 201
    assert resp.json() == {"id": pinned_uuid, "topic": "t", "content": "c"}
    _, kwargs = backend.only_call()
    assert kwargs["owner_id"] == "alice"


def test_http_post_forwards_the_scope_query_params(client, backend, pinned_uuid):
    # Non-root caller + an owner override through the real dependency stack:
    # the 403 comes from the route body, not from the app.
    resp = client.post("/journal?owner_id=bob", json={"topic": "t", "content": "c"})

    assert resp.status_code == 403
    assert backend.calls == []


def test_http_post_missing_content_is_422_from_pydantic(client, backend):
    resp = client.post("/journal", json={"topic": "t"})

    assert resp.status_code == 422
    assert backend.calls == []


def test_http_post_bad_date_is_422_with_the_module_detail(client, backend):
    resp = client.post("/journal", json={"topic": "t", "content": "c", "date": "05-11-2026"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Invalid date format; expected YYYY-MM-DD"
    assert backend.calls == []


def test_http_get_journal_uses_the_date_query_alias(client, backend):
    backend.results["list_journal_entries"] = [{"id": "a"}]

    resp = client.get("/journal", params={"date": "2026-11-05", "limit": 5})

    assert resp.status_code == 200
    assert resp.json() == {"entries": [{"id": "a"}], "count": 1}
    _, kwargs = backend.only_call()
    assert kwargs["entry_date"] == date(2026, 11, 5)
    assert kwargs["limit"] == 5


def test_http_get_journal_ignores_an_unknown_query_param(client, backend):
    # FastAPI drops undeclared query params rather than rejecting them.
    resp = client.get("/journal", params={"nonsense": "1"})

    assert resp.status_code == 200
    _, kwargs = backend.only_call()
    assert kwargs["topic"] is None
    assert kwargs["search"] is None


@pytest.mark.parametrize("limit", [0, -1, 201, 1000, "abc"])
def test_http_get_journal_limit_out_of_range_is_422(client, backend, limit):
    resp = client.get("/journal", params={"limit": limit})

    assert resp.status_code == 422
    assert backend.calls == []


def test_http_delete_journal_is_204_with_empty_body(client, backend):
    resp = client.delete("/journal/some-id")

    assert resp.status_code == 204
    assert resp.content == b""
    _, kwargs = backend.only_call()
    assert kwargs["entry_id"] == "some-id"


def test_http_delete_missing_entry_is_404(client, backend):
    backend.results["delete_journal_entry"] = False

    resp = client.delete("/journal/some-id")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Entry not found"


def test_http_post_backend_error_is_500_json(client, backend, pinned_uuid):
    backend.errors["create_journal_entry"] = RuntimeError("boom")

    resp = client.post("/journal", json={"topic": "t", "content": "c"})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal server error"


def test_http_missing_backend_is_503_with_the_shared_detail(client, backend):
    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = None
    try:
        resp = client.get("/journal")
    finally:
        lifecycle._persistence_backend = backend

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Persistence backend not available"
