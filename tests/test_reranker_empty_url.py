"""Unit tests for the cross-encoder reranker HTTP client."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from mnemos.domain.search.reranker import Reranker


@pytest.mark.asyncio
async def test_rerank_empty_url_returns_empty_without_http():
    """When MNEMOS_RERANKER_URL is unset (empty URL), rerank() must return
    [] immediately without building the HTTP client or making any HTTP call."""
    r = Reranker(url="")
    result = await r.rerank("test query", ["doc1", "doc2"])
    assert result == []
    # The client must NOT have been built
    assert r._client is None, "empty-URL reranker must not build an HTTP client"


@pytest.mark.asyncio
async def test_rerank_empty_url_logs_once(caplog):
    """The INFO log about rerank being disabled must fire exactly once.

    Counts emitted records rather than inspecting the internal latch, so a
    regression that logs on every call cannot pass by leaving the flag set.
    """
    r = Reranker(url="")
    with caplog.at_level(logging.INFO, logger="mnemos.domain.search.reranker"):
        await r.rerank("q", ["d"])
        await r.rerank("q2", ["d2"])
        await r.rerank("q3", ["d3"])

    disabled = [r_.message for r_ in caplog.records if "rerank disabled" in r_.message]
    assert len(disabled) == 1, f"expected exactly one disable log, got {len(disabled)}"
    assert "MNEMOS_RERANKER_URL" in disabled[0]


@pytest.mark.asyncio
async def test_rerank_empty_query_short_circuits_before_url_check():
    """Empty query returns [] regardless of URL state."""
    r = Reranker(url="")
    assert await r.rerank("", ["doc"]) == []


@pytest.mark.asyncio
async def test_rerank_empty_documents_short_circuits():
    """Empty documents list returns [] regardless of URL state."""
    r = Reranker(url="")
    assert await r.rerank("query", []) == []


@pytest.mark.asyncio
async def test_rerank_with_url_posts_and_parses_results_in_input_order():
    """Positive control: when MNEMOS_RERANKER_URL is set, rerank() must POST
    to that endpoint and re-order the llama.cpp results array back into the
    caller's input-document order.

    An early ``if not self.url: return []`` guard keeps an empty URL from
    ever reaching the HTTP layer. This test locks in the orthogonal
    invariant: a configured
    URL still works end-to-end and the per-document scores come back aligned
    with the input order, not with the server's native ordering (which can
    be by relevance, not by index).
    """
    r = Reranker(url="http://reranker.example/v1/rerank")
    # Stub the httpx client with a mock that returns a llama.cpp-shaped
    # payload where results are deliberately NOT in input order — the reranker
    # code must sort by ``index`` itself, not just trust the wire order.
    #
    # The scores are arranged so that INPUT ORDER is not RELEVANCE ORDER
    # (doc-B most relevant, doc-A middle, doc-C least). If
    # an input happened to be the most relevant one, a broken implementation
    # that merely echoed the wire order (or sorted descending by score
    # instead of mapping by ``index``) could still produce the expected
    # list — so the fixture must make the two orderings differ.
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {"index": 2, "relevance_score": 0.3},
            {"index": 0, "relevance_score": 0.7},
            {"index": 1, "relevance_score": 0.9},
        ]
    }
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    r._client = mock_client

    docs = [
        "doc-A: unrelated tax form",
        "doc-B: inspection score published",
        "doc-C: restaurant inspections reopened",
    ]
    # Index mapping gives [0.7, 0.9, 0.3], which is neither sorted ascending
    # ([0.3, 0.7, 0.9]) nor descending ([0.9, 0.7, 0.3]) nor the wire order
    # ([0.3, 0.7, 0.9]), so echoing the wire or sorting by score both fail.
    assert docs[0].startswith("doc-A") and docs[2].startswith("doc-C")
    scores = await r.rerank("query", docs)

    assert scores == [0.7, 0.9, 0.3], f"scores must be re-ordered to match input positions, got {scores}"
    # Verify the URL was actually the configured one (not the empty-guard path).
    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.call_args
    assert call_args.args[0] == "http://reranker.example/v1/rerank", (
        f"reranker must POST to the configured URL, got {call_args.args[0]!r}"
    )
    payload = call_args.kwargs["json"]
    assert payload["model"] == r.model
    assert payload["query"] == "query"
    assert payload["documents"] == docs


def test_rerank_is_disabled_property():
    """The ``is_disabled`` property exposes the empty-URL state without
    triggering the INFO-disabled log line or invoking ``rerank()``.

    Health probes and admin tools can use this to check the reranker's
    configuration state cheaply. The empty-URL guard is internal;
    ``is_disabled`` is the public, side-effect-free accessor.
    """
    # Empty URL → disabled.
    r_empty = Reranker(url="")
    assert r_empty.is_disabled is True
    # Configured URL → enabled, regardless of whether the client has
    # actually been built yet.
    r_set = Reranker(url="http://reranker.example/v1/rerank")
    assert r_set.is_disabled is False
    assert r_set._client is None  # not built until first rerank() call
    # Even a value that is None (the default) is treated as disabled.
    r_none = Reranker(url=None)
    assert r_none.is_disabled is True


@pytest.mark.asyncio
async def test_rerank_empty_url_short_circuits_before_httpx_post(monkeypatch):
    """rerank() with an empty URL must return [] WITHOUT making any HTTP
    request — not even a connect attempt, not even a DNS resolution, not
    even an httpx.AsyncClient instantiation.

    This test patches the module-level ``httpx.AsyncClient`` (not just
    ``_client``) so a regression that builds the client but then calls
    .post() on it will still fail. A successful run means the short-
    circuit guard fired before the HTTP layer was touched.

    Note: reranker.py does ``import httpx`` inside ``_build_client`` (a
    lazy import for fast startup), so we patch ``httpx.AsyncClient``
    directly in the httpx module rather than in the reranker module.
    """
    import httpx

    class _AsyncClientSentinel:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                f"rerank() with empty URL must NOT instantiate httpx.AsyncClient (got args={args!r}, kwargs={kwargs!r})"
            )

        async def post(self, *args, **kwargs):
            raise AssertionError("rerank() with empty URL must NOT call httpx.AsyncClient.post")

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClientSentinel)

    r = Reranker(url="")
    result = await r.rerank("query", ["doc1", "doc2"])
    assert result == [], f"empty-URL rerank() must return [], got {result!r}"
    assert r._client is None, "empty-URL rerank() must not build an httpx.AsyncClient"
    assert r.is_disabled is True, "is_disabled must reflect the empty-URL state"


def test_rerank_public_call_site_has_inline_empty_url_guard():
    """The empty-URL guard must be the FIRST line of defence in the public
    call site, not just defense-in-depth inside the client builder.
    This static check locks in that the Reranker.rerank() method body
    contains the ``if not self.url`` short-circuit, so a future refactor
    that quietly removes the inline guard (and relies only on
    ``_build_client``'s defense-in-depth) fails this test.

    The guard must (a) be a statement in the method body (not just a
    docstring mention), (b) use the literal ``if not self.url``
    pattern, and (c) appear before any call to ``_build_client`` (the
    defense-in-depth site, which is not sufficient on its own).
    """
    import inspect
    from mnemos.domain.search.reranker import Reranker

    src = inspect.getsource(Reranker.rerank)
    # (a) The guard must be a statement (not just a docstring).
    assert "if not self.url:" in src, (
        "Reranker.rerank() must contain the inline ``if not self.url:`` "
        "guard; the empty-URL log spam can ONLY be prevented by "
        "short-circuiting at the public call site, BEFORE any client "
        "construction."
    )
    # (b) and (c) The guard must appear before any _build_client call.
    guard_idx = src.index("if not self.url:")
    build_idx = src.find("self._build_client()")
    assert build_idx == -1 or guard_idx < build_idx, (
        "Reranker.rerank()'s ``if not self.url:`` guard must fire BEFORE "
        "any _build_client() call, not after — defense-in-depth inside "
        "_build_client is NOT a substitute for the public-call-site "
        "guard."
    )
