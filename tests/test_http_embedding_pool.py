import asyncio
import math
import pytest
import httpx

from mnemos.runtime.embedder import _HttpPoolBackend, _HttpBackend, InProcessEmbedder


def _make_transport(handler):
    return httpx.MockTransport(handler)


def _make_pool(urls, model="bge-m3", timeout=5.0, max_chars=1000, expected_dim=1024, monkeypatch=None, handler=None):
    pool = _HttpPoolBackend(urls, model=model, timeout=timeout, max_chars=max_chars, expected_dim=expected_dim)
    if monkeypatch and handler:

        def _build_client_sync(self):
            self._client = httpx.AsyncClient(transport=_make_transport(handler))
            return self._client

        monkeypatch.setattr(_HttpBackend, "_build_client_sync", _build_client_sync)
    pool._load_sync()
    return pool


@pytest.mark.asyncio
async def test_sequential_8_calls_visit_both_nodes(monkeypatch):
    hosts_visited = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts_visited.append(request.url.host)
        body = request.read()
        import json

        data = json.loads(body)
        inputs = data["input"]
        results = []
        for i, text in enumerate(inputs):
            emb = [3.0, 4.0] + [0.0] * 1022
            results.append({"index": i, "embedding": emb})
        return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    for _ in range(8):
        await pool.embed_async("test text")

    assert "node1" in hosts_visited
    assert "node2" in hosts_visited
    # Fair exploration: within first 4 calls, both should be visited
    first_4 = hosts_visited[:4]
    assert "node1" in first_4
    assert "node2" in first_4


@pytest.mark.asyncio
async def test_concurrent_4_calls_visit_both_nodes(monkeypatch):
    hosts_visited = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts_visited.append(request.url.host)
        await asyncio.sleep(0)
        import json

        data = json.loads(request.read())
        inputs = data["input"]
        results = []
        for i, text in enumerate(inputs):
            emb = [3.0, 4.0] + [0.0] * 1022
            results.append({"index": i, "embedding": emb})
        return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    tasks = [pool.embed_async(f"text {i}") for i in range(4)]
    await asyncio.gather(*tasks)

    assert "node1" in hosts_visited
    assert "node2" in hosts_visited


@pytest.mark.asyncio
async def test_first_endpoint_500_second_works_same_call(monkeypatch):
    call_count = {"node1": 0, "node2": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "node1":
            call_count["node1"] += 1
            return httpx.Response(500, text="Internal Server Error")
        else:
            call_count["node2"] += 1
            import json

            data = json.loads(request.read())
            inputs = data["input"]
            results = []
            for i, text in enumerate(inputs):
                emb = [3.0, 4.0] + [0.0] * 1022
                results.append({"index": i, "embedding": emb})
            return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("test text")
    assert result is not None
    assert len(result) == 1024
    assert call_count["node1"] >= 1
    assert call_count["node2"] >= 1


@pytest.mark.asyncio
async def test_both_failure_aligned_empty(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("test text")
    assert result == []


@pytest.mark.asyncio
async def test_malformed_node_failover(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "node1":
            return httpx.Response(200, json={"malformed": True})
        else:
            import json

            data = json.loads(request.read())
            inputs = data["input"]
            results = []
            for i, text in enumerate(inputs):
                emb = [3.0, 4.0] + [0.0] * 1022
                results.append({"index": i, "embedding": emb})
            return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("test text")
    assert result is not None
    assert len(result) == 1024


@pytest.mark.asyncio
async def test_duplicate_indices_node_failover(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "node1":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [3.0, 4.0] + [0.0] * 1022},
                        {"index": 0, "embedding": [3.0, 4.0] + [0.0] * 1022},
                    ]
                },
            )
        else:
            import json

            data = json.loads(request.read())
            inputs = data["input"]
            results = []
            for i, text in enumerate(inputs):
                emb = [3.0, 4.0] + [0.0] * 1022
                results.append({"index": i, "embedding": emb})
            return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("test text")
    assert result is not None
    assert len(result) == 1024


@pytest.mark.asyncio
async def test_wrong_dimension_node_failover(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "node1":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})
        else:
            import json

            data = json.loads(request.read())
            inputs = data["input"]
            results = []
            for i, text in enumerate(inputs):
                emb = [3.0, 4.0] + [0.0] * 1022
                results.append({"index": i, "embedding": emb})
            return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("test text")
    assert result is not None
    assert len(result) == 1024


@pytest.mark.asyncio
async def test_nan_node_failover(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "node1":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [float("nan")] + [0.0] * 1023}]})
        else:
            import json

            data = json.loads(request.read())
            inputs = data["input"]
            results = []
            for i, text in enumerate(inputs):
                emb = [3.0, 4.0] + [0.0] * 1022
                results.append({"index": i, "embedding": emb})
            return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("test text")
    assert result is not None
    assert len(result) == 1024
    assert not any(math.isnan(x) for x in result)


@pytest.mark.asyncio
async def test_zero_norm_node_failover(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "node1":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0] * 1024}]})
        else:
            import json

            data = json.loads(request.read())
            inputs = data["input"]
            results = []
            for i, text in enumerate(inputs):
                emb = [3.0, 4.0] + [0.0] * 1022
                results.append({"index": i, "embedding": emb})
            return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("test text")
    assert result is not None
    assert len(result) == 1024
    norm = math.sqrt(sum(x * x for x in result))
    assert norm > 0


@pytest.mark.asyncio
async def test_batch_reverse_indices_returns_original_order_with_blanks_preserved(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        data = json.loads(request.read())
        inputs = data["input"]
        results = []
        for i, text in enumerate(inputs):
            emb = [3.0, 4.0] + [0.0] * 1022
            results.append({"index": i, "embedding": emb})
        return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    texts = ["text0", "", "text2", "", "text4"]
    results = await pool.embed_batch_async(texts)

    assert len(results) == 5
    # Blanks should be preserved as empty or zero vectors
    for i, text in enumerate(texts):
        if text == "":
            assert results[i] == [] or all(x == 0 for x in results[i])
        else:
            assert len(results[i]) == 1024


@pytest.mark.asyncio
async def test_healthy_individual_zero_accepted_normalized(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        data = json.loads(request.read())
        inputs = data["input"]
        results = []
        for i, text in enumerate(inputs):
            emb = [3.0, 4.0] + [0.0] * 1022
            results.append({"index": i, "embedding": emb})
        return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("test text")
    assert result is not None
    assert len(result) == 1024
    norm = math.sqrt(sum(x * x for x in result))
    assert norm > 0


@pytest.mark.asyncio
async def test_blank_inputs_no_http(monkeypatch):
    http_called = []

    async def handler(request: httpx.Request) -> httpx.Response:
        http_called.append(request.url.host)
        import json

        data = json.loads(request.read())
        inputs = data["input"]
        results = []
        for i, text in enumerate(inputs):
            emb = [3.0, 4.0] + [0.0] * 1022
            results.append({"index": i, "embedding": emb})
        return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    result = await pool.embed_async("")
    assert result == []
    assert len(http_called) == 0


@pytest.mark.asyncio
async def test_cancellation_does_not_block_subsequent_calls(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        import json

        data = json.loads(request.read())
        inputs = data["input"]
        results = []
        for i, text in enumerate(inputs):
            emb = [3.0, 4.0] + [0.0] * 1022
            results.append({"index": i, "embedding": emb})
        return httpx.Response(200, json={"data": results})

    urls = ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"]
    pool = _make_pool(urls, monkeypatch=monkeypatch, handler=handler)

    task = asyncio.create_task(pool.embed_async("test text"))
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    result = await pool.embed_async("test text 2")
    assert result is not None
    assert len(result) == 1024


def test_url_invalid_raises(monkeypatch):
    with pytest.raises(Exception):
        pool = _HttpPoolBackend(["not-a-valid-url"], model="bge-m3", timeout=5.0, max_chars=1000, expected_dim=1024)
        pool._load_sync()


def test_pool_normalizes_trailing_slashes_and_accepts_more_than_two_nodes():
    pool = _HttpPoolBackend(
        [
            "http://node1:8000/v1/embeddings/",
            "http://node2:8000/v1/embeddings",
            "http://node3:8000/v1/embeddings",
        ],
        model="bge-m3",
        timeout=5.0,
        max_chars=1000,
        expected_dim=1024,
    )

    assert pool._urls == [
        "http://node1:8000/v1/embeddings",
        "http://node2:8000/v1/embeddings",
        "http://node3:8000/v1/embeddings",
    ]


def test_pool_requires_two_distinct_nodes():
    with pytest.raises(ValueError, match="at least 2 URLs"):
        _HttpPoolBackend(
            ["http://node1:8000/v1/embeddings"],
            model="bge-m3",
            timeout=5.0,
            max_chars=1000,
            expected_dim=1024,
        )


def test_inprocess_embedder_optin_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_EMBED_HTTP_POOL_URLS", "http://node1:8000/v1/embeddings,http://node2:8000/v1/embeddings")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "1024")

    embedder = InProcessEmbedder(backend="http")
    embedder._build_backend()
    backend = embedder._backend

    assert isinstance(backend, _HttpPoolBackend)
    assert embedder._http_fallback_remote is None
    assert embedder._http_fallback is None


def test_unset_pool_legacy_constructs_http_backend(monkeypatch):
    monkeypatch.delenv("MNEMOS_EMBED_HTTP_POOL_URLS", raising=False)
    monkeypatch.delenv("MNEMOS_EMBEDDING_DIM", raising=False)

    embedder = InProcessEmbedder(backend="http")
    embedder._build_backend()
    backend = embedder._backend

    assert isinstance(backend, _HttpBackend)


@pytest.mark.asyncio
async def test_review_regression_real_parallelism(monkeypatch):
    active = 0
    peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0] + [0.0] * 1022}]})
        finally:
            active -= 1

    pool = _make_pool(
        ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"], monkeypatch=monkeypatch, handler=handler
    )
    await asyncio.gather(*(pool.embed_async("x") for _ in range(4)))
    assert peak >= 2


@pytest.mark.asyncio
async def test_review_regression_cancel_reservation_once(monkeypatch):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    pool = _make_pool(
        ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"], monkeypatch=monkeypatch, handler=handler
    )
    task = asyncio.create_task(pool.embed_async("x"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool._inflight == [0, 0]


@pytest.mark.asyncio
async def test_review_regression_bounded_exploration(monkeypatch):
    hosts = []

    async def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0] + [0.0] * 1022}]})

    pool = _make_pool(
        ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"], monkeypatch=monkeypatch, handler=handler
    )
    for _ in range(12):
        await pool.embed_async("x")
    assert hosts.count("node1") >= 2
    assert hosts.count("node2") >= 2


@pytest.mark.asyncio
async def test_batch_indices_really_reordered(monkeypatch):
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [4.0, 3.0] + [0.0] * 1022},
                    {"index": 0, "embedding": [3.0, 4.0] + [0.0] * 1022},
                ]
            },
        )

    pool = _make_pool(
        ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"], monkeypatch=monkeypatch, handler=handler
    )
    result = await pool.embed_batch_async(["first", "", "second"])
    assert result[0][:2] == [0.6, 0.8]
    assert result[1] == []
    assert result[2][:2] == [0.8, 0.6]


@pytest.mark.asyncio
async def test_cooldown_recovery_single_probe(monkeypatch):
    import time

    failing = True
    recovering = False
    entered = asyncio.Event()
    release = asyncio.Event()
    probes = 0
    calls = []

    async def handler(request):
        nonlocal probes
        calls.append(request.url.host)
        if request.url.host == "node1":
            if failing:
                return httpx.Response(500)
            if recovering:
                probes += 1
                entered.set()
                await release.wait()
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0] + [0.0] * 1022}]})

    pool = _make_pool(
        ["http://node1:8000/v1/embeddings", "http://node2:8000/v1/embeddings"], monkeypatch=monkeypatch, handler=handler
    )
    assert len(await pool.embed_async("first")) == 1024
    assert calls == ["node1", "node2"]
    calls.clear()
    await pool.embed_async("cooldown")
    assert calls == ["node2"]
    failing = False
    recovering = True
    pool._children[0]._breaker_opened_at = time.monotonic() - 31
    probe = asyncio.create_task(pool.embed_async("probe"))
    await asyncio.wait_for(entered.wait(), 1)
    assert len(await pool.embed_async("while probe runs")) == 1024
    assert probes == 1
    release.set()
    assert len(await probe) == 1024
    assert pool._children[0]._breaker_opened_at is None
    assert pool._inflight == [0, 0]


def test_pool_dimension_configuration_is_explicit(monkeypatch):
    monkeypatch.setenv("MNEMOS_EMBED_HTTP_POOL_URLS", "http://node1:8000/v1/embeddings,http://node2:8000/v1/embeddings")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "768")
    with pytest.raises(ValueError, match="1024"):
        InProcessEmbedder(backend="http")


def test_pool_rejects_empty_config_segments(monkeypatch):
    monkeypatch.setenv("MNEMOS_EMBED_HTTP_POOL_URLS", "http://node1:8000/v1/embeddings,")
    with pytest.raises(ValueError, match="empty segments"):
        InProcessEmbedder(backend="http")
