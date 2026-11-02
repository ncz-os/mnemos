"""M12 replica features — lag metrics, embedder fingerprint, anti-entropy.

Decision M12 (docs/design/mnemos-code-health-decisions-20261031.md, fleet-ops)
items 1, 2 and 4. Each class below is the ADR's test plan, end to end, against
a fake peer served over httpx.MockTransport:

1. Per-peer lag/health metrics — a fake peer feed advances the cursor and the
   reported lag drops; an injected error raises the consecutive-failure
   counter and the Prometheus gauge labelled by peer.
2. Embedder/dimension fingerprint — two nodes with different dimensions: the
   sync stores the content but no vectors, and /federation/status says why.
   A peer that rejects the fingerprint dup refuses the sync (409).
4. Anti-entropy — drop a replica row, ask both sides for /federation/digest,
   and exactly the dropped row's bucket differs; the repair restores it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import pytest

# ── shared fixtures ─────────────────────────────────────────────────────────


class _Req:
    def __init__(self, method: str, url: str, headers: dict, json_body=None, params=None):
        self.method = method
        self.url = url
        self.headers = headers
        self.json = json_body
        self.params = params or {}


class FakePeerFeed:
    """A fake remote MNEMOS: /schema, /feed, /memory/{id} and /digest.

    ``rows`` are feed rows in the source's own id space. Feed pages honour the
    compound ``since`` cursor the way the real handler does. ``schema`` is what
    /schema answers; ``fail_feed`` injects an error into every feed request.
    """

    def __init__(
        self,
        rows: list[dict],
        *,
        schema: dict,
        page_size: int = 2,
        fail_feed: bool = False,
        bucket_size: int = 1,
        digest_error: bool = False,
    ):
        # Rows carry ISO strings on the wire; keep the parsed instant for
        # ordering and cursor construction (the real feed encodes datetimes).
        self.rows = sorted(rows, key=lambda r: (r["updated"], r["id"]))
        self._instants = {r["id"]: datetime.fromisoformat(str(r["updated"]).replace("Z", "+00:00")) for r in self.rows}
        self.schema = schema
        self.page_size = page_size
        self.fail_feed = fail_feed
        self.bucket_size = bucket_size
        self.digest_error = digest_error
        self.requests: list[_Req] = []

    async def __call__(self, request):  # httpx.MockTransport handler
        import httpx

        url = urlparse(str(request.url))
        body = None
        if request.content:
            body = json.loads(request.content)
        self.requests.append(
            _Req(request.method, str(request.url), dict(request.headers), body, dict(request.url.params))
        )
        if url.path.endswith("/v1/federation/schema"):
            return httpx.Response(200, json=self.schema)
        if url.path.endswith("/v1/federation/digest"):
            if self.digest_error:
                return httpx.Response(500, json={"detail": "digest exploded"})
            return httpx.Response(200, json=self.digest(params=dict(request.url.params)))
        if url.path.endswith("/v1/federation/feed"):
            if self.fail_feed:
                return httpx.Response(503, json={"detail": "peer down"})
            return httpx.Response(200, json=self.feed_page(params=dict(request.url.params)))
        if "/v1/federation/memory/" in url.path:
            memory_id = url.path.rsplit("/", 1)[-1]
            for row in self.rows:
                if row["id"] == memory_id:
                    return httpx.Response(200, json=dict(row))
            return httpx.Response(404, json={"detail": "memory not found"})
        return httpx.Response(404, json={"detail": "not found"})

    def feed_page(self, *, params: dict) -> dict:
        from mnemos.domain.federation import _decode_feed_cursor, _encode_feed_cursor

        since = params.get("since")
        rows = self.rows
        if since:
            cursor = _decode_feed_cursor(since)
            rows = [
                r
                for r in rows
                if self._instants[r["id"]] > cursor.updated
                or (self._instants[r["id"]] == cursor.updated and r["id"] > cursor.memory_id)
            ]
        page = rows[: self.page_size]
        has_more = len(rows) > self.page_size
        if page:
            last = page[-1]
            next_cursor = _encode_feed_cursor(self._instants[last["id"]], last["id"])
        else:
            next_cursor = since
        return {"memories": [dict(r) for r in page], "next_cursor": next_cursor, "has_more": has_more}

    def digest(self, *, params: dict, ids: list[str] | None = None) -> dict:
        from mnemos.domain.replication_digest import (
            build_namespace_digest,
            filter_digest_by_namespace,
        )

        namespace = params.get("namespace") or None
        # /v1/federation/digest hashes the *stored* content_hash column, not
        # the payload; the feed rows carry content, so derive it the same way
        # the server-side column is derived or the two sides never agree.
        document = build_namespace_digest(
            [{**row, "content_hash": hashlib.sha256(row["content"].encode()).hexdigest()} for row in self.rows],
            bucket_size=self.bucket_size,
            namespace=namespace,
            id_prefix=None,
        )
        if ids is not None:
            buckets = filter_digest_by_namespace(document["buckets"], ids, bucket_size=self.bucket_size)
            document = {**document, "row_count": sum(b["count"] for b in buckets), "buckets": buckets}
        return document


def _remote_row(memory_id: str, updated: datetime, *, content: str | None = None) -> dict:
    content = content or f"content {memory_id}"
    return {
        "id": memory_id,
        "content": content,
        "category": "facts",
        "subcategory": None,
        "metadata": {"source": "fake-peer"},
        "quality_rating": 75,
        "verbatim_content": content,
        "owner_id": "owner-1",
        "namespace": "default",
        "permission_mode": 644,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "created": updated.isoformat().replace("+00:00", "Z"),
        "updated": updated.isoformat().replace("+00:00", "Z"),
    }


def _encode_vec(values: list[float]) -> str:
    import array

    return base64.b64encode(array.array("f", values).tobytes()).decode("ascii")


def _schema(*, embed_model: str | None, embed_dim: int | None) -> dict:
    """A peer /schema answer that passes this node's strict preflight.

    The fingerprint must be this tree's real one: same major.minor plus a
    missing/foreign migrations_fingerprint is a strict abort long before the
    features under test run (and a same-major/different-minor pair skips the
    comparison entirely, which would make the test pass for the wrong reason).
    """
    from mnemos._version import __version__
    from mnemos.domain.federation import _local_migrations_fingerprint

    parts = __version__.split(".")
    return {
        "mnemos_version": __version__,
        "schema_signature": f"{parts[0]}.{parts[1]}",
        "migrations_fingerprint": _local_migrations_fingerprint(),
        "embed_model": embed_model,
        "embed_dim": embed_dim,
    }


class FakeReplicaBackend:
    """In-memory replica backend behind ``backend.federation``.

    The repository speaks the same ABC surface ``sync_peer`` calls, so the
    production sync engine runs unmodified against it. Rows are keyed by the
    local ``fed:{peer}:{remote_id}`` id.
    """

    audit_chain = None

    def __init__(self) -> None:
        from mnemos.persistence.base import FederationRepository

        outer = self

        # The ABC is collected at class-creation time, so __getattr__ cannot
        # stand in for missing abstract methods — a double has to implement
        # every one it does not exercise, explicitly, as "not used here".
        _unused: dict[str, Any] = {}

        def _unused_method(name):
            async def _raise(*args, **kwargs):
                raise NotImplementedError(f"FakeReplicaBackend._Repo.{name}")

            _raise.__name__ = name
            return _raise

        for _name in (
            "apply_consolidation_tombstone",
            "count_exportable_rows",
            "count_federated_missing_embeddings",
            "create_peer",
            "delete_federated_memory",
            "delete_peer",
            "feed_query",
            "fetch_memory_page",
            "fetch_parity_page",
            "fetch_source_export_keyset_page",
            "fetch_sync_log",
            "get_feed_memory",
            "list_due_peers",
            "list_federated_missing_embeddings",
            "redispatch_federation_journal",
            "update_peer",
            "upsert_peer",
        ):
            _unused[_name] = _unused_method(_name)

        class _Repo(FederationRepository):  # noqa: D401 - in-memory double
            # Only the methods sync_peer / anti-entropy actually call; the rest
            # of the ABC surface raises rather than quietly returning nothing.
            locals().update(_unused)

            def __init__(self) -> None:
                self.peer = {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "name": "peer",
                    "enabled": True,
                    "last_sync_cursor": None,
                    "base_url": "https://peer.example",
                    "auth_token": "token",
                    "namespace_filter": None,
                    "category_filter": None,
                    "compat_mode": "strict",
                    "copy_embeddings": False,
                }
                self.rows: dict[str, dict] = {}
                self.embeddings: dict[str, list[float]] = {}
                self.sync_log: list[dict] = []
                self.lag_updates: list[dict] = []
                self.trailing_updates: list[dict] = []

            async def get_sync_peer(self, tx, peer_id):
                return dict(self.peer)

            async def update_peer_schema_check(self, tx, peer_id, peer_version, embed_model=None, embed_dim=None):
                self.peer["peer_mnemos_version"] = peer_version
                self.peer["peer_embed_model"] = embed_model
                self.peer["peer_embed_dim"] = embed_dim

            async def record_schema_abort(self, tx, *, peer_id, peer_version, cursor_before, error, is_transient):
                self.peer["last_error"] = error
                log_id = await self.create_sync_log(tx, peer_id, cursor_before)
                await self.finish_sync_log(
                    tx,
                    log_id=log_id,
                    memories_pulled=0,
                    memories_new=0,
                    memories_updated=0,
                    error=error,
                    cursor_after=cursor_before,
                )

            async def create_sync_log(self, tx, peer_id, cursor_before):
                self.sync_log.append({"cursor_before": cursor_before})
                return f"log-{len(self.sync_log)}"

            async def finish_sync_log(
                self,
                tx,
                *,
                log_id,
                memories_pulled,
                memories_new,
                memories_updated,
                error,
                cursor_after,
                events_received=0,
                rows_without_embedding=0,
                skipped_deleted=0,
            ):
                self.sync_log[-1].update(
                    {
                        "memories_pulled": memories_pulled,
                        "memories_new": memories_new,
                        "memories_updated": memories_updated,
                        "error": error,
                        "cursor_after": cursor_after,
                        "events_received": events_received,
                        "rows_without_embedding": rows_without_embedding,
                        "skipped_deleted": skipped_deleted,
                    }
                )

            async def record_sync_error(self, tx, peer_id, error):
                self.peer["last_error"] = error

            async def record_sync_success(self, tx, peer_id, cursor, total_pulled):
                self.peer["last_error"] = None
                self.peer["last_sync_cursor"] = cursor
                self.peer["last_sync_at"] = datetime.now(timezone.utc)
                self.peer["total_pulled"] = self.peer.get("total_pulled", 0) + total_pulled

            async def record_sync_lag(self, tx, peer_id, **fields):
                # Persist onto the peer row the way every backend's UPDATE
                # does, so /federation/status reads back what was recorded.
                self.lag_updates.append(fields)
                if fields.get("rows_pulled") is not None:
                    self.peer["rows_last_sync"] = fields["rows_pulled"]
                for key in ("last_cursor_ts", "lag_seconds", "embedding_status"):
                    if fields.get(key) is not None:
                        self.peer[key] = fields[key]

            async def record_consecutive_failures(self, tx, peer_id, value):
                self.trailing_updates.append({"consecutive_failures": value})
                self.peer["consecutive_failures"] = value

            async def fetch_federated_memory_marker(self, tx, local_id):
                row = self.rows.get(local_id)
                if row is None:
                    return None
                return {"federation_remote_updated": row["federation_remote_updated"]}

            async def fetch_federated_memory_markers(self, tx, local_ids):
                return {lid: await self.fetch_federated_memory_marker(tx, lid) for lid in local_ids}

            async def insert_federated_memory(self, tx, *, local_id, content, peer_name, remote_updated, **kwargs):
                if local_id in self.rows:
                    return False
                self.rows[local_id] = {
                    "id": local_id,
                    "content": content,
                    "namespace": kwargs.get("namespace"),
                    "federation_source": peer_name,
                    "federation_remote_updated": remote_updated,
                    "deleted_at": None,
                    "archived_at": None,
                    "consolidated_into": None,
                    "updated": remote_updated,
                }
                return True

            async def update_federated_memory_if_newer(
                self, tx, *, local_id, content, remote_updated, namespace=None, **kwargs
            ):
                row = self.rows.get(local_id)
                if row is None:
                    return False
                row["content"] = content
                row["federation_remote_updated"] = remote_updated
                row["updated"] = remote_updated
                return True

            async def count_federated_rows_without_embedding_per_peer(self, tx):
                # F-5: the durable per-peer embedding gap, as the backends
                # compute it (rows of this peer with no vector).
                return {}

            async def get_peer(self, tx, peer_id):
                return dict(self.peer) if peer_id == self.peer["id"] else None

            async def list_peers(self, tx):
                return [dict(self.peer)]

            async def list_federated_memory_hashes(self, tx, *, peer_name, namespace=None):
                prefix = f"fed:{peer_name}:"
                out = []
                for row in outer.rows.values():
                    if not row["id"].startswith(prefix):
                        continue
                    if namespace and row.get("namespace") != namespace:
                        continue
                    out.append(
                        {
                            "id": row["id"].removeprefix(prefix),
                            "content_hash": hashlib.sha256(row["content"].encode()).hexdigest(),
                            "updated": row["updated"],
                        }
                    )
                return out

            async def list_namespace_digest_rows(self, tx, *, namespace=None, limit=100_000):
                """M12 item 4, as the /federation/digest route consumes it.

                The real SQL returns the *local* row id (``fed:{peer}:{remote}``)
                because the handler has no peer argument; the route builds the
                document without an ``id_prefix``, so the prefix is stripped
                here exactly as the backends' SQL does it.
                """
                out = []
                for row in outer.rows.values():
                    if namespace and row.get("namespace") != namespace:
                        continue
                    out.append(
                        {
                            "id": row["id"].removeprefix("fed:peer:"),
                            "content_hash": hashlib.sha256(row["content"].encode()).hexdigest(),
                            "updated": row["updated"],
                        }
                    )
                return out[:limit]

        self.federation = _Repo()
        self.memories = _Memories(self)
        self._local_embed_model = "bge-m3"
        self._local_embed_dim = 1024

    @property
    def rows(self) -> dict[str, dict]:
        return self.federation.rows

    @asynccontextmanager
    async def transactional(self):
        yield object()


class _Memories:
    def __init__(self, backend: FakeReplicaBackend) -> None:
        self._backend = backend

    async def upsert_memory_embedding(self, tx, memory_id, embedding):
        self._backend.federation.embeddings[memory_id] = list(embedding)


def _install_transport(monkeypatch, feed) -> None:
    """Route mnemos.domain.federation's HTTP through the fake peer."""
    import httpx

    from mnemos import domain

    transport = httpx.MockTransport(feed)

    async def _safe_client(url, *, timeout=30.0, allow_private=False):
        return httpx.AsyncClient(transport=transport), object()

    monkeypatch.setattr(domain.federation, "make_safe_client", _safe_client)
    # The local embedder fingerprint is what makes a two-node dimension
    # mismatch observable; pin it per test through _install_local_fingerprint.
    from mnemos.persistence import federation_journal

    monkeypatch.setattr(federation_journal, "supports_journal", lambda tx: False)


def _install_local_fingerprint(monkeypatch, *, embed_model: str | None, embed_dim: int | None) -> None:
    from mnemos.domain import federation

    monkeypatch.setattr(
        federation,
        "_local_embedding_fingerprint",
        lambda: {"embed_model": embed_model, "embed_dim": embed_dim},
    )


# ── 1. per-peer lag and health metrics ──────────────────────────────────────


class TestPerPeerLagMetrics:
    @pytest.mark.asyncio
    async def test_fake_peer_feed_advances_cursor_and_lag_drops(self, monkeypatch):
        """A successful pull records the cursor age and the rows it moved."""
        from mnemos.domain import federation

        t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        feed = FakePeerFeed(
            [_remote_row("mem-1", t0), _remote_row("mem-2", t0 + timedelta(seconds=5))],
            schema=_schema(embed_model="bge-m3", embed_dim=1024),
        )
        backend = FakeReplicaBackend()
        _install_transport(monkeypatch, feed)
        _install_local_fingerprint(monkeypatch, embed_model="bge-m3", embed_dim=1024)
        now = t0 + timedelta(seconds=125)
        monkeypatch.setattr(federation, "_utcnow", lambda: now)

        pulled, new, _updated = await federation.sync_peer(backend, backend.federation.peer["id"])

        assert (pulled, new) == (2, 2)
        lag = backend.federation.lag_updates[-1]
        assert lag["lag_seconds"] == 120.0
        assert lag["last_cursor_ts"] == t0 + timedelta(seconds=5)
        assert lag["rows_pulled"] == 2
        # The lag metric projects straight onto FederationStatusResponse.
        from mnemos.api.routes.federation import _to_peer
        from mnemos.api.routes.federation import federation_status_values

        values = federation_status_values(_to_peer(backend.federation.peer))
        assert values["last_cursor_ts"] is not None
        assert values["lag_seconds"] == 120.0
        assert values["rows_last_sync"] == 2
        assert values["consecutive_failures"] == 0
        assert values["last_error"] is None

    def test_embedding_copy_enabled_tracks_the_status_reason(self):
        """The gauge says 1 only when vectors really may be copied.

        ``embedding_status`` is None until a peer has been checked at all
        ("cannot verify" is not "enabled"), and an explicit
        ``copy_embeddings disabled: ...`` reason is a refusal. Both used to
        read as "copyable", which reported a healthy copy path for a peer
        that never synced and for a peer whose fingerprint was rejected.
        """
        from mnemos.api.routes.federation import federation_status_values
        from mnemos.domain.models import FederationPeer

        def peer_with(status):
            return FederationPeer(
                id="11111111-1111-1111-1111-111111111111",
                name="peer",
                base_url="https://peer.example",
                enabled=True,
                sync_interval_secs=300,
                created="2026-10-31T00:00:00Z",
                updated="2026-10-31T00:00:00Z",
                embedding_status=status,
            )

        assert federation_status_values(peer_with(None))["embedding_copy_enabled"] is False
        assert (
            federation_status_values(peer_with("copy_embeddings disabled: embed_dim mismatch"))[
                "embedding_copy_enabled"
            ]
            is False
        )
        assert federation_status_values(peer_with("copy_embeddings ok"))["embedding_copy_enabled"] is True
        assert (
            federation_status_values(peer_with("copy_embeddings idle: no vectors in feed"))["embedding_copy_enabled"]
            is True
        )

    @pytest.mark.asyncio
    async def test_injected_error_raises_failure_counter_and_gauge(self, monkeypatch):
        """A failed pull bumps consecutive_failures and the labelled gauge."""
        from mnemos.api.routes.federation import federation_status_values, sync_federation_metrics
        from mnemos.domain import federation

        t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        feed = FakePeerFeed(
            [_remote_row("mem-1", t0)],
            schema=_schema(embed_model="bge-m3", embed_dim=1024),
            fail_feed=True,
        )
        backend = FakeReplicaBackend()
        backend.federation.peer["consecutive_failures"] = 2
        _install_transport(monkeypatch, feed)
        _install_local_fingerprint(monkeypatch, embed_model="bge-m3", embed_dim=1024)
        monkeypatch.setattr(federation, "_utcnow", lambda: t0)

        with pytest.raises(federation.FederationSyncError):
            await federation.sync_peer(backend, backend.federation.peer["id"])

        assert backend.federation.trailing_updates[-1] == {"consecutive_failures": 3}
        status = _install_status_backend(monkeypatch, backend)
        payload = await status()
        peer = payload.peers[0]
        assert peer.consecutive_failures == 3
        assert peer.last_error is not None
        # The status route itself keeps the labelled gauges in step so a
        # scraper sees the same numbers without a separate sync.
        sync_federation_metrics([(peer.name, federation_status_values(peer))])
        assert _gauge_value("mnemos_federation_peer_consecutive_failures_total", "peer") == 3

    @pytest.mark.asyncio
    async def test_success_resets_the_failure_counter(self, monkeypatch):
        from mnemos.domain import federation

        t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        feed = FakePeerFeed(
            [_remote_row("mem-1", t0)],
            schema=_schema(embed_model="bge-m3", embed_dim=1024),
        )
        backend = FakeReplicaBackend()
        backend.federation.peer["consecutive_failures"] = 5
        _install_transport(monkeypatch, feed)
        _install_local_fingerprint(monkeypatch, embed_model="bge-m3", embed_dim=1024)
        monkeypatch.setattr(federation, "_utcnow", lambda: t0 + timedelta(seconds=10))

        await federation.sync_peer(backend, backend.federation.peer["id"])

        assert backend.federation.trailing_updates[-1] == {"consecutive_failures": 0}


def _install_status_backend(monkeypatch, backend: FakeReplicaBackend):
    import mnemos.core.lifecycle as lc
    from mnemos.api.routes import federation as handler

    monkeypatch.setattr(lc, "_persistence_backend", backend)
    return handler.federation_status


def _gauge_value(name: str, labelname: str) -> float:
    from prometheus_client import REGISTRY

    for metric in REGISTRY.collect():
        if metric.name != name:
            continue
        for sample in metric.samples:
            if sample.name == name and labelname in sample.labels:
                return sample.value
    raise AssertionError(f"gauge {name} not registered")


# ── 2. embedder / dimension fingerprint ────────────────────────────────────


class TestEmbedderFingerprint:
    @pytest.mark.asyncio
    async def test_schema_advertises_embed_model_and_dim(self, monkeypatch):
        from mnemos.api.routes.federation import federation_schema
        from mnemos.domain import federation

        monkeypatch.setattr(
            federation,
            "_local_embedding_fingerprint",
            lambda: {"embed_model": "bge-m3", "embed_dim": 1024},
        )
        payload = await federation_schema(None)
        assert payload["embed_model"] == "bge-m3"
        assert payload["embed_dim"] == 1024

    @pytest.mark.asyncio
    async def test_check_peer_schema_returns_peer_fingerprint(self, monkeypatch):
        from mnemos.domain import federation

        feed = FakePeerFeed([], schema=_schema(embed_model="nomic-embed-text-v1.5", embed_dim=768))
        _install_transport(monkeypatch, feed)
        result = await federation._check_peer_schema("https://peer.example", "token", "peer")
        assert result["ok"] is True
        assert result["embed_model"] == "nomic-embed-text-v1.5"
        assert result["embed_dim"] == 768

    @pytest.mark.asyncio
    async def test_dimension_mismatch_stores_content_without_vectors(self, monkeypatch):
        """Two nodes with different dimensions: content lands, vectors do not.

        The fake peer ships 768-d vectors; the replica's own space is 1024-d.
        The sync must not adopt them, and must say so in the status.
        """
        from mnemos.api.routes.federation import federation_status, federation_status_values
        from mnemos.domain import federation

        t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        row = _remote_row("mem-1", t0)
        row["embedding"] = _encode_vec([0.1] * 768)
        row["embedding_model"] = "nomic-embed-text-v1.5"
        row["embedding_dim"] = 768
        feed = FakePeerFeed([row], schema=_schema(embed_model="nomic-embed-text-v1.5", embed_dim=768))
        backend = FakeReplicaBackend()
        backend.federation.peer["copy_embeddings"] = True
        _install_transport(monkeypatch, feed)
        _install_local_fingerprint(monkeypatch, embed_model="bge-m3", embed_dim=1024)
        monkeypatch.setattr(federation, "_utcnow", lambda: t0 + timedelta(seconds=5))
        _install_status_backend(monkeypatch, backend)

        pulled, new, _ = await federation.sync_peer(backend, backend.federation.peer["id"])

        assert (pulled, new) == (1, 1)
        assert "fed:peer:mem-1" in backend.rows, "content must land regardless"
        assert backend.federation.embeddings == {}, "a 768-d vector must not enter a 1024-d space"
        payload = await federation_status(None)
        peer = payload.peers[0]
        assert "copy_embeddings disabled" in (peer.embedding_status or "")
        assert "dim" in (peer.embedding_status or "")
        assert federation_status_values(peer)["embedding_status"].startswith("copy_embeddings disabled")

    @pytest.mark.asyncio
    async def test_matching_fingerprint_copies_vectors(self, monkeypatch):
        from mnemos.domain import federation

        t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        row = _remote_row("mem-1", t0)
        row["embedding"] = _encode_vec([0.1] * 1024)
        row["embedding_model"] = "bge-m3"
        row["embedding_dim"] = 1024
        feed = FakePeerFeed([row], schema=_schema(embed_model="bge-m3", embed_dim=1024))
        backend = FakeReplicaBackend()
        backend.federation.peer["copy_embeddings"] = True
        _install_transport(monkeypatch, feed)
        _install_local_fingerprint(monkeypatch, embed_model="bge-m3", embed_dim=1024)
        monkeypatch.setattr(federation, "_utcnow", lambda: t0 + timedelta(seconds=5))

        await federation.sync_peer(backend, backend.federation.peer["id"])

        assert len(backend.federation.embeddings["fed:peer:mem-1"]) == 1024
        assert backend.federation.lag_updates[-1]["embedding_status"] == "copy_embeddings ok"

    @pytest.mark.asyncio
    async def test_peer_that_rejects_the_fingerprint_refuses_the_sync(self, monkeypatch):
        """A peer answering 412 to the sync preflight is a durable rejection."""
        from mnemos.domain import federation

        feed = FakePeerFeed([], schema=_schema(embed_model="nomic-embed-text-v1.5", embed_dim=768))
        backend = FakeReplicaBackend()
        _install_transport(monkeypatch, feed)
        _install_local_fingerprint(monkeypatch, embed_model="bge-m3", embed_dim=1024)

        async def _rejecting_post(base_url, auth_token, fingerprint, *, client=None):
            return {"ok": False, "status": 412, "reason": "embed_dim mismatch: peer=768 local=1024"}

        monkeypatch.setattr(federation, "post_peer_fingerprint", _rejecting_post)

        with pytest.raises(federation.FederationSchemaIncompatible, match="embed_dim mismatch"):
            await federation.sync_peer(backend, backend.federation.peer["id"])
        assert backend.rows == {}
        assert "embed_dim mismatch" in (backend.federation.peer["last_error"] or "")

    def test_replica_schema_check_rejects_mismatched_fingerprint(self):
        from mnemos.domain.federation import fingerprint_mismatch_reason

        assert (
            fingerprint_mismatch_reason(
                {"embed_model": "bge-m3", "embed_dim": 1024}, {"embed_model": "bge-m3", "embed_dim": 1024}
            )
            is None
        )
        reason = fingerprint_mismatch_reason(
            {"embed_model": "bge-m3", "embed_dim": 1024}, {"embed_model": "nomic", "embed_dim": 768}
        )
        assert reason and "embed_dim" in reason and "embed_model" in reason
        # Unknown on either side is "cannot verify", not "matches".
        assert fingerprint_mismatch_reason(
            {"embed_model": None, "embed_dim": None}, {"embed_model": "bge-m3", "embed_dim": 1024}
        )


# ── 4. anti-entropy digest ─────────────────────────────────────────────────


class TestAntiEntropyDigest:
    @pytest.mark.asyncio
    async def test_digest_endpoint_returns_hash_buckets(self, monkeypatch):
        from mnemos.domain.replication_digest import build_namespace_digest

        from mnemos.domain.replication_digest import bucket_index

        # Bucket labels are SHA-256(id) mod bucket_size, so give the two rows
        # ids that *land in different buckets* at bucket_size=2 rather than
        # trusting hand-picked ids to be separated.
        ids = next(
            (f"mem-{i}", f"mem-{j}")
            for i in range(64)
            for j in range(i + 1, 64)
            if bucket_index(f"mem-{i}", 2) != bucket_index(f"mem-{j}", 2)
        )
        rows = [
            {"id": ids[0], "content_hash": "h1", "updated": "2026-10-31T10:00:00Z"},
            {"id": ids[1], "content_hash": "h2", "updated": "2026-10-31T10:01:00Z"},
        ]
        document = build_namespace_digest(rows, bucket_size=2, namespace=None)
        assert document["bucket_size"] == 2
        assert document["row_count"] == 2
        buckets = document["buckets"]
        assert len(buckets) == 2
        assert [b["bucket"] for b in buckets] == sorted(bucket_index(r["id"], 2) for r in rows)
        # A single-row bucket's root must equal the audit Merkle root over
        # that row's (id, content_hash, updated) leaf -- one hashing scheme.
        from mnemos.audit.crypto import merkle_root
        from mnemos.domain.replication_digest import merkle_leaf_for_row

        assert [b["count"] for b in buckets] == [1, 1]
        # buckets[] is ordered by bucket label, not by row order: pair each
        # bucket with its own row instead of assuming rows[0] == buckets[0].
        by_id = {row["id"]: row for row in rows}
        for entry in buckets:
            (member,) = entry["ids"]
            assert entry["merkle_root"] == merkle_root([merkle_leaf_for_row(by_id[member])]).hex()
        # The document a peer serves is the same shape a replica diffs: no
        # caller should have to re-wrap buckets before diff_digests sees it.
        from mnemos.domain.replication_digest import diff_digests

        assert diff_digests(document, document)["in_sync"] is True

    @pytest.mark.asyncio
    async def test_dropped_replica_row_is_found_and_repaired(self, monkeypatch):
        """Drop one replica row; exactly its bucket differs; repair restores it."""
        from mnemos.api.routes import federation as handler
        from mnemos.domain.anti_entropy import repair_namespace
        from mnemos.domain.replication_digest import diff_digests

        t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        from mnemos.domain.replication_digest import bucket_index

        # Four ids that each land in their OWN bucket at bucket_size=4, so
        # losing one row names exactly one bucket in the diff.
        source_ids = []
        seen: set[str] = set()
        for i in range(128):
            bucket = bucket_index(f"mem-{i}", 4)
            if bucket not in seen:
                seen.add(bucket)
                source_ids.append(f"mem-{i}")
            if len(source_ids) == 4:
                break
        source_rows = [_remote_row(mid, t0 + timedelta(seconds=n)) for n, mid in enumerate(source_ids)]
        feed = FakePeerFeed(source_rows, schema=_schema(embed_model="bge-m3", embed_dim=1024), bucket_size=4)
        backend = FakeReplicaBackend()
        _install_transport(monkeypatch, feed)
        _install_local_fingerprint(monkeypatch, embed_model="bge-m3", embed_dim=1024)
        monkeypatch.setattr("mnemos.domain.federation._utcnow", lambda: t0 + timedelta(minutes=5))
        _install_status_backend(monkeypatch, backend)

        await federation_sync(backend)
        assert len(backend.rows) == 4

        # A healthy replica and its source agree bucket for bucket.
        # An omitted ?bucket= is the shared default everywhere, so both
        # nodes must be diffed at that same width.
        from mnemos.domain.replication_digest import DEFAULT_BUCKET_SIZE

        feed.bucket_size = DEFAULT_BUCKET_SIZE
        source_digest = feed.digest(params={"namespace": ""})
        same = await handler.federation_digest(None, namespace=None, bucket_size=None, ids=None)
        assert diff_digests(source_digest, same)["in_sync"] is True

        # The replica loses a row (operator error, crash before commit, ...).
        lost = source_ids[2]
        backend.rows.pop(f"fed:peer:{lost}")

        replica_digest = await handler.federation_digest(None, namespace=None, bucket_size=None, ids=None)
        diff = diff_digests(source_digest, replica_digest)
        # Exactly the lost row's bucket differs, and the diff names the row.
        assert diff["differing_buckets"] == [bucket_index(lost, DEFAULT_BUCKET_SIZE)]
        assert diff["source_only_bucket_ids"] == [lost]
        assert diff["repair_ids"] == [lost]

        result = await repair_namespace(backend, backend.federation.peer["id"], diff)
        assert result["pulled"] == 1
        assert f"fed:peer:{lost}" in backend.rows
        assert backend.rows[f"fed:peer:{lost}"]["content"] == f"content {lost}"

        # Nothing left to repair.
        replica_digest = await handler.federation_digest(None, namespace=None, bucket_size=None, ids=None)
        assert diff_digests(source_digest, replica_digest)["differing_buckets"] == []

    @pytest.mark.asyncio
    async def test_digest_diff_is_empty_when_nodes_agree(self, monkeypatch):
        from mnemos.api.routes import federation as handler
        from mnemos.domain.replication_digest import DEFAULT_BUCKET_SIZE, diff_digests

        t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        source_rows = [_remote_row(f"mem-{i}", t0 + timedelta(seconds=i)) for i in range(1, 4)]
        feed = FakePeerFeed(source_rows, schema=_schema(embed_model="bge-m3", embed_dim=1024))
        backend = FakeReplicaBackend()
        _install_transport(monkeypatch, feed)
        _install_local_fingerprint(monkeypatch, embed_model="bge-m3", embed_dim=1024)
        monkeypatch.setattr("mnemos.domain.federation._utcnow", lambda: t0 + timedelta(minutes=5))
        _install_status_backend(monkeypatch, backend)

        await federation_sync(backend)
        feed.bucket_size = DEFAULT_BUCKET_SIZE
        diff = diff_digests(
            feed.digest(params={"namespace": ""}),
            await handler.federation_digest(None, namespace=None, bucket_size=None, ids=None),
        )
        assert diff["differing_buckets"] == []

    @pytest.mark.asyncio
    async def test_digest_route_requires_federation_role(self, monkeypatch):
        from fastapi import HTTPException

        from mnemos.api.routes import federation as handler
        from mnemos.api.dependencies import UserContext

        with pytest.raises(HTTPException) as exc:
            await handler._require_federation_role(
                UserContext(user_id="u", group_ids=[], role="reader", namespace="default", authenticated=True)
            )
        assert exc.value.status_code == 403


async def federation_sync(backend: FakeReplicaBackend):
    from mnemos.domain import federation

    return await federation.sync_peer(backend, backend.federation.peer["id"])
