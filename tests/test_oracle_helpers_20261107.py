"""Characterisation: `_conn_from_tx` and `_call` (FB797).

Two driver-shape adapters. `_conn_from_tx` turns a backend-neutral tx handle into
an oracledb connection; `_call` invokes a callable and awaits the result when the
driver returned a coroutine. Neither touches the database.
"""

from __future__ import annotations

import functools
import inspect

import pytest

from mnemos.persistence.oracle_helpers import _call, _conn_from_tx


class Handle:
    def __init__(self, conn):
        self.conn = conn


# ── _conn_from_tx ────────────────────────────────────────────────────────────


def test_none_has_no_connection():
    assert _conn_from_tx(None) is None


def test_the_connection_is_taken_from_the_documented_attribute():
    connection = object()
    assert _conn_from_tx(Handle(connection)) is connection


def test_an_object_without_the_attribute_is_its_own_connection():
    """DIVERGENCE: the brief expects a missing attribute to raise. `getattr(tx,
    "conn", tx)` falls back to the tx itself, so a handle without `conn` is
    silently treated as its own connection — no error is documented or raised."""
    connection = object()
    assert _conn_from_tx(connection) is connection


def test_a_none_valued_attribute_is_none_not_the_fallback():
    """The fallback only fires when the attribute is absent; an explicit `conn =
    None` returns `None`."""
    assert _conn_from_tx(Handle(None)) is None


def test_a_raising_connection_property_propagates():
    class Exploding:
        @property
        def conn(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _conn_from_tx(Exploding())


def test_the_connection_is_never_called():
    class CallableConn:
        def __call__(self):
            raise AssertionError("connection must not be called")

    connection = CallableConn()
    assert _conn_from_tx(Handle(connection)) is connection


def test_the_resolver_is_not_async_and_takes_one_argument():
    assert not inspect.iscoroutinefunction(_conn_from_tx)
    assert list(inspect.signature(_conn_from_tx).parameters) == ["tx"]


# ── _call ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [5, 0, "text", "", True, None])
async def test_a_plain_value_is_returned_unchanged(value):
    assert await _call(value) == value


@pytest.mark.asyncio
async def test_a_plain_object_is_returned_by_identity():
    payload = {"a": 1}
    assert await _call(payload) is payload


@pytest.mark.asyncio
async def test_a_sync_callable_is_invoked():
    assert await _call(lambda: "sync") == "sync"


@pytest.mark.asyncio
async def test_positional_arguments_are_forwarded():
    assert await _call(lambda a, b: a + b, 2, 3) == 5


@pytest.mark.asyncio
async def test_keyword_arguments_are_forwarded():
    def combine(a, *, b=0):
        return a + b

    assert await _call(combine, 2, b=5) == 7


@pytest.mark.asyncio
async def test_a_coroutine_function_is_awaited():
    async def work(value):
        return value * 2

    assert await _call(work, 4) == 8


@pytest.mark.asyncio
async def test_a_callable_returning_a_coroutine_is_awaited():
    def factory():
        async def inner():
            return "via-coroutine"

        return inner()

    assert await _call(factory) == "via-coroutine"


@pytest.mark.asyncio
async def test_a_coroutine_passed_directly_is_awaited():
    """A coroutine object is not callable, so the invocation branch is skipped and
    the awaitable branch takes over."""
    async def inner():
        return "direct"

    assert await _call(inner()) == "direct"


@pytest.mark.asyncio
async def test_a_future_passed_directly_is_awaited():
    import asyncio

    future = asyncio.get_running_loop().create_future()
    future.set_result("fut")
    assert await _call(future) == "fut"


@pytest.mark.asyncio
async def test_an_exception_from_the_callable_propagates():
    def explode():
        raise ValueError("call failed")

    with pytest.raises(ValueError, match="call failed"):
        await _call(explode)


@pytest.mark.asyncio
async def test_an_exception_from_the_awaited_result_propagates():
    async def explode():
        raise KeyError("awaited failed")

    with pytest.raises(KeyError):
        await _call(explode)


@pytest.mark.asyncio
async def test_a_class_is_callable_and_is_instantiated():
    """Pinned hazard: any callable goes through the invocation branch, so passing a
    class constructs an instance instead of returning the class."""
    class Widget:
        def __init__(self):
            self.made = True

    result = await _call(Widget)
    assert isinstance(result, Widget)
    assert result.made is True


@pytest.mark.asyncio
async def test_an_object_with_call_is_invoked():
    class Callable:
        def __call__(self, value):
            return value + 1

    assert await _call(Callable(), 1) == 2


@pytest.mark.asyncio
async def test_an_async_callable_object_is_awaited():
    class AsyncCallable:
        async def __call__(self, value):
            return value * 3

    assert await _call(AsyncCallable(), 3) == 9


@pytest.mark.asyncio
async def test_a_partial_is_invoked_with_the_remaining_arguments():
    def add(a, b):
        return a + b

    assert await _call(functools.partial(add, 2), 3) == 5


@pytest.mark.asyncio
async def test_a_callable_returning_none_yields_none():
    assert await _call(lambda: None) is None


@pytest.mark.asyncio
async def test_the_callable_is_invoked_exactly_once():
    calls = []

    def counted():
        calls.append(1)
        return "once"

    assert await _call(counted) == "once"
    assert len(calls) == 1


def test_call_is_a_coroutine_function_taking_a_value_and_forwarded_arguments():
    assert inspect.iscoroutinefunction(_call)
    parameters = list(inspect.signature(_call).parameters)
    assert parameters[:2] == ["value", "args"]
    assert "kwargs" in parameters
