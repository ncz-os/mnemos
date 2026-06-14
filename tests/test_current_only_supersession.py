from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnemos.api.dependencies import UserContext
import mnemos.api.routes.memories as memories_handler
from mnemos.domain.models import MemoryListRequest, MemorySearchRequest
from mnemos.domain.search.decay import apply_decay
from tests._fake_backend import _FakeBackend


def _root() -> UserContext:
    return UserContext(
        user_id="admin",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _row(memory_id: str, *, consolidated_into: str | None = None):
    return {
        "id": memory_id,
        "content": f"{memory_id} needle",
        "category": "facts",
        "subcategory": None,
        "created": "2026-06-14T00:00:00+00:00",
        "updated": "2026-06-14T00:00:00+00:00",
        "metadata": {},
        "quality_rating": 50,
        "compressed_content": None,
        "verbatim_content": None,
        "owner_id": "root",
        "group_id": None,
        "namespace": "default",
        "permission_mode": 4,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "archived_at": None,
        "consolidated_into": consolidated_into,
    }


def test_request_models_default_back_compat_and_alias_flags():
    search = MemorySearchRequest(query="needle")
    listing = MemoryListRequest()

    assert search.exclude_superseded is False
    assert search.current_only is False
    assert listing.exclude_superseded is False
    assert listing.current_only is False


def test_apply_decay_can_filter_superseded_rows():
    current = SimpleNamespace(id="current", category="facts", created="2026-06-14T00:00:00+00:00", score=0.4)
    stale = SimpleNamespace(
        id="stale",
        category="facts",
        created="2026-06-14T00:00:00+00:00",
        score=1.0,
        superseded_by="current",
    )

    assert [m.id for m in apply_decay([stale, current], {}, exclude_superseded=True)] == ["current"]


@pytest.mark.asyncio
async def test_search_route_passes_current_only_to_backend(monkeypatch):
    backend = _FakeBackend()
    backend.memories.configure_return("fts_search", [_row("current")])
    monkeypatch.setattr(memories_handler, "_backend_or_503", lambda: backend)
    monkeypatch.setattr(memories_handler, "load_decay_table", lambda _backend: {})
    memories_handler._lc._cache = None

    response = await memories_handler.search_memories(
        MemorySearchRequest(query="needle", semantic=False, current_only=True),
        user=_root(),
    )

    assert [m.id for m in response.memories] == ["current"]
    assert backend.memories.calls[-1][0] == "fts_search"
    assert backend.memories.calls[-1][1]["exclude_superseded"] is True


@pytest.mark.asyncio
async def test_list_route_passes_exclude_superseded_to_backend(monkeypatch):
    backend = _FakeBackend()
    backend.memories.configure_return("list_memories", ([_row("current")], 1))
    monkeypatch.setattr(memories_handler, "_backend_or_503", lambda: backend)

    response = await memories_handler.list_memories(exclude_superseded=True, limit=20, offset=0, user=_root())

    assert [m.id for m in response.memories] == ["current"]
    assert backend.memories.calls[-1][0] == "list_memories"
    assert backend.memories.calls[-1][1]["exclude_superseded"] is True


@pytest.mark.parametrize(
    "repo_path, expected",
    [
        ("mnemos.persistence.postgres.PostgresMemoryRepository", "consolidated_into IS NULL"),
        ("mnemos.persistence.sqlite.SqliteMemoryRepository", "consolidated_into IS NULL"),
        ("mnemos.persistence.mysql.MysqlMemoryRepository", "m.consolidated_into IS NULL"),
        ("mnemos.persistence.db2.Db2MemoryRepository", "m.consolidated_into IS NULL"),
        ("mnemos.persistence.oracle.OracleMemoryRepository", "m.consolidated_into IS NULL"),
    ],
)
def test_backend_current_only_predicates_are_wired(repo_path, expected):
    import inspect

    module_name, class_name = repo_path.rsplit(".", 1)
    module = __import__(module_name, fromlist=[class_name])
    repo_cls = getattr(module, class_name)

    for method_name in ("list_memories", "semantic_search", "fts_search"):
        source = inspect.getsource(getattr(repo_cls, method_name))
        assert "exclude_superseded: bool = False" in source
        assert "if exclude_superseded" in source
        assert expected in source
