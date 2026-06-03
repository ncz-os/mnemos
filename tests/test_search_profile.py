"""Unit tests for v6.2 M-2.2.3 retrieval-profile resolver + handler wiring."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest
from mnemos.domain.search.profile import SearchProfile, resolve_profile

from tests._fake_backend import install_fake_backend


_TS = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _RecordingReranker:
    def __init__(self, scores: list[float]) -> None:
        self._scores = list(scores)
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        return list(self._scores)


async def _noop_bump_recall_counters(_memory_ids: list[str]) -> None:
    return None


async def _empty_decay_table(_backend) -> dict:
    return {}


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _memory_row(memory_id: str, content: str) -> dict:
    return {
        "id": memory_id,
        "content": content,
        "category": "facts",
        "subcategory": None,
        "created": _TS,
        "updated": _TS,
        "metadata": {},
        "quality_rating": 80,
        "compressed_content": None,
        "verbatim_content": content,
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }


def _memory_call_names(backend) -> list[str]:
    return [name for name, _kwargs in backend.memories.calls]


def test_default_when_none():
    assert resolve_profile(None) is SearchProfile.BALANCED


def test_default_when_empty():
    assert resolve_profile("") is SearchProfile.BALANCED


def test_fast():
    assert resolve_profile("fast") is SearchProfile.FAST


def test_balanced():
    assert resolve_profile("balanced") is SearchProfile.BALANCED


def test_deep():
    assert resolve_profile("deep") is SearchProfile.DEEP


def test_unknown_rejected():
    with pytest.raises(ValueError, match="unknown retrieval profile"):
        resolve_profile("turbo")


def test_case_sensitive():
    # SearchProfile values are lowercase per spec; uppercase rejected.
    with pytest.raises(ValueError):
        resolve_profile("FAST")


def test_custom_default():
    assert resolve_profile(None, default=SearchProfile.FAST) is SearchProfile.FAST


def test_explicit_override_custom_default():
    assert resolve_profile("deep", default=SearchProfile.FAST) is SearchProfile.DEEP


@pytest.mark.asyncio
async def test_search_memories_deep_profile_uses_reranker(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "fts_search",
        [
            _memory_row("mem_a", "first hit"),
            _memory_row("mem_b", "second hit"),
        ],
    )
    reranker = _RecordingReranker([0.1, 0.9])
    monkeypatch.setattr(memories_handler, "get_reranker", lambda: reranker)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump_recall_counters)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay_table)

    response = await memories_handler.search_memories(
        MemorySearchRequest(query="needle", limit=10, semantic=False, profile="deep"),
        user=_alice(),
    )
    await asyncio.sleep(0)

    assert [memory.id for memory in response.memories] == ["mem_b", "mem_a"]
    assert reranker.calls == [("needle", ["first hit", "second hit"])]
    assert _memory_call_names(backend) == ["fts_search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "balanced", "fast"])
async def test_search_memories_non_deep_profiles_skip_reranker(monkeypatch, profile):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "fts_search",
        [
            _memory_row("mem_a", "first hit"),
            _memory_row("mem_b", "second hit"),
        ],
    )
    reranker = _RecordingReranker([0.9, 0.1])
    monkeypatch.setattr(memories_handler, "get_reranker", lambda: reranker)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump_recall_counters)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay_table)

    response = await memories_handler.search_memories(
        MemorySearchRequest(query="needle", limit=10, semantic=False, profile=profile),
        user=_alice(),
    )
    await asyncio.sleep(0)

    assert [memory.id for memory in response.memories] == ["mem_a", "mem_b"]
    assert reranker.calls == []
    assert _memory_call_names(backend) == ["fts_search"]


@pytest.mark.asyncio
async def test_search_memories_unknown_profile_maps_to_http_400(monkeypatch):
    backend = install_fake_backend(monkeypatch)

    with pytest.raises(HTTPException, match="unknown retrieval profile") as exc:
        await memories_handler.search_memories(
            MemorySearchRequest(query="needle", limit=10, semantic=False, profile="turbo"),
            user=_alice(),
        )

    assert exc.value.status_code == 400
    assert backend.memories.calls == []
