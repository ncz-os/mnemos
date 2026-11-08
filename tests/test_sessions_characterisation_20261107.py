"""Characterisation of ``mnemos/api/routes/sessions.py`` (FB1120).

Pins what the five session endpoints do today: their shapes, the arguments the
handlers hand to the repository, and every error path. Behaviour that looks
wrong is pinned as it is and named as a divergence in the summary; nothing here
is a proposal to change the route.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes import sessions

CREATED = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
LAST = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def _row(**overrides):
    row = {
        "id": "sess_1",
        "user_id": "user-a",
        "namespace": "team-a",
        "created_at": CREATED,
        "last_activity": LAST,
        "message_count": 3,
        "total_tokens": 120,
        "model": "gpt-4o-mini",
    }
    row.update(overrides)
    return row


class _Repo:
    """Records every repository call and returns canned rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.row = _row()
        self.injections = ["mem_1", "mem_2"]
        self.history = [
            {
                "role": "user",
                "content": "hi",
                "timestamp": CREATED,
                "model": "gpt-4o-mini",
            }
        ]
        self.history_total = 1
        self.create_error: Exception | None = None
        self.message_ids = ["msg_user", "msg_assistant"]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def only(self, name: str) -> dict:
        matches = [args for called, args in self.calls if called == name]
        assert len(matches) == 1, f"expected one {name} call, got {self.calls}"
        return matches[0]

    async def create_session(self, tx, *, user_id, namespace, model, initial_context):
        self.calls.append(
            (
                "create_session",
                {
                    "user_id": user_id,
                    "namespace": namespace,
                    "model": model,
                    "initial_context": initial_context,
                },
            )
        )
        if self.create_error is not None:
            raise self.create_error
        return {"id": "sess_new", "created_at": CREATED, "model": model}

    async def get_session(self, tx, session_id, user_id, namespace):
        self.calls.append(
            (
                "get_session",
                {"session_id": session_id, "user_id": user_id, "namespace": namespace},
            )
        )
        return self.row

    async def list_injected_memory_ids(self, tx, session_id, limit=10):
        self.calls.append(("list_injected_memory_ids", {"session_id": session_id, "limit": limit}))
        return self.injections

    async def add_message(
        self,
        tx,
        *,
        session_id,
        role,
        content,
        model=None,
        tokens_used=None,
        memories_injected=None,
    ):
        self.calls.append(
            (
                "add_message",
                {
                    "session_id": session_id,
                    "role": role,
                    "content": content,
                    "model": model,
                    "tokens_used": tokens_used,
                    "memories_injected": memories_injected,
                },
            )
        )
        return self.message_ids.pop(0) if self.message_ids else "msg_x"

    async def fetch_provider_history(self, tx, session_id):
        self.calls.append(("fetch_provider_history", {"session_id": session_id}))
        return self.history

    async def add_memory_injections(self, tx, *, session_id, message_id, memory_ids):
        self.calls.append(
            (
                "add_memory_injections",
                {
                    "session_id": session_id,
                    "message_id": message_id,
                    "memory_ids": list(memory_ids),
                },
            )
        )

    async def update_metrics(self, tx, *, session_id, user_id, namespace, tokens_used):
        self.calls.append(
            (
                "update_metrics",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "namespace": namespace,
                    "tokens_used": tokens_used,
                },
            )
        )

    async def fetch_history(self, tx, session_id, limit, offset):
        self.calls.append(
            (
                "fetch_history",
                {"session_id": session_id, "limit": limit, "offset": offset},
            )
        )
        return self.history, self.history_total

    async def delete_session(self, tx, session_id, user_id, namespace):
        self.calls.append(
            (
                "delete_session",
                {"session_id": session_id, "user_id": user_id, "namespace": namespace},
            )
        )
        return True


class _Tx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


class _Backend:
    def __init__(self, repo: _Repo) -> None:
        self.sessions = repo

    def transactional(self, **kwargs):
        return _Tx()


def _client(
    monkeypatch: pytest.MonkeyPatch,
    repo: _Repo,
    *,
    backend: object | None = None,
    role: str = "user",
    namespace: str = "team-a",
) -> TestClient:
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend if backend is not None else _Backend(repo))
    monkeypatch.setattr(lifecycle, "_pool", None)
    user = UserContext(
        user_id="user-a",
        group_ids=[],
        role=role,
        namespace=namespace,
        authenticated=True,
    )
    app = FastAPI()
    app.include_router(sessions.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: str = "hello there",
    error: Exception | None = None,
    seen: dict | None = None,
):
    async def _route(*, model, messages, temperature, max_tokens, user):
        if seen is not None:
            seen.update(
                model=model,
                messages=[dict(message) for message in messages],
                temperature=temperature,
                max_tokens=max_tokens,
                user=user,
            )
        if error is not None:
            raise error
        return reply

    monkeypatch.setattr(sessions, "_route_to_provider", _route)


def _patch_search(
    monkeypatch: pytest.MonkeyPatch,
    *,
    docs: list[dict] | None = None,
    error: Exception | None = None,
    calls: list[dict] | None = None,
):
    async def _search(content, user, limit=3):
        if calls is not None:
            calls.append({"content": content, "limit": limit})
        if error is not None:
            raise error
        return docs or []

    monkeypatch.setattr(sessions, "_search_mnemos_context", _search)


# ── Router shape ─────────────────────────────────────────────────────────────


def test_the_router_is_the_five_v1_session_endpoints() -> None:
    assert sessions.router.prefix == "/v1/sessions"
    assert sessions.router.tags == ["sessions"]

    found = {(route.path, method) for route in sessions.router.routes for method in route.methods}

    assert found == {
        ("/v1/sessions", "POST"),
        ("/v1/sessions/{session_id}", "GET"),
        ("/v1/sessions/{session_id}", "DELETE"),
        ("/v1/sessions/{session_id}/messages", "POST"),
        ("/v1/sessions/{session_id}/history", "GET"),
    }


# ── POST /v1/sessions ────────────────────────────────────────────────────────


def test_create_session_defaults_the_model_to_gpt_4o(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    response = client.post("/v1/sessions", json={})

    assert response.status_code == 200, response.text
    assert repo.only("create_session") == {
        "user_id": "user-a",
        "namespace": "team-a",
        "model": "gpt-4o",
        "initial_context": None,
    }


def test_create_session_passes_the_body_model_and_initial_context_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    client.post(
        "/v1/sessions",
        json={"model": "claude-x", "initial_context": "alice context"},
    )

    assert repo.only("create_session") == {
        "user_id": "user-a",
        "namespace": "team-a",
        "model": "claude-x",
        "initial_context": "alice context",
    }


def test_create_session_answers_with_the_row_id_iso_created_at_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    body = client.post("/v1/sessions", json={"model": "m"}).json()

    assert body == {
        "session_id": "sess_new",
        "created_at": CREATED.isoformat(),
        "model": "m",
    }


def test_create_session_stringifies_a_created_at_that_has_no_isoformat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()

    async def create_session(tx, **_kwargs):
        return {"id": "sess_s", "created_at": "2026-01-01T00:00:00", "model": "m"}

    monkeypatch.setattr(repo, "create_session", create_session)
    client = _client(monkeypatch, repo)

    assert client.post("/v1/sessions", json={}).json()["created_at"] == "2026-01-01T00:00:00"


def test_create_session_turns_a_repository_error_into_a_500_that_echoes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.create_error = RuntimeError("boom")
    client = _client(monkeypatch, repo)

    response = client.post("/v1/sessions", json={})

    assert response.status_code == 500
    assert response.json()["detail"] == "Session creation failed: boom"


def test_every_route_answers_503_when_no_backend_is_booted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)
    monkeypatch.setattr(lifecycle, "_persistence_backend", None)

    response = client.get("/v1/sessions/sess_1")

    assert response.status_code == 503
    assert response.json()["detail"] == "Persistence backend not available"


def test_a_backend_without_a_sessions_repository_is_a_503_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    # A bare object advertises no capabilities and has no ``sessions`` accessor.
    client = _client(monkeypatch, repo, backend=object())

    response = client.get("/v1/sessions/sess_1")

    assert response.status_code == 503
    assert response.json()["detail"] == "persistence backend does not support 'sessions' for object"


# ── GET /v1/sessions/{session_id} ────────────────────────────────────────────


def test_get_session_returns_the_session_context_with_its_injected_memory_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    body = client.get("/v1/sessions/sess_1").json()

    assert body == {
        "session_id": "sess_1",
        "user_id": "user-a",
        "created_at": CREATED.isoformat(),
        "last_activity": LAST.isoformat(),
        "message_count": 3,
        "total_tokens": 120,
        "model": "gpt-4o-mini",
        "injected_memories": ["mem_1", "mem_2"],
    }


def test_get_session_scopes_the_lookup_to_the_callers_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    client.get("/v1/sessions/sess_1")

    assert repo.only("get_session") == {
        "session_id": "sess_1",
        "user_id": "user-a",
        "namespace": "team-a",
    }


def test_root_may_look_a_session_up_in_another_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo, role="root")

    assert client.get("/v1/sessions/sess_1?namespace=team-b").status_code == 200
    assert repo.only("get_session")["namespace"] == "team-b"


def test_a_non_root_namespace_override_is_a_403_and_never_reaches_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    response = client.get("/v1/sessions/sess_1?namespace=team-b")

    assert response.status_code == 403
    assert response.json()["detail"] == "cross-namespace access requires root"
    assert repo.calls == []


def test_get_session_404s_a_row_the_repository_did_not_find(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.row = None
    client = _client(monkeypatch, repo)

    response = client.get("/v1/sessions/nope")

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"
    # The injections read runs BEFORE the None check, so a missing session still
    # costs one repository round trip. Pinned as observed.
    assert repo.names() == ["get_session", "list_injected_memory_ids"]


# ── POST /v1/sessions/{session_id}/messages ──────────────────────────────────


def test_add_message_404s_before_storing_anything_when_the_session_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.row = None
    seen = {"called": False}

    async def _route(**_kwargs):
        seen["called"] = True
        return "unused"

    monkeypatch.setattr(sessions, "_route_to_provider", _route)
    client = _client(monkeypatch, repo)

    response = client.post("/v1/sessions/nope/messages", json={"role": "user", "content": "hi"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"
    assert repo.names() == ["get_session"]
    assert seen["called"] is False


def test_add_message_stores_the_user_message_with_the_requested_role_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _patch_provider(monkeypatch)
    _patch_search(monkeypatch)
    client = _client(monkeypatch, repo)

    client.post(
        "/v1/sessions/sess_1/messages",
        json={"role": "assistant", "content": "hello", "model": "override-model"},
    )

    first_add = [args for called, args in repo.calls if called == "add_message"][0]
    assert first_add == {
        "session_id": "sess_1",
        "role": "assistant",
        "content": "hello",
        "model": "override-model",
        "tokens_used": None,
        "memories_injected": None,
    }


def test_add_message_falls_back_to_user_role_and_the_session_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _patch_provider(monkeypatch)
    _patch_search(monkeypatch)
    client = _client(monkeypatch, repo)

    client.post("/v1/sessions/sess_1/messages", json={"role": "", "content": "hello"})

    first_add = [args for called, args in repo.calls if called == "add_message"][0]
    assert first_add["role"] == "user"
    assert first_add["model"] == "gpt-4o-mini"


def test_add_message_hands_the_provider_the_stored_history_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.history = [
        {"role": "system", "content": "rules", "timestamp": CREATED, "model": "m"},
        {"role": "user", "content": "hi", "timestamp": CREATED, "model": "m"},
    ]
    seen: dict = {}
    _patch_provider(monkeypatch, seen=seen)
    _patch_search(monkeypatch)
    client = _client(monkeypatch, repo)

    client.post("/v1/sessions/sess_1/messages", json={"role": "user", "content": "next"})

    assert seen["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hi"},
    ]
    assert seen["temperature"] == 0.7
    assert seen["max_tokens"] is None
    assert seen["model"] == "gpt-4o-mini"
    assert seen["user"].user_id == "user-a"


def test_add_message_inserts_a_system_prompt_when_the_history_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.history = [
        {"role": "user", "content": "hi", "timestamp": CREATED, "model": "m"},
    ]
    seen: dict = {}
    _patch_provider(monkeypatch, seen=seen)
    _patch_search(monkeypatch, docs=[{"id": "mem_1", "content": "x" * 600}])
    client = _client(monkeypatch, repo)

    client.post("/v1/sessions/sess_1/messages", json={"role": "user", "content": "q"})

    assert seen["messages"][0]["role"] == "system"
    header, _, body = seen["messages"][0]["content"].partition("[Memory]\n")
    assert header == "[MNEMOS Context - 1 memories]\n"
    # Each injected document is truncated to 500 characters.
    assert body == "x" * 500
    assert seen["messages"][1] == {"role": "user", "content": "hi"}


def test_add_message_appends_context_to_the_first_message_when_a_system_row_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The handler appends to ``messages[0]`` after testing that SOME row is a
    # system row, so an ordering where the system row is not first appends the
    # memory context to a user message. Pinned as observed, not repaired.
    repo = _Repo()
    repo.history = [
        {"role": "user", "content": "hi", "timestamp": CREATED, "model": "m"},
        {"role": "system", "content": "late rules", "timestamp": CREATED, "model": "m"},
    ]
    seen: dict = {}
    _patch_provider(monkeypatch, seen=seen)
    _patch_search(monkeypatch, docs=[{"id": "mem_1", "content": "ctx"}])
    client = _client(monkeypatch, repo)

    client.post("/v1/sessions/sess_1/messages", json={"role": "user", "content": "q"})

    assert seen["messages"][0] == {
        "role": "user",
        "content": "hi\n\n[MNEMOS Context - 1 memories]\n[Memory]\nctx",
    }
    assert seen["messages"][1] == {"role": "system", "content": "late rules"}
    assert len(seen["messages"]) == 2


def test_add_message_records_one_injection_row_per_retrieved_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _patch_provider(monkeypatch)
    _patch_search(
        monkeypatch,
        docs=[{"id": "mem_1", "content": "a"}, {"content": "b"}],
    )
    client = _client(monkeypatch, repo)

    client.post("/v1/sessions/sess_1/messages", json={"role": "user", "content": "q"})

    assert repo.only("add_memory_injections") == {
        "session_id": "sess_1",
        "message_id": "msg_user",
        "memory_ids": ["mem_1", "doc_1"],
    }


def test_add_message_survives_a_failed_memory_search_and_says_zero_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _patch_provider(monkeypatch, reply="answer")
    _patch_search(monkeypatch, error=RuntimeError("search down"))
    client = _client(monkeypatch, repo)

    response = client.post("/v1/sessions/sess_1/messages", json={"role": "user", "content": "q"})

    assert response.status_code == 200, response.text
    assert response.json()["memories_injected"] == 0
    assert "add_memory_injections" not in repo.names()


def test_add_message_turns_a_provider_failure_into_a_503_and_stores_no_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _patch_provider(monkeypatch, error=RuntimeError("no route"))
    _patch_search(monkeypatch)
    client = _client(monkeypatch, repo)

    response = client.post("/v1/sessions/sess_1/messages", json={"role": "user", "content": "q"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Provider unavailable: no route"
    # The user message was already committed before the provider was called;
    # the assistant message and the metric update never happened.
    assert repo.names() == ["get_session", "add_message", "fetch_provider_history"]


def test_add_message_estimates_tokens_as_a_quarter_of_the_reply_length_and_bills_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _patch_provider(monkeypatch, reply="x" * 41)
    _patch_search(monkeypatch)
    client = _client(monkeypatch, repo)

    body = client.post("/v1/sessions/sess_1/messages", json={"role": "user", "content": "q"}).json()

    assert body["tokens_used"] == 10
    assert body["message_id"] == "msg_assistant"
    assert body["role"] == "assistant"
    assert body["content"] == "x" * 41
    assert body["session_id"] == "sess_1"
    assert body["model"] == "gpt-4o-mini"

    assistant_add = [args for called, args in repo.calls if called == "add_message"][1]
    assert assistant_add == {
        "session_id": "sess_1",
        "role": "assistant",
        "content": "x" * 41,
        "model": "gpt-4o-mini",
        "tokens_used": 10,
        "memories_injected": 0,
    }
    assert repo.only("update_metrics") == {
        "session_id": "sess_1",
        "user_id": "user-a",
        "namespace": "team-a",
        "tokens_used": 10,
    }


def test_add_message_counts_injected_memories_and_bills_the_root_namespace_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _patch_provider(monkeypatch, reply="abcd")
    _patch_search(monkeypatch, docs=[{"id": "m1", "content": "a"}, {"id": "m2", "content": "b"}])
    client = _client(monkeypatch, repo, role="root")

    body = client.post(
        "/v1/sessions/sess_1/messages?namespace=team-b",
        json={"role": "user", "content": "q"},
    ).json()

    assert body["memories_injected"] == 2
    assert body["tokens_used"] == 1
    assert repo.only("update_metrics")["namespace"] == "team-b"
    assert repo.only("get_session")["namespace"] == "team-b"


def test_add_message_searches_memory_with_the_message_text_and_limit_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    calls: list[dict] = []
    _patch_provider(monkeypatch)
    _patch_search(monkeypatch, calls=calls)
    client = _client(monkeypatch, repo)

    client.post("/v1/sessions/sess_1/messages", json={"role": "user", "content": "find this"})

    assert calls == [{"content": "find this", "limit": 3}]


# ── GET /v1/sessions/{session_id}/history ────────────────────────────────────


def test_history_404s_without_reading_messages_when_the_session_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.row = None
    client = _client(monkeypatch, repo)

    response = client.get("/v1/sessions/nope/history")

    assert response.status_code == 404
    assert repo.names() == ["get_session"]


def test_history_returns_messages_with_converted_timestamps_and_session_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.history = [
        {"role": "user", "content": "a", "timestamp": CREATED, "model": "m"},
        {"role": "assistant", "content": "b", "timestamp": "2026-01-01", "model": None},
        {"role": "assistant", "content": "c", "timestamp": None, "model": "m"},
    ]
    repo.history_total = 9
    client = _client(monkeypatch, repo)

    body = client.get("/v1/sessions/sess_1/history").json()

    assert body == {
        "session_id": "sess_1",
        "messages": [
            {"role": "user", "content": "a", "timestamp": CREATED.isoformat(), "model": "m"},
            {"role": "assistant", "content": "b", "timestamp": "2026-01-01", "model": None},
            {"role": "assistant", "content": "c", "timestamp": None, "model": "m"},
        ],
        "total_messages": 9,
        "total_tokens": 120,
        "created_at": CREATED.isoformat(),
    }


def test_history_defaults_to_limit_fifty_offset_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    client.get("/v1/sessions/sess_1/history")

    assert repo.only("fetch_history") == {"session_id": "sess_1", "limit": 50, "offset": 0}


def test_history_passes_the_window_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    client.get("/v1/sessions/sess_1/history?limit=500&offset=7")

    assert repo.only("fetch_history") == {"session_id": "sess_1", "limit": 500, "offset": 7}


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=501", "limit=-1", "offset=-1", "limit=abc"],
)
def test_history_window_outside_its_bounds_is_a_422(monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    response = client.get(f"/v1/sessions/sess_1/history?{query}")

    assert response.status_code == 422
    assert repo.calls == []


# ── DELETE /v1/sessions/{session_id} ─────────────────────────────────────────


def test_delete_404s_a_missing_session_without_deleting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.row = None
    client = _client(monkeypatch, repo)

    response = client.delete("/v1/sessions/nope")

    assert response.status_code == 404
    assert repo.names() == ["get_session"]


def test_delete_scopes_the_delete_to_the_caller_and_answers_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    response = client.delete("/v1/sessions/sess_1")

    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "session_id": "sess_1"}
    assert repo.only("delete_session") == {
        "session_id": "sess_1",
        "user_id": "user-a",
        "namespace": "team-a",
    }


def test_delete_refuses_a_cross_namespace_non_root_before_touching_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    client = _client(monkeypatch, repo)

    response = client.delete("/v1/sessions/sess_1?namespace=team-b")

    assert response.status_code == 403
    assert repo.calls == []
