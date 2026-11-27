"""Characterisation tests for ``mnemos/api/routes/state.py``.

``state.py`` (152 lines) had zero test references before this file. It is the
per-owner, per-namespace KV surface behind ``GET /state``, ``GET /state/{key}``,
``PUT /state/{key}`` and ``DELETE /state/{key}``.

These tests PIN today's behaviour through the module's public handlers,
``StateSetRequest`` and the router itself:

  * the success shapes (``{"keys": [...]}`` for the list, the row dict for
    get/set, ``None`` for the 204 delete);
  * the owner/namespace scoping: the caller's ``user_id``/``namespace`` is
    what reaches the repository, and the root-only ``?owner_id=`` /
    ``?namespace=`` overrides;
  * the read-side ``MNEMOS_SM:v1:`` envelope strip, and the rows that must
    NOT be stripped (legacy opaque text, non-``str`` values, a bare prefix);
  * every raised ``HTTPException``/error mapping: the 503 when no persistence
    backend is active, the 404s, the 409 for a soft-deleted key, the
    ``NotImplementedError`` -> 503 and generic -> 500 translation, and the
    ``HTTPException`` pass-through;
  * the one edge that is easy to regress: a repository row that is *falsy*
    but not ``None`` for get, versus the ``row is None`` (not falsy) check
    for set.

Two layers are exercised on purpose:

* the route-level truthfulness (scoping, envelope, error mapping) against a
  recording fake backend, so the assertions are about the *handler's*
  decisions; and
* the end-to-end path against a real ``SqliteBackend`` in ``tmp_path`` (no
  network, no monkeypatched SQL) so the pinned formats are the ones a real
  backend really stores.

Nothing here sleeps, touches the network, or depends on wall-clock time.

One suspected defect is pinned as ``xfail(strict=True)`` — see
``test_direct_call_without_owner_id_arguments_does_not_raise``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import get_current_user
from mnemos.api.routes import state as state_routes
from mnemos.core.auth_context import UserContext
from mnemos.domain.memory_categorization.state import _SM_ENVELOPE_PREFIX
from mnemos.persistence.sqlite import SqliteBackend

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_ROW_FIELDS = ("key", "value", "updated", "version", "owner_id", "namespace")


def _row(
    key: str = "alpha",
    value: Any = '"v"',
    *,
    version: int | str = 1,
    owner_id: str = "alice",
    namespace: str = "alice-ns",
) -> dict:
    """A backend-shaped state row (the SQLite column order)."""
    return {
        "key": key,
        "value": value,
        "updated": "2026-11-05 00:00:00",
        "version": version,
        "owner_id": owner_id,
        "namespace": namespace,
    }


class _FakeStateRepo:
    """Records every ``state_kv`` call and returns queued results.

    The route handlers only ever call the five ``StateRepository`` methods
    with keyword-only scoping, so a recording dict of queues is enough to
    observe exactly what the handler asked for.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._results: dict[str, list[Any]] = {}
        self._raises: dict[str, BaseException] = {}

    # -- configuration -----------------------------------------------------
    def returns(self, name: str, *results: Any) -> None:
        self._results[name] = list(results)

    def raises(self, name: str, exc: BaseException) -> None:
        self._raises[name] = exc

    # -- introspection -----------------------------------------------------
    def calls_for(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for called, kwargs in self.calls if called == name]

    # -- transport ---------------------------------------------------------
    async def _dispatch(self, name: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((name, kwargs))
        if name in self._raises:
            raise self._raises[name]
        queue = self._results.get(name) or [None]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def get(self, tx, key, **kwargs):
        return await self._dispatch("get", {"key": key, **kwargs})

    async def set(self, tx, key, value, **kwargs):
        return await self._dispatch("set", {"key": key, "value": value, **kwargs})

    async def delete(self, tx, key, **kwargs):
        return await self._dispatch("delete", {"key": key, **kwargs})

    async def list_namespace(self, tx, **kwargs):
        return await self._dispatch("list_namespace", dict(kwargs))

    async def delete_namespace(self, tx, **kwargs):
        return await self._dispatch("delete_namespace", dict(kwargs))


class _FakeBackendWithState:
    """Backend facade that advertises ``state`` and owns a fake repo."""

    capabilities = frozenset({"core", "state"})

    def __init__(self) -> None:
        self.state_kv = _FakeStateRepo()
        self.transactions = 0

    def transactional(self, **_kwargs):
        backend = self

        class _Tx:
            async def __aenter__(self_inner):
                backend.transactions += 1
                return SimpleNamespace(_fake=True, conn=SimpleNamespace())

            async def __aexit__(self_inner, *exc_info):
                return False

        return _Tx()


class _BackendWithoutState:
    """Backend that has a repo but does not advertise the capability."""

    capabilities = frozenset({"core"})

    def __init__(self) -> None:
        self.state_kv = _FakeStateRepo()


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _alice(namespace: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace=namespace,
        authenticated=True,
    )


def _root(namespace: str = "default") -> UserContext:
    return UserContext(
        user_id="admin",
        group_ids=[],
        role="root",
        namespace=namespace,
        authenticated=True,
    )


def _call(awaitable):
    """Run a handler coroutine on a fresh event loop (deterministic)."""
    return asyncio.run(awaitable)


@pytest.fixture
def fake_backend(monkeypatch) -> _FakeBackendWithState:
    backend = _FakeBackendWithState()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)
    return backend


@pytest.fixture
def no_backend(monkeypatch):
    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    monkeypatch.setattr(lifecycle, "_pool", None)


@pytest.fixture
def sqlite_backend(monkeypatch, tmp_path) -> SqliteBackend:
    """A real SQLite persistence facade in ``tmp_path`` (no network)."""
    backend = SqliteBackend(tmp_path / "state-char.db", SimpleNamespace())
    asyncio.run(backend.open())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)
    try:
        yield backend
    finally:
        asyncio.run(backend.close())


# ---------------------------------------------------------------------------
# Module / router surface
# ---------------------------------------------------------------------------


def test_module_is_untouched_by_these_tests():
    """Sanity pin: the file under characterisation is the small route module."""
    assert state_routes.router.prefix == ""
    assert state_routes.router.tags == ["state"]
    assert state_routes.StateSetRequest.model_fields["value"].annotation is Any


def test_router_exposes_exactly_the_four_state_routes():
    """Route table is part of the public contract (paths + methods + statuses)."""
    routes = [
        (route.path, sorted(route.methods), getattr(route, "status_code", None))
        for route in state_routes.router.routes
    ]
    assert routes == [
        ("/state", ["GET"], None),
        ("/state/{key}", ["GET"], None),
        ("/state/{key}", ["PUT"], 200),
        ("/state/{key}", ["DELETE"], 204),
    ]


def test_state_set_request_wraps_any_json_value():
    for value in (_SM_ENVELOPE_PREFIX + "1", None, 0, False, "", [], {}, 3.5):
        req = state_routes.StateSetRequest(value=value)
        assert req.value == value


def test_state_set_request_rejects_a_missing_value():
    with pytest.raises(ValidationError) as excinfo:
        state_routes.StateSetRequest()
    message = str(excinfo.value)
    assert "value" in message
    assert "Field required" in message


# ---------------------------------------------------------------------------
# No persistence backend -> 503 before any scoping work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda user: state_routes.list_state_keys(user=user), id="list"),
        pytest.param(lambda user: state_routes.get_state("k", user=user), id="get"),
        pytest.param(
            lambda user: state_routes.set_state(
                "k", state_routes.StateSetRequest(value=1), user=user
            ),
            id="set",
        ),
        pytest.param(lambda user: state_routes.delete_state("k", user=user), id="delete"),
    ],
)
def test_missing_backend_raises_503_before_scoping(no_backend, invoke):
    """The backend guard runs first: even a non-root caller with no override
    gets the 503, not a 403 or a KeyError."""
    with pytest.raises(HTTPException) as excinfo:
        _call(invoke(_alice()))
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == "Persistence backend not available"


def test_backend_without_state_capability_raises_503_naming_the_backend(
    monkeypatch,
):
    backend = _BackendWithoutState()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    with pytest.raises(HTTPException) as excinfo:
        _call(state_routes.get_state("k", user=_alice(), owner_id=None, namespace=None))
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == (
        "persistence backend does not support 'state' for _BackendWithoutState"
    )
    assert backend.state_kv.calls == []


# ---------------------------------------------------------------------------
# Owner / namespace scoping
# ---------------------------------------------------------------------------


def test_scoping_uses_the_callers_identity(fake_backend):
    row = _row()

    async def scenario() -> dict:
        fake_backend.state_kv.returns("set", _row("alpha", '{"n": 1}'))
        await state_routes.set_state(
            "alpha",
            state_routes.StateSetRequest(value={"n": 1}),
            user=_alice(),
            owner_id=None,
            namespace=None,
        )
        fake_backend.state_kv.returns("get", row)
        fetched = await state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None)
        fake_backend.state_kv.returns("list_namespace", [row])
        listed = await state_routes.list_state_keys(user=_alice(), owner_id=None, namespace=None)
        fake_backend.state_kv.returns("delete", True)
        await state_routes.delete_state("alpha", user=_alice(), owner_id=None, namespace=None)
        return {"get": fetched, "list": listed}

    result = _call(scenario())

    for name in ("set", "get", "delete"):
        call = fake_backend.state_kv.calls_for(name)[0]
        assert call["owner_id"] == "alice"
        assert call["namespace"] == "alice-ns"
    list_call = fake_backend.state_kv.calls_for("list_namespace")[0]
    assert list_call == {"owner_id": "alice", "namespace": "alice-ns"}
    assert result["get"]["key"] == "alpha"


def test_set_persists_the_value_as_compact_json(fake_backend):
    payloads: dict[str, Any] = {}

    async def scenario() -> None:
        for key, value in (
            ("dict", {"a": 1, "b": [1, 2]}),
            ("str", "hello"),
            ("none", None),
            ("bool", False),
            ("int", 0),
        ):
            fake_backend.state_kv.returns("set", _row(key))
            await state_routes.set_state(
                key,
                state_routes.StateSetRequest(value=value),
                user=_alice(),
                owner_id=None,
                namespace=None,
            )
            payloads[key] = fake_backend.state_kv.calls_for("set")[-1]["value"]

    _call(scenario())

    assert payloads["dict"] == '{"a": 1, "b": [1, 2]}'
    assert payloads["str"] == '"hello"'
    assert payloads["none"] == "null"
    assert payloads["bool"] == "false"
    assert payloads["int"] == "0"
    # Every payload is valid JSON back to the original value.
    assert json.loads(payloads["dict"]) == {"a": 1, "b": [1, 2]}
    assert json.loads(payloads["none"]) is None


def test_root_may_override_owner_and_namespace(fake_backend):
    fake_backend.state_kv.returns("list_namespace", [])

    _call(
        state_routes.list_state_keys(
            user=_root(),
            owner_id="someone-else",
            namespace="their-ns",
        )
    )

    assert fake_backend.state_kv.calls_for("list_namespace") == [
        {"owner_id": "someone-else", "namespace": "their-ns"}
    ]


def test_override_equal_to_the_callers_own_scope_is_allowed_for_a_user(fake_backend):
    """``scope_owner``/``scope_namespace`` only gate a *different* target."""
    fake_backend.state_kv.returns("list_namespace", [])

    _call(
        state_routes.list_state_keys(
            user=_alice(),
            owner_id="alice",
            namespace="alice-ns",
        )
    )

    assert fake_backend.state_kv.calls_for("list_namespace") == [
        {"owner_id": "alice", "namespace": "alice-ns"}
    ]


def test_non_root_owner_override_raises_403_before_the_repository(fake_backend):
    with pytest.raises(HTTPException) as excinfo:
        _call(
            state_routes.get_state(
                "k", user=_alice(), owner_id="someone-else", namespace=None
            )
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "owner_id override requires root"
    assert fake_backend.state_kv.calls == []
    assert fake_backend.transactions == 0


def test_non_root_namespace_override_raises_403_before_the_repository(fake_backend):
    with pytest.raises(HTTPException) as excinfo:
        _call(
            state_routes.delete_state(
                "k", user=_alice(), owner_id=None, namespace="their-ns"
            )
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "cross-namespace access requires root"
    assert fake_backend.state_kv.calls == []


@pytest.mark.parametrize("handler_name", ["list_state_keys", "get_state", "set_state", "delete_state"])
def test_every_handler_rejects_a_non_root_owner_override(fake_backend, handler_name):
    handler = getattr(state_routes, handler_name)
    args: tuple = () if handler_name == "list_state_keys" else ("k",)
    extra = (
        (state_routes.StateSetRequest(value=1),) if handler_name == "set_state" else ()
    )

    with pytest.raises(HTTPException) as excinfo:
        _call(handler(*args, *extra, user=_alice(), owner_id="eve", namespace=None))

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "owner_id override requires root"


# ---------------------------------------------------------------------------
# Success shapes
# ---------------------------------------------------------------------------


def test_list_state_keys_wraps_rows_under_keys_and_copies_them(fake_backend):
    rows = [_row("a"), _row("b", version=7)]

    result = _call(
        _list_with(fake_backend, rows)
    )

    assert result == {"keys": rows}
    # ``dict(r)`` per row: the response is not the backend's own objects.
    assert result["keys"][0] is not rows[0]


def _list_with(backend: _FakeBackendWithState, rows: list[dict]):
    backend.state_kv.returns("list_namespace", rows)
    return state_routes.list_state_keys(user=_alice(), owner_id=None, namespace=None)


def test_list_state_keys_maps_an_empty_namespace_to_an_empty_list(fake_backend):
    result = _call(_list_with(fake_backend, []))

    assert result == {"keys": []}
    # The empty result is still a dict with a list value, never None.
    assert isinstance(result["keys"], list)


def test_get_state_returns_the_row_dict(fake_backend):
    row = _row("alpha", '"v"', version=3)
    fake_backend.state_kv.returns("get", row)

    result = _call(
        state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None)
    )

    assert result == row
    assert tuple(result) == _ROW_FIELDS
    assert fake_backend.state_kv.calls_for("get") == [
        {"key": "alpha", "owner_id": "alice", "namespace": "alice-ns"}
    ]


def test_set_state_returns_the_written_row(fake_backend):
    row = _row("alpha", '{"n": 2}', version=2)
    fake_backend.state_kv.returns("set", row)

    result = _call(
        state_routes.set_state(
            "alpha",
            state_routes.StateSetRequest(value={"n": 2}),
            user=_alice(),
            owner_id=None,
            namespace=None,
        )
    )

    assert result == row
    assert set(result) == set(_ROW_FIELDS)


def test_delete_state_returns_none_for_the_204_response(fake_backend):
    fake_backend.state_kv.returns("delete", True)

    result = _call(
        state_routes.delete_state("alpha", user=_alice(), owner_id=None, namespace=None)
    )

    assert result is None


# ---------------------------------------------------------------------------
# Read-side envelope strip
# ---------------------------------------------------------------------------


def test_get_state_strips_the_state_manager_envelope_prefix(fake_backend):
    fake_backend.state_kv.returns("get", _row("alpha", _SM_ENVELOPE_PREFIX + '{"n": 1}'))

    result = _call(state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None))

    assert result["value"] == '{"n": 1}'


def test_get_state_strips_only_one_prefix_occurrence(fake_backend):
    doubled = _SM_ENVELOPE_PREFIX + _SM_ENVELOPE_PREFIX + "1"
    fake_backend.state_kv.returns("get", _row("alpha", doubled))

    result = _call(state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None))

    assert result["value"] == _SM_ENVELOPE_PREFIX + "1"


def test_get_state_strips_the_prefix_from_a_non_json_suffix(fake_backend):
    """The strip is a plain string operation: no JSON validation happens."""
    fake_backend.state_kv.returns("get", _row("alpha", _SM_ENVELOPE_PREFIX + "not json"))

    result = _call(state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None))

    assert result["value"] == "not json"


def test_get_state_strips_the_prefix_down_to_an_empty_string(fake_backend):
    fake_backend.state_kv.returns("get", _row("alpha", _SM_ENVELOPE_PREFIX))

    result = _call(state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None))

    assert result["value"] == ""


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("raw opaque text", id="legacy-text"),
        pytest.param('{"n": 1}', id="json-without-envelope"),
        pytest.param("mnemos_sm:v1:lower", id="lowercase-prefix"),
        pytest.param(" MNEMOS_SM:v1:1", id="leading-space"),
        pytest.param(123, id="int-value"),
        pytest.param(None, id="none-value"),
        pytest.param({"nested": 1}, id="dict-value"),
        pytest.param(["a"], id="list-value"),
    ],
)
def test_get_state_leaves_non_enveloped_values_untouched(fake_backend, stored):
    fake_backend.state_kv.returns("get", _row("alpha", stored))

    result = _call(state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None))

    assert result["value"] == stored


def test_get_state_tolerates_a_row_without_a_value_column(fake_backend):
    """``result.get("value")`` — a projection row is returned unchanged."""
    fake_backend.state_kv.returns("get", {"key": "alpha", "version": 1})

    result = _call(state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None))

    assert result == {"key": "alpha", "version": 1}


def test_get_state_does_not_mutate_the_repository_row(fake_backend):
    row = _row("alpha", _SM_ENVELOPE_PREFIX + "1")
    fake_backend.state_kv.returns("get", row)

    _call(state_routes.get_state("alpha", user=_alice(), owner_id=None, namespace=None))

    assert row["value"] == _SM_ENVELOPE_PREFIX + "1"


# ---------------------------------------------------------------------------
# 404 / 409 paths
# ---------------------------------------------------------------------------


def test_get_state_missing_key_raises_404_with_the_key(fake_backend):
    fake_backend.state_kv.returns("get", None)

    with pytest.raises(HTTPException) as excinfo:
        _call(state_routes.get_state("ghost", user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "State key 'ghost' not found"


def test_get_state_treats_a_falsy_non_none_row_as_missing(fake_backend):
    """``if not row`` — the get path is falsy-based, not ``is None``."""
    fake_backend.state_kv.returns("get", {})

    with pytest.raises(HTTPException) as excinfo:
        _call(state_routes.get_state("empty", user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "State key 'empty' not found"


def test_delete_state_missing_key_raises_404_with_the_key(fake_backend):
    fake_backend.state_kv.returns("delete", False)

    with pytest.raises(HTTPException) as excinfo:
        _call(state_routes.delete_state("ghost", user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "State key 'ghost' not found"


def test_delete_state_reports_a_missing_key_even_when_none_is_returned(fake_backend):
    fake_backend.state_kv.returns("delete", None)

    with pytest.raises(HTTPException) as excinfo:
        _call(state_routes.delete_state("ghost", user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 404


def test_set_state_soft_deleted_key_raises_409(fake_backend):
    fake_backend.state_kv.returns("set", None)

    with pytest.raises(HTTPException) as excinfo:
        _call(
            state_routes.set_state(
                "tombstoned",
                state_routes.StateSetRequest(value=1),
                user=_alice(),
                owner_id=None,
                namespace=None,
            )
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == (
        "State key is reserved by a soft-deleted row; restore before updating it"
    )


def test_set_state_returns_a_falsy_but_present_row(fake_backend):
    """The set path checks ``row is None``; an empty dict is a success."""
    fake_backend.state_kv.returns("set", {})

    result = _call(
        state_routes.set_state("alpha", state_routes.StateSetRequest(value=1), user=_alice(), owner_id=None, namespace=None)
    )

    assert result == {}


# ---------------------------------------------------------------------------
# Persistence-error translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler_name", ["list_state_keys", "get_state", "set_state", "delete_state"])
def test_not_implemented_error_becomes_503_with_a_generic_capability_message(fake_backend, handler_name):
    repo = fake_backend.state_kv
    repo.raises("list_namespace", NotImplementedError("no list on this backend"))
    repo.raises("get", NotImplementedError("no get on this backend"))
    repo.raises("set", NotImplementedError("no set on this backend"))
    repo.raises("delete", NotImplementedError("no delete on this backend"))
    handler = getattr(state_routes, handler_name)
    args: tuple = () if handler_name == "list_state_keys" else ("k",)
    extra = (
        (state_routes.StateSetRequest(value=1),) if handler_name == "set_state" else ()
    )

    with pytest.raises(HTTPException) as excinfo:
        _call(handler(*args, *extra, user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 503
    # translate_persistence_error returns a fixed generic detail: the backend's
    # exception text is NOT client-visible (the distinct texts above prove it).
    assert excinfo.value.detail == "Capability temporarily unavailable"


@pytest.mark.parametrize(
    "handler_name, repo_method, log_message",
    [
        ("list_state_keys", "list_namespace", "Error listing state keys: boom"),
        ("get_state", "get", "Error getting state key: boom"),
        ("set_state", "set", "Error setting state key 'k': boom"),
    ],
)
def test_unexpected_error_becomes_a_generic_500_and_is_logged(
    fake_backend, caplog, handler_name, repo_method, log_message
):
    fake_backend.state_kv.raises(repo_method, RuntimeError("boom"))
    handler = getattr(state_routes, handler_name)
    args: tuple = () if handler_name == "list_state_keys" else ("k",)
    extra = (
        (state_routes.StateSetRequest(value=1),) if handler_name == "set_state" else ()
    )

    with caplog.at_level(logging.ERROR, logger=state_routes.__name__):
        with pytest.raises(HTTPException) as excinfo:
            _call(handler(*args, *extra, user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 500
    # The exception text is NOT client-visible.
    assert excinfo.value.detail == "Internal server error"
    assert log_message in caplog.text
    assert "Traceback" in caplog.text  # exc_info=True


def test_delete_state_does_not_translate_an_unexpected_error(fake_backend):
    """``delete_state`` has no generic ``except``: a plain error propagates as-is.

    This is the current shape of the module — the other three handlers log and
    map to 500, delete does not. Pinned so a later unification is deliberate.
    """
    fake_backend.state_kv.raises("delete", RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        _call(state_routes.delete_state("k", user=_alice(), owner_id=None, namespace=None))


def test_delete_state_does_not_translate_a_disconnect_either(fake_backend):
    class InterfaceError(Exception):
        pass

    fake_backend.state_kv.raises("delete", InterfaceError("connection lost"))

    with pytest.raises(InterfaceError):
        _call(state_routes.delete_state("k", user=_alice(), owner_id=None, namespace=None))


def test_lost_connection_maps_to_a_503_database_disconnected_body(fake_backend):
    class InterfaceError(Exception):
        pass

    fake_backend.state_kv.raises("get", InterfaceError("connection lost"))

    with pytest.raises(HTTPException) as excinfo:
        _call(state_routes.get_state("k", user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 503
    # database_disconnected_detail returns a generic message plus a request_id
    # for correlation; the exception type and text are NOT client-visible.
    detail = excinfo.value.detail
    assert detail["error"] == "database_disconnected"
    assert detail["message"] == "Database temporarily unavailable"
    assert isinstance(detail["request_id"], str) and detail["request_id"]
    assert "exception_type" not in detail


def test_a_transaction_body_exception_is_rolled_back_and_translated(fake_backend):
    """The repository error escapes the ``async with`` before translation."""
    original = fake_backend.state_kv.get

    async def failing_get(tx, key, **kwargs):
        return await original(tx, key, **kwargs)

    fake_backend.state_kv.raises("get", NotImplementedError("gone"))
    fake_backend.state_kv.get = failing_get  # type: ignore[assignment]

    with pytest.raises(HTTPException) as excinfo:
        _call(state_routes.get_state("k", user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 503
    assert fake_backend.transactions == 1


def test_errors_from_transaction_setup_are_mapped_too(fake_backend):
    class PoolGone(RuntimeError):
        pass

    def broken_transactional(**_kwargs):
        raise PoolGone("pool closed")

    fake_backend.transactional = broken_transactional  # type: ignore[assignment]

    with pytest.raises(HTTPException) as excinfo:
        _call(state_routes.list_state_keys(user=_alice(), owner_id=None, namespace=None))

    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == "Internal server error"


# ---------------------------------------------------------------------------
# End-to-end against a real SqliteBackend (tmp_path, no network)
# ---------------------------------------------------------------------------


def test_sqlite_round_trip_through_the_handlers(sqlite_backend):
    async def scenario() -> dict:
        user = _alice()
        written = await state_routes.set_state(
            "alpha", state_routes.StateSetRequest(value={"n": 1}), user=user,
            owner_id=None, namespace=None,
        )
        fetched = await state_routes.get_state("alpha", user=user, owner_id=None, namespace=None)
        listed = await state_routes.list_state_keys(user=user, owner_id=None, namespace=None)
        updated = await state_routes.set_state(
            "alpha", state_routes.StateSetRequest(value=[1, 2]), user=user,
            owner_id=None, namespace=None,
        )
        deleted = await state_routes.delete_state("alpha", user=user, owner_id=None, namespace=None)
        after = await state_routes.list_state_keys(user=user, owner_id=None, namespace=None)
        return {
            "written": written,
            "fetched": fetched,
            "listed": listed,
            "updated": updated,
            "deleted": deleted,
            "after": after,
        }

    out = _call(scenario())

    assert out["written"]["value"] == '{"n": 1}'
    assert out["written"]["version"] == 1
    # The REST-facing value is the caller's JSON, not the storage payload.
    assert json.loads(out["fetched"]["value"]) == {"n": 1}
    assert [row["key"] for row in out["listed"]["keys"]] == ["alpha"]
    # ``list_state_keys`` is a KEY listing, not a row dump: the SQLite
    # ``list_namespace`` projection selects key/updated/version/owner_id/
    # namespace and never the ``value`` column, while ``get`` does. Pin both
    # sides so neither can drift into the other's shape.
    assert sorted(out["listed"]["keys"][0]) == [
        "key",
        "namespace",
        "owner_id",
        "updated",
        "version",
    ]
    assert sorted(out["fetched"]) == [
        "key",
        "namespace",
        "owner_id",
        "updated",
        "value",
        "version",
    ]
    assert out["updated"]["version"] == 2
    assert json.loads(out["updated"]["value"]) == [1, 2]
    assert out["deleted"] is None
    assert out["after"] == {"keys": []}


def test_sqlite_returns_404_after_delete_and_409_in_a_restore_scenario(sqlite_backend):
    async def scenario():
        user = _alice()
        await state_routes.set_state(
            "alpha", state_routes.StateSetRequest(value=1), user=user,
            owner_id=None, namespace=None,
        )
        await state_routes.delete_state("alpha", user=user, owner_id=None, namespace=None)
        with pytest.raises(HTTPException) as missing:
            await state_routes.get_state("alpha", user=user, owner_id=None, namespace=None)
        with pytest.raises(HTTPException) as tombstone:
            await state_routes.delete_state("alpha", user=user, owner_id=None, namespace=None)
        return missing.value, tombstone.value

    missing, tombstone = _call(scenario())
    assert (missing.status_code, missing.detail) == (404, "State key 'alpha' not found")
    assert (tombstone.status_code, tombstone.detail) == (404, "State key 'alpha' not found")


def test_sqlite_namespaces_are_isolated(sqlite_backend):
    async def scenario():
        alice = _alice("alice-ns")
        bob = UserContext(
            user_id="bob", group_ids=[], role="user", namespace="bob-ns", authenticated=True
        )
        await state_routes.set_state(
            "shared",
            state_routes.StateSetRequest(value="mine"),
            user=alice,
            owner_id=None,
            namespace=None,
        )
        with pytest.raises(HTTPException) as excinfo:
            await state_routes.get_state("shared", user=bob, owner_id=None, namespace=None)
        return excinfo.value

    excinfo = _call(scenario())
    assert excinfo.status_code == 404


def test_sqlite_root_can_read_another_owners_key(sqlite_backend):
    async def scenario() -> dict:
        await state_routes.set_state(
            "alpha",
            state_routes.StateSetRequest(value="alice-only"),
            user=_alice(),
            owner_id=None,
            namespace=None,
        )
        return await state_routes.get_state(
            "alpha", user=_root("root-ns"), owner_id="alice", namespace="alice-ns"
        )

    row = _call(scenario())

    assert json.loads(row["value"]) == "alice-only"
    assert row["owner_id"] == "alice"


def test_sqlite_store_written_envelope_is_stripped_on_read(sqlite_backend):
    """StateManager-written rows read back as the REST payload."""

    async def scenario() -> dict:
        async with sqlite_backend.transactional() as tx:
            await sqlite_backend.state_kv.set(
                tx,
                "sm-key",
                _SM_ENVELOPE_PREFIX + '{"value": {"n": 1}}',
                owner_id="alice",
                namespace="alice-ns",
            )
        return await state_routes.get_state("sm-key", user=_alice(), owner_id=None, namespace=None)

    row = _call(scenario())

    assert row["value"] == '{"value": {"n": 1}}'
    assert json.loads(row["value"]) == {"value": {"n": 1}}


def test_sqlite_soft_deleted_key_raises_409_on_set(sqlite_backend):
    """The 409 branch is reachable on a real backend with a tombstoned row."""

    async def scenario():
        user = _alice()
        await state_routes.set_state(
            "tomb", state_routes.StateSetRequest(value=1), user=user,
            owner_id=None, namespace=None,
        )
        async with sqlite_backend.transactional() as tx:
            conn = tx.conn if hasattr(tx, "conn") else None
            await conn.execute(
                "UPDATE state SET deleted_at = CURRENT_TIMESTAMP "
                "WHERE owner_id = ? AND namespace = ? AND key = ?",
                ("alice", "alice-ns", "tomb"),
            )
        with pytest.raises(HTTPException) as excinfo:
            await state_routes.set_state(
                "tomb", state_routes.StateSetRequest(value=2), user=user,
                owner_id=None, namespace=None,
            )
        return excinfo.value

    excinfo = _call(scenario())
    assert excinfo.status_code == 409


# ---------------------------------------------------------------------------
# HTTP surface (FastAPI wires the Query defaults to None)
# ---------------------------------------------------------------------------


@pytest.fixture
def http_client(sqlite_backend):
    app = FastAPI()
    app.include_router(state_routes.router)
    app.dependency_overrides[get_current_user] = _alice
    return TestClient(app)


def test_http_put_get_list_delete_round_trip(http_client):
    put = http_client.put("/state/k", json={"value": {"a": 1}})
    assert put.status_code == 200
    assert json.loads(put.json()["value"]) == {"a": 1}

    get = http_client.get("/state/k")
    assert get.status_code == 200
    assert json.loads(get.json()["value"]) == {"a": 1}

    listed = http_client.get("/state")
    assert listed.status_code == 200
    assert [row["key"] for row in listed.json()["keys"]] == ["k"]

    deleted = http_client.delete("/state/k")
    assert deleted.status_code == 204
    assert deleted.content == b""

    assert http_client.get("/state/k").status_code == 404


def test_http_missing_key_404_body(http_client):
    response = http_client.get("/state/ghost")
    assert response.status_code == 404
    assert response.json() == {"detail": "State key 'ghost' not found"}


def test_http_put_requires_a_value_field(http_client):
    response = http_client.put("/state/k", json={})
    assert response.status_code == 422


def test_http_no_backend_503_body(no_backend):
    app = FastAPI()
    app.include_router(state_routes.router)
    app.dependency_overrides[get_current_user] = _alice
    response = TestClient(app).get("/state")
    assert response.status_code == 503
    assert response.json() == {"detail": "Persistence backend not available"}


def test_http_non_root_override_403_body(sqlite_backend):
    app = FastAPI()
    app.include_router(state_routes.router)
    app.dependency_overrides[get_current_user] = _alice
    response = TestClient(app).get("/state", params={"owner_id": "eve"})
    assert response.status_code == 403
    assert response.json() == {"detail": "owner_id override requires root"}


# ---------------------------------------------------------------------------
# Suspected defect (pinned, not fixed)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: set_state/list_state_keys/get_state/delete_state declare "
        "owner_id/namespace as `Optional[str] = Query(None)`. FastAPI resolves "
        "that object to None on a request, but a DIRECT call that omits the "
        "arguments (the way every other handler-level test in this suite calls "
        "handlers, e.g. `await set_state(key, req, user=u)`) passes the truthy "
        "Query(...) sentinel into scope_owner/scope_namespace. Those helpers "
        "gate on truthiness (`if override and override != user.user_id`), so a "
        "non-root caller gets 403 'owner_id override requires root' on its own "
        "key. HTTP traffic is unaffected. Fixing it means defaulting to None "
        "and validating the override inside the handler (or making the scope "
        "helpers ignore non-str values) -- not done here, tests only."
    ),
)
def test_direct_call_without_owner_id_arguments_does_not_raise(sqlite_backend):
    """A non-root caller reading back its own key must succeed.

    The call deliberately omits ``owner_id``/``namespace`` — the natural way
    to call the handler, and how the rest of this suite's handler-level tests
    call theirs. It currently raises 403 on the caller's OWN key.
    """

    async def scenario():
        user = _alice()
        await state_routes.set_state(
            "alpha",
            state_routes.StateSetRequest(value=1),
            user=user,
            owner_id=None,
            namespace=None,
        )
        return await state_routes.get_state("alpha", user=user)

    row = _call(scenario())
    assert json.loads(row["value"]) == 1


def test_direct_call_with_the_query_sentinel_is_rejected_for_non_root(fake_backend):
    """The mechanism behind the xfail above, pinned as the CURRENT behaviour.

    Passing the module's own defaults positionally reproduces exactly what a
    bare ``set_state(key, req, user=u)`` call does today.
    """
    defaults = state_routes.set_state.__defaults__  # (user, owner_id, namespace)
    sentinel_owner, sentinel_ns = defaults[1], defaults[2]

    with pytest.raises(HTTPException) as excinfo:
        _call(
            state_routes.list_state_keys(
                user=_alice(), owner_id=sentinel_owner, namespace=sentinel_ns
            )
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "owner_id override requires root"
