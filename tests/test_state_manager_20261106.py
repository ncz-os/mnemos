"""FB461 — categorisation state manager for ``mnemos.domain.memory_categorization.state``.

Pins ``StateManager`` on ``master`` (``22a3f7d8``) against a fake backend: a
``transactional()`` context manager plus a ``state_kv`` fake, so no database is
touched.

Pinned divergences, not endorsed: ``get``/``delete``/``list_keys`` swallow backend
exceptions (return None/False/[]) while ``set`` propagates, so a read failure looks
exactly like a missing row; and the store only accepts JSON-serialisable values,
with ``load_today`` regenerating its default on every call.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from mnemos.core.auth_context import UserContext
from mnemos.domain.memory_categorization.state import StateManager, _wrap_state_value

PREFIX = "MNEMOS_SM:v1:"


def run(coro):
    return asyncio.run(coro)


def make_user(user_id="user-1", namespace="ns"):
    return UserContext(user_id=user_id, group_ids=[], role="user", namespace=namespace, authenticated=True)


class FakeKV:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.calls = []
        self.fail = None

    async def get(self, tx, key, *, owner_id, namespace):
        self.calls.append(("get", key, owner_id, namespace))
        if self.fail == "get":
            raise RuntimeError("store down")
        value = self.rows.get((owner_id, namespace, key))
        return None if value is None else {"key": key, "value": value}

    async def set(self, tx, key, value, *, owner_id, namespace):
        self.calls.append(("set", key, owner_id, namespace))
        if self.fail == "set":
            raise RuntimeError("write failed")
        self.rows[(owner_id, namespace, key)] = value

    async def delete(self, tx, key, *, owner_id, namespace):
        self.calls.append(("delete", key, owner_id, namespace))
        if self.fail == "delete":
            raise RuntimeError("delete failed")
        return self.rows.pop((owner_id, namespace, key), None) is not None

    async def list_namespace(self, tx, *, owner_id, namespace):
        self.calls.append(("list", None, owner_id, namespace))
        if self.fail == "list":
            raise RuntimeError("list failed")
        return [{"key": k, "value": v} for (o, n, k), v in self.rows.items() if o == owner_id and n == namespace]


class FakeBackend:
    def __init__(self, kv=None):
        self.state_kv = kv if kv is not None else FakeKV()
        self.transactions = 0

    def transactional(self):
        backend = self

        class CM:
            async def __aenter__(self):
                backend.transactions += 1
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return CM()


def manager(kv=None):
    return StateManager(backend=FakeBackend(kv))


# --------------------------------------------------------------------------- #
# get / set
# --------------------------------------------------------------------------- #
def test_missing_key_returns_the_documented_default():
    kv = FakeKV()
    assert run(manager(kv).get("absent", user=make_user())) is None
    assert kv.calls == [("get", "absent", "user-1", "ns")]


def test_no_backend_returns_none():
    assert run(StateManager().get("k", user=make_user())) is None


def test_set_writes_the_envelope_and_reads_back():
    kv = FakeKV()
    state = manager(kv)
    run(state.set("k", {"a": [1, 2]}, user=make_user()))
    assert kv.rows[("user-1", "ns", "k")] == PREFIX + json.dumps({"a": [1, 2]})
    assert run(state.get("k", user=make_user())) == {"a": [1, 2]}


def test_a_cold_manager_sees_the_same_value():
    kv = FakeKV()
    run(manager(kv).set("k", {"a": 1}, user=make_user()))
    assert run(manager(kv).get("k", user=make_user())) == {"a": 1}


def test_get_caches_and_does_not_hit_the_store_twice():
    kv = FakeKV({("user-1", "ns", "k"): PREFIX + "1"})
    state = manager(kv)
    assert run(state.get("k", user=make_user())) == 1
    assert run(state.get("k", user=make_user())) == 1
    assert [c[0] for c in kv.calls] == ["get"]


def test_scalars_round_trip_through_the_envelope():
    kv = FakeKV()
    state = manager(kv)
    for value in (0, 3.5, True, False, None, "text"):
        run(state.set("k", value, user=make_user()))
        assert run(state.get("k", user=make_user())) == value


def test_tuples_and_int_keys_are_canonicalised():
    state = manager()
    run(state.set("k", (1, 2), user=make_user()))
    assert run(state.get("k", user=make_user())) == [1, 2]
    run(state.set("k2", {1: "one"}, user=make_user()))
    assert run(state.get("k2", user=make_user())) == {"1": "one"}


def test_a_non_serialisable_value_fails_before_any_write():
    kv = FakeKV()
    state = manager(kv)
    with pytest.raises(TypeError):
        run(state.set("k", {1, 2, 3}, user=make_user()))
    assert kv.calls == []
    assert run(state.get("k", user=make_user())) is None


def test_a_durable_write_failure_propagates_and_leaves_the_cache_clean():
    kv = FakeKV()
    kv.fail = "set"
    state = manager(kv)
    with pytest.raises(RuntimeError, match="write failed"):
        run(state.set("k", {"a": 1}, user=make_user()))
    kv.fail = None
    assert run(state.get("k", user=make_user())) is None


def test_a_read_failure_returns_none_and_logs(caplog):
    kv = FakeKV()
    kv.fail = "get"
    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.state"):
        assert run(manager(kv).get("k", user=make_user())) is None
    assert "Error loading state key" in caplog.text


def test_in_memory_mode_uses_the_cache_as_the_store():
    state = StateManager()
    run(state.set("k", {"a": 1}, user=make_user()))
    assert run(state.get("k", user=make_user())) == {"a": 1}
    assert run(state.delete("k", user=make_user())) is False


# --------------------------------------------------------------------------- #
# scoping
# --------------------------------------------------------------------------- #
def test_namespaces_are_separate():
    kv = FakeKV()
    state = manager(kv)
    run(state.set("k", {"a": 1}, user=make_user()))
    assert run(state.get("k", user=make_user(namespace="other"))) is None
    assert kv.calls[-1] == ("get", "k", "user-1", "other")


def test_users_are_separate():
    kv = FakeKV()
    state = manager(kv)
    run(state.set("k", {"a": 1}, user=make_user()))
    assert run(state.get("k", user=make_user(user_id="user-2"))) is None


# --------------------------------------------------------------------------- #
# delete / list
# --------------------------------------------------------------------------- #
def test_delete_returns_true_then_false_and_drops_the_cache():
    kv = FakeKV()
    state = manager(kv)
    run(state.set("k", {"a": 1}, user=make_user()))
    assert run(state.delete("k", user=make_user())) is True
    assert run(state.get("k", user=make_user())) is None
    assert run(state.delete("k", user=make_user())) is False


def test_delete_without_a_backend_is_false():
    assert run(StateManager().delete("k", user=make_user())) is False


def test_delete_error_returns_false(caplog):
    kv = FakeKV()
    kv.fail = "delete"
    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.state"):
        assert run(manager(kv).delete("k", user=make_user())) is False
    assert "Error deleting state key" in caplog.text


def test_list_keys_returns_namespace_rows():
    kv = FakeKV({("user-1", "ns", "a"): PREFIX + "1", ("user-1", "ns", "b"): PREFIX + "2"})
    rows = run(manager(kv).list_keys(user=make_user()))
    assert sorted(row["key"] for row in rows) == ["a", "b"]


def test_list_keys_without_a_backend_is_empty():
    assert run(StateManager().list_keys(user=make_user())) == []


def test_list_keys_error_returns_empty(caplog):
    kv = FakeKV()
    kv.fail = "list"
    with caplog.at_level(logging.ERROR, logger="mnemos.domain.memory_categorization.state"):
        assert run(manager(kv).list_keys(user=make_user())) == []
    assert "Error listing state keys" in caplog.text


# --------------------------------------------------------------------------- #
# convenience accessors
# --------------------------------------------------------------------------- #
def test_load_identity_defaults_when_missing():
    default = run(manager().load_identity(user=make_user()))
    assert default == {"id": "unknown", "name": "Unknown User", "workspace": "default"}


def test_load_identity_returns_the_stored_value():
    kv = FakeKV()
    state = manager(kv)
    run(state.set("identity", {"id": "u", "name": "N", "workspace": "w"}, user=make_user()))
    assert run(state.load_identity(user=make_user())) == {"id": "u", "name": "N", "workspace": "w"}


def test_load_workspace_defaults_when_missing():
    assert run(manager().load_workspace(user=make_user())) == {
        "id": "default",
        "name": "Default Workspace",
        "projects": [],
    }


def test_load_today_defaults_are_generated_fresh():
    state = manager()
    first = run(state.load_today(user=make_user()))
    second = run(state.load_today(user=make_user()))
    assert set(first) == {"date", "day_of_week", "schedule", "events"}
    assert first["schedule"] == [] and first["events"] == []
    # Pinned: the default is regenerated per call, so the timestamps need not match.
    assert first["date"][:10] == second["date"][:10]
    assert first["day_of_week"] == second["day_of_week"]


def test_load_today_returns_the_stored_value():
    kv = FakeKV()
    state = manager(kv)
    run(
        state.set(
            "today", {"date": "2026-11-06", "day_of_week": "Friday", "schedule": [], "events": []}, user=make_user()
        )
    )
    assert run(state.load_today(user=make_user()))["day_of_week"] == "Friday"


# --------------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_sets_of_one_key_leave_exactly_one_winner():
    kv = FakeKV()
    state = manager(kv)

    async def go():
        await asyncio.gather(
            state.set("k", 1, user=make_user()),
            state.set("k", 2, user=make_user()),
        )
        return state._cache[("user-1", "ns", "k")]

    winner = run(go())
    assert winner in (1, 2)
    assert kv.rows[("user-1", "ns", "k")] == _wrap_state_value(winner)


def test_clear_cache_forces_a_store_read():
    kv = FakeKV({("user-1", "ns", "k"): PREFIX + "7"})
    state = manager(kv)
    assert run(state.get("k", user=make_user())) == 7
    state.clear_cache()
    assert run(state.get("k", user=make_user())) == 7
    assert [c[0] for c in kv.calls] == ["get", "get"]
