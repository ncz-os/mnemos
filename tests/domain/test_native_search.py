from __future__ import annotations

import importlib
import math
import sys
import types

import pytest

from mnemos.domain.search import native_bridge


def test_pure_python_cosine_reference_cases() -> None:
    assert native_bridge.pure_python_cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert native_bridge.pure_python_cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert native_bridge.pure_python_cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert native_bridge.pure_python_cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert native_bridge.pure_python_cosine_similarity([1.0], [1.0, 2.0]) == 0.0


def test_batch_reference_matches_pairwise() -> None:
    query = [1.0, 0.0]
    corpus = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
    assert native_bridge.pure_python_batch_cosine_similarity(query, corpus) == pytest.approx([1.0, 0.0, -1.0])


def test_adapter_falls_back_when_native_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_cosine_similarity(_left, _right):
        raise RuntimeError("synthetic native failure")

    def broken_batch_cosine_similarity(_query, _corpus):
        raise RuntimeError("synthetic native failure")

    fake = types.SimpleNamespace(
        cosine_similarity=broken_cosine_similarity,
        batch_cosine_similarity=broken_batch_cosine_similarity,
    )
    monkeypatch.setitem(sys.modules, "mnemos_native_search", fake)
    bridge = importlib.reload(native_bridge)

    assert bridge.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert bridge.batch_cosine_similarity([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]]) == pytest.approx([1.0, 0.0])


def test_native_scores_match_python_reference() -> None:
    native = pytest.importorskip("mnemos_native_search")
    a = [math.sin(idx + 1) for idx in range(384)]
    b = [math.cos(idx + 7) for idx in range(384)]

    expected = native_bridge.pure_python_cosine_similarity(a, b)
    assert native.cosine_similarity(a, b) == pytest.approx(expected, abs=1e-6)
    assert native.cosine(a, b) == pytest.approx(expected, abs=1e-6)


def test_native_batch_scores_match_python_reference() -> None:
    native = pytest.importorskip("mnemos_native_search")
    query = [math.sin(idx + 1) for idx in range(384)]
    corpus = [[math.sin(idx + offset) for idx in range(384)] for offset in (1, 7, 13)]

    expected = native_bridge.pure_python_batch_cosine_similarity(query, corpus)
    assert native.batch_cosine_similarity(query, corpus) == pytest.approx(expected, abs=1e-6)
    assert native.cosine_batch(query, corpus) == pytest.approx(expected, abs=1e-6)


def test_native_numpy_batch_scores_match_python_reference() -> None:
    native = pytest.importorskip("mnemos_native_search")
    np = pytest.importorskip("numpy")
    query = [math.sin(idx + 1) for idx in range(384)]
    corpus = [[math.sin(idx + offset) for idx in range(384)] for offset in (1, 7, 13)]

    expected = native_bridge.pure_python_batch_cosine_similarity(query, corpus)
    query_np = np.asarray(query, dtype=np.float32)
    corpus_np = np.asarray(corpus, dtype=np.float32)
    assert native.batch_cosine_similarity(query_np, corpus_np) == pytest.approx(expected, abs=1e-6)


def test_similarity_dot_normalized_matches_python_reference_float64() -> None:
    native = pytest.importorskip("mnemos_native_search")
    np = pytest.importorskip("numpy")
    query = np.asarray([math.sin(idx + 1) for idx in range(1024)], dtype=np.float64)
    candidates = np.asarray(
        [[math.sin(idx + offset) for idx in range(1024)] for offset in range(1, 8)],
        dtype=np.float64,
    )

    expected = native_bridge.pure_python_similarity_dot_normalized(query, candidates)
    actual = native.similarity_dot_normalized(query, candidates)

    assert isinstance(actual, np.ndarray)
    assert actual.dtype == np.float64
    np.testing.assert_allclose(actual, expected, atol=1e-9, rtol=0.0)


def test_bridge_similarity_dot_normalized_falls_back_when_native_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    np = pytest.importorskip("numpy")

    def broken_similarity_dot_normalized(_query, _candidates):
        raise RuntimeError("synthetic native failure")

    fake = types.SimpleNamespace(similarity_dot_normalized=broken_similarity_dot_normalized)
    monkeypatch.setitem(sys.modules, "mnemos_native_search", fake)
    bridge = importlib.reload(native_bridge)

    query = np.asarray([1.0, 0.0], dtype=np.float64)
    candidates = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float64)
    np.testing.assert_allclose(
        bridge.similarity_dot_normalized(query, candidates),
        np.asarray([1.0, 0.0]),
        atol=1e-9,
        rtol=0.0,
    )
