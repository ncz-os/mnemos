from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mnemos.persistence import hot_search
from mnemos.persistence.hot_search import HotSearchMixin


def test_cosine_rank_rows_uses_native_batch_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[float], list[list[float]]]] = []

    def batch_cosine_similarity(query, corpus):
        calls.append((query, corpus))
        return [1.0, 0.0]

    monkeypatch.setattr(hot_search, "_HOT_RS", SimpleNamespace(batch_cosine_similarity=batch_cosine_similarity))

    distances = HotSearchMixin()._cosine_rank_rows(
        [1.0, 0.0],
        [{"embedding": [1.0, 0.0]}, {"embedding": [0.0, 1.0]}],
        "embedding",
    )

    assert calls == [([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]])]
    assert distances == pytest.approx([0.0, 1.0])


def test_cosine_rank_rows_supports_cosine_batch_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    native = SimpleNamespace(cosine_batch=lambda _query, _corpus: [-1.0])
    monkeypatch.setattr(hot_search, "_HOT_RS", native)

    distances = HotSearchMixin()._cosine_rank_rows(
        [1.0, 0.0],
        [{"embedding": [-1.0, 0.0]}],
        "embedding",
    )

    assert distances == pytest.approx([2.0])


def test_cosine_rank_rows_falls_back_per_row_when_native_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_batch(_query, _corpus):
        raise RuntimeError("synthetic native failure")

    monkeypatch.setattr(hot_search, "_HOT_RS", SimpleNamespace(batch_cosine_similarity=broken_batch))
    rows = [
        {"embedding_json": "[1.0, 0.0]"},
        {"embedding_json": "[0.0, 1.0]"},
        {"embedding_json": "not json"},
        {"embedding_json": None},
    ]

    distances = HotSearchMixin()._cosine_rank_rows(
        [1.0, 0.0],
        rows,
        "embedding_json",
        extract_embedding=lambda value: json.loads(value) if value else None,
    )

    assert distances == pytest.approx([0.0, 1.0, 1.0, 1.0])


def test_cosine_rank_rows_lazily_loads_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    native = SimpleNamespace(batch_cosine_similarity=lambda _query, _corpus: [1.0])
    calls: list[str] = []
    monkeypatch.setattr(hot_search, "_HOT_RS", None)
    monkeypatch.setattr(hot_search, "_HOT_RS_LOAD_ATTEMPTED", False)
    monkeypatch.setattr(hot_search, "hot_rs_enabled", lambda: True)
    monkeypatch.setattr(
        hot_search,
        "load_hot_rs",
        lambda _logger, component: calls.append(component) or native,
    )

    mixin = HotSearchMixin()
    assert mixin._cosine_rank_rows([1.0], [{"embedding": [1.0]}], "embedding") == pytest.approx([0.0])
    assert mixin._cosine_rank_rows([1.0], [{"embedding": [1.0]}], "embedding") == pytest.approx([0.0])
    assert calls == ["MySQL/MariaDB cosine search"]


def test_cosine_rank_rows_does_not_load_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hot_search, "_HOT_RS", None)
    monkeypatch.setattr(hot_search, "_HOT_RS_LOAD_ATTEMPTED", False)
    monkeypatch.setattr(hot_search, "hot_rs_enabled", lambda: False)
    monkeypatch.setattr(
        hot_search,
        "load_hot_rs",
        lambda *_args: pytest.fail("disabled acceleration must not load the native module"),
    )

    distances = HotSearchMixin()._cosine_rank_rows(
        [1.0, 0.0],
        [{"embedding": [0.0, 1.0]}],
        "embedding",
    )

    assert distances == pytest.approx([1.0])
