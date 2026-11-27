"""Round-2 replica-mode gap tests (continuation 2026-11-22).

The BASE delivery passed its own suite but an independent full-handler-body
audit found gaps the original tests could not see. Each test here pins one
proven gap shut:

  * ``POST /admin/persephone/archive/{id}`` had no replica guard (the old
    static audit used a 35-line window that bled into the NEXT handler's
    guard and masked the miss).
  * ``GET /auth/oauth/{provider}/callback`` provisions a user and INSERTs an
    oauth_sessions row — a mutating GET that must 503 on a replica, and the
    guard must sit OUTSIDE the handler's catch-all ``except Exception``
    (which would otherwise flatten the 503 into a 502).
  * MCP tool-call audit persistence (``_schedule_audit_persist``) scheduled
    an INSERT into mcp_audit_log for EVERY tool call on a replica — reads
    and 503 rejections alike. It must skip the durable write; the logger
    entry stays.
  * The lifespan worker-skip only gated ``honor_worker_enabled=True``
    workers via the general switch; ``honor=False`` workers (persephone
    archival, morpheus, webhook repair/delivery, federation sync/local-embed)
    still launched, and the post-DB NATS consumer hooks ran after the skip
    block. Both must be gated on replica mode.
  * Query-time embedding on a replica must use the shared HTTP embedder
    only — never load a local GGUF/NPU model, even when the http chain
    fails and a local fallback exists. Missing endpoint => [] => lexical
    degrade. Primary behaviour (local fallback) is unchanged.
  * ``diff_memory_commits`` is a pure read (two SELECTs + difflib) and was
    wrongly listed in ``_WRITE_TOOLS``, 503-ing a read on a replica.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mnemos.core import replica_mode
from mnemos.core.replica_mode import REPLICA_MODE_ENV

ROUTES_DIR = Path(__file__).resolve().parents[1] / "mnemos" / "api" / "routes"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    replica_mode._reset_replica_state_for_tests()
    monkeypatch.delenv(REPLICA_MODE_ENV, raising=False)
    monkeypatch.delenv("MNEMOS_PRIMARY_URL", raising=False)
    yield
    replica_mode._reset_replica_state_for_tests()


def _set_replica(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(replica_mode, "_replica", value)


# ────────────────────────────────────────────────────────────────────────────
# Gap 1: POST /admin/persephone/archive/{memory_id} must 503 on a replica
# ────────────────────────────────────────────────────────────────────────────


def test_persephone_archive_memory_returns_503_on_replica(monkeypatch):
    from mnemos.api.routes import admin

    _set_replica(monkeypatch, True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            admin.persephone_archive_memory("mem-1", user=SimpleNamespace(user_id="root"))
        )
    assert exc.value.status_code == 503
    assert exc.value.detail == {"error": "replica_read_only", "primary_hint": None}


def test_persephone_archive_memory_primary_hint_propagates(monkeypatch):
    from mnemos.api.routes import admin

    _set_replica(monkeypatch, True)
    monkeypatch.setenv("MNEMOS_PRIMARY_URL", "https://primary.example.test")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            admin.persephone_archive_memory("mem-1", user=SimpleNamespace(user_id="root"))
        )
    assert exc.value.status_code == 503
    assert exc.value.detail["primary_hint"] == "https://primary.example.test"


# ────────────────────────────────────────────────────────────────────────────
# Gap 2: the OAuth callback is a mutating GET — 503, not a swallowed 502
# ────────────────────────────────────────────────────────────────────────────


def test_oauth_callback_returns_503_on_replica_before_any_write(monkeypatch):
    from mnemos.api.routes import oauth

    _set_replica(monkeypatch, True)

    # If the guard were missing or inside the try/except, the handler would
    # reach _load_provider (needs a live backend) and surface a 502/500.
    async def _fail_load_provider(name):
        raise AssertionError("oauth callback reached provider load on a replica")

    monkeypatch.setattr(oauth, "_load_provider", _fail_load_provider)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(oauth.oauth_callback("some-provider", request=SimpleNamespace()))
    assert exc.value.status_code == 503
    assert exc.value.detail == {"error": "replica_read_only", "primary_hint": None}


def test_oauth_callback_guard_is_outside_the_catch_all():
    """Static pin: require_write_allowed() precedes the try block so the
    catch-all ``except Exception -> 502`` cannot flatten the 503."""
    src = (ROUTES_DIR / "oauth.py").read_text()
    start = src.index("async def oauth_callback(")
    body = src[start:]
    guard_idx = body.index("require_write_allowed()")
    try_idx = body.index("try:")
    assert guard_idx < try_idx, (
        "oauth_callback replica guard must run before the catch-all try block"
    )


# ────────────────────────────────────────────────────────────────────────────
# Gap 3: MCP audit persistence must not write on a replica
# ────────────────────────────────────────────────────────────────────────────


def test_schedule_audit_persist_skipped_in_replica_mode(monkeypatch):
    from mnemos.mcp.tools import _security

    _set_replica(monkeypatch, True)

    async def _boom(**kwargs):
        raise AssertionError("audit persist attempted on a replica")

    monkeypatch.setattr("mnemos.domain.mcp_audit_repo.persist_audit_record", _boom)

    async def scenario():
        before = len(_security._INFLIGHT_AUDIT_TASKS)
        _security._schedule_audit_persist(
            caller_user_id="u1",
            role="root",
            tool="get_stats",
            parameter_shape={},
            outcome="success",
            error_class=None,
        )
        await asyncio.sleep(0)  # let any (wrongly) scheduled task run
        assert len(_security._INFLIGHT_AUDIT_TASKS) == before

    asyncio.run(scenario())


def test_mcp_log_tool_audit_no_db_write_in_replica_mode(monkeypatch):
    """Every tool call (reads included) logs an audit record; on a replica
    the durable INSERT must be skipped while the logger entry remains."""
    from mnemos.mcp.tools import _security

    _set_replica(monkeypatch, True)

    async def _boom(**kwargs):
        raise AssertionError("audit persist attempted on a replica")

    monkeypatch.setattr("mnemos.domain.mcp_audit_repo.persist_audit_record", _boom)

    async def scenario():
        before = len(_security._INFLIGHT_AUDIT_TASKS)
        _security._mcp_log_tool_audit(
            caller_id="u1",
            role="root",
            tool_name="search_memories",
            parameters={"query": "x"},
            outcome="success",
        )
        _security._mcp_log_root_bypass(
            caller_id="u1", tool_name="search_memories", parameters={}
        )
        await asyncio.sleep(0)
        assert len(_security._INFLIGHT_AUDIT_TASKS) == before

    asyncio.run(scenario())


def test_schedule_audit_persist_still_writes_on_primary(monkeypatch):
    """Primary behaviour unchanged: the durable mirror is scheduled."""
    from mnemos.mcp.tools import _security

    _set_replica(monkeypatch, False)
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("mnemos.domain.mcp_audit_repo.persist_audit_record", _record)

    async def scenario():
        _security._schedule_audit_persist(
            caller_user_id="u1",
            role="root",
            tool="get_stats",
            parameter_shape={},
            outcome="success",
            error_class=None,
        )
        pending = list(_security._INFLIGHT_AUDIT_TASKS)
        assert pending, "primary must schedule the audit persist task"
        await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) == 1


# ────────────────────────────────────────────────────────────────────────────
# Gap 4: lifespan must skip honor=False workers AND post-DB hooks on replica
# ────────────────────────────────────────────────────────────────────────────


def _lifespan_harness(monkeypatch, tmp_path, *, replica: bool):
    """Run the REAL lifespan against a fake sqlite backend with two fake
    worker factories (honor=True and honor=False) and one fake post-DB hook.
    Pattern follows tests/test_validate_lifespan_cleanup.py."""
    from unittest.mock import AsyncMock

    from mnemos.core import lifecycle as lc
    from mnemos.core.config import get_settings

    monkeypatch.setenv("_MNEMOS_BIND_VALIDATED_HOST", "127.0.0.1")
    _set_replica(monkeypatch, replica)

    backend = SimpleNamespace(close=AsyncMock(), capabilities=set(), oauth=None)
    for name in ["_worker_tasks", "_background_tasks", "_delivery_attempt_tasks"]:
        monkeypatch.setattr(lc, name, set())
    monkeypatch.setattr(lc, "_lifespan_cleanup_hooks", {})
    monkeypatch.setattr(lc, "_auth_configurer", None)
    monkeypatch.setattr(lc, "_provider_manifest_reloader", None)
    monkeypatch.setattr(lc, "_load_config", lambda: {"worker": {"enabled": True}})
    monkeypatch.setattr(lc, "_select_persistence_backend", lambda settings: "sqlite")
    monkeypatch.setattr(lc, "_build_sqlite_backend", AsyncMock(return_value=backend))
    monkeypatch.setattr(lc, "_log_and_validate_backend_capabilities", lambda *a: None)
    monkeypatch.setattr(lc, "_log_federation_startup_guidance", AsyncMock())
    monkeypatch.setattr(
        "mnemos.persistence.base.backend_supported_layers",
        lambda backend: set(get_settings().layers.active_layers),
    )
    monkeypatch.setattr("mnemos.nats.connect_nats", AsyncMock())

    launched: list[str] = []

    def _factory(name):
        def factory(_handle):
            launched.append(name)

            async def _loop():
                await asyncio.Event().wait()

            return _loop()

        return factory

    # Real registry names: the non-Postgres path only launches these four
    # (existing primary gate), and "persephone archival worker" is registered
    # honor_worker_enabled=False — exactly the worker class the replica skip
    # must cover.
    monkeypatch.setattr(
        lc,
        "_lifespan_worker_factories",
        {
            "audit_sealer_worker": (_factory("audit_sealer_worker"), True),
            "persephone archival worker": (_factory("persephone archival worker"), False),
        },
    )

    hooks_called: list[str] = []

    async def _post_db_hook(handle, settings):
        hooks_called.append("post_db_hook")

    monkeypatch.setattr(lc, "_post_db_startup_hooks", {"fake hook": _post_db_hook})
    return lc, launched, hooks_called


def test_lifespan_replica_skips_all_workers_and_post_db_hooks(monkeypatch, tmp_path):
    lc, launched, hooks_called = _lifespan_harness(monkeypatch, tmp_path, replica=True)

    async def run():
        async with lc.lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass

    asyncio.run(run())
    assert launched == [], (
        f"replica must skip EVERY worker incl. honor_worker_enabled=False; launched: {launched}"
    )
    assert hooks_called == [], "replica must skip post-DB startup hooks (writing consumers)"
    assert lc._worker_tasks == set()


def test_lifespan_primary_launches_workers_and_hooks_unchanged(monkeypatch, tmp_path):
    lc, launched, hooks_called = _lifespan_harness(monkeypatch, tmp_path, replica=False)

    async def run():
        async with lc.lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass

    asyncio.run(run())
    assert sorted(launched) == ["audit_sealer_worker", "persephone archival worker"], (
        "primary behaviour must be unchanged: both workers launch, including "
        "the honor_worker_enabled=False archival worker"
    )
    assert hooks_called == ["post_db_hook"]


def test_run_post_db_startup_hooks_replica_gate(monkeypatch):
    """Direct unit pin on the hook runner itself."""
    from mnemos.core import lifecycle as lc

    called = []

    async def hook(handle, settings):
        called.append(handle)

    monkeypatch.setattr(lc, "_post_db_startup_hooks", {"h": hook})

    _set_replica(monkeypatch, True)
    asyncio.run(lc._run_post_db_startup_hooks(object(), None))
    assert called == []

    _set_replica(monkeypatch, False)
    handle = object()
    asyncio.run(lc._run_post_db_startup_hooks(handle, None))
    assert called == [handle]


# ────────────────────────────────────────────────────────────────────────────
# Gap 5: replica embedding is HTTP-only; no local embedder ever starts
# ────────────────────────────────────────────────────────────────────────────


def _no_local_load(monkeypatch):
    """Fail loudly if any local backend load/embed is attempted."""
    from mnemos.runtime import embedder as emb

    def _refuse(self, *a, **k):
        raise AssertionError("local embedder load/embed attempted on a replica")

    monkeypatch.setattr(emb._LlamaCppBackend, "_load_sync", _refuse)
    monkeypatch.setattr(emb._LlamaCppBackend, "_embed_sync", _refuse)


def test_replica_forces_http_backend_even_when_llamacpp_requested(monkeypatch, tmp_path):
    from mnemos.runtime import embedder as emb

    _set_replica(monkeypatch, True)
    _no_local_load(monkeypatch)
    e = emb.InProcessEmbedder(
        backend="llamacpp",
        http_url="",
        http_url_fallback="",
        model_path=str(tmp_path / "missing.gguf"),
    )
    vec = asyncio.run(e.embed("hello world"))
    assert vec == []  # missing endpoint => lexical degrade, never a crash
    assert e.backend_name == "http"


def test_replica_never_falls_back_to_local_gguf(monkeypatch, tmp_path):
    """http chain fails AND a local GGUF exists: a primary would load it;
    a replica must return [] instead (no per-host embedder)."""
    from mnemos.runtime import embedder as emb

    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"not-a-real-model")
    _set_replica(monkeypatch, True)
    _no_local_load(monkeypatch)
    e = emb.InProcessEmbedder(
        backend="http",
        http_url="",
        http_url_fallback="",
        model_path=str(gguf),
    )
    assert asyncio.run(e.embed("hello")) == []
    assert asyncio.run(e.embed_batch(["hello", "world"])) == [[], []]


def test_replica_refuses_prebuilt_local_backend(monkeypatch, tmp_path):
    """Singleton built before the replica flag was set: embed() must refuse
    the local backend BEFORE _ensure_loaded can load it."""
    from mnemos.runtime import embedder as emb

    _set_replica(monkeypatch, False)
    e = emb.InProcessEmbedder(
        backend="llamacpp",
        http_url="",
        model_path=str(tmp_path / "missing.gguf"),
    )

    async def _no_load():
        raise AssertionError("_ensure_loaded reached on a replica with a local backend")

    # Simulate a pre-existing local backend without loading anything.
    e._backend = emb._LlamaCppBackend(
        model_path=e.model_path, n_ctx=8, n_threads=1, n_gpu_layers=0, max_chars=1000
    )
    e._backend_name = "llamacpp"
    monkeypatch.setattr(e, "_ensure_loaded", _no_load)
    _set_replica(monkeypatch, True)
    assert asyncio.run(e.embed("hello")) == []


def test_replica_batch_refuses_prebuilt_local_backend(monkeypatch, tmp_path):
    from mnemos.runtime import embedder as emb

    _set_replica(monkeypatch, False)
    e = emb.InProcessEmbedder(backend="llamacpp", model_path=str(tmp_path / "missing.gguf"))
    e._backend = emb._LlamaCppBackend(
        model_path=e.model_path, n_ctx=8, n_threads=1, n_gpu_layers=0, max_chars=1000
    )
    e._backend_name = "llamacpp"

    async def _no_load():
        raise AssertionError("local backend load attempted on a replica")

    def _no_embed(*args, **kwargs):
        raise AssertionError("local backend called on a replica")

    monkeypatch.setattr(e, "_ensure_loaded", _no_load)
    monkeypatch.setattr(emb._LlamaCppBackend, "_embed_sync", _no_embed)
    _set_replica(monkeypatch, True)
    assert asyncio.run(e.embed_batch(["hello", "world"])) == [[], []]


def test_primary_local_fallback_unchanged(monkeypatch, tmp_path):
    """Primary (flag False): the http -> local-GGUF fallback still works."""
    from mnemos.runtime import embedder as emb

    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"not-a-real-model")
    _set_replica(monkeypatch, False)

    monkeypatch.setattr(emb._LlamaCppBackend, "_load_sync", lambda self: None)
    monkeypatch.setattr(
        emb._LlamaCppBackend, "_embed_sync", lambda self, text: [0.1, 0.2, 0.3]
    )
    e = emb.InProcessEmbedder(
        backend="http",
        http_url="",
        http_url_fallback="",
        model_path=str(gguf),
    )
    assert asyncio.run(e.embed("hello")) == [0.1, 0.2, 0.3]


def test_replica_http_embedder_uses_shared_endpoint(monkeypatch):
    """With the shared endpoint configured, a replica embeds through the
    SAME _HttpBackend client the primary uses (no new client type)."""
    from mnemos.runtime import embedder as emb

    _set_replica(monkeypatch, True)
    e = emb.InProcessEmbedder(
        backend="auto",
        http_url="https://embed.example.test/v1/embeddings",
        http_url_fallback="",
        model_path="/nonexistent/model.gguf",
    )

    async def scenario():
        await e._ensure_loaded()
        assert isinstance(e._backend, emb._HttpBackend)
        assert e._backend.url == "https://embed.example.test/v1/embeddings"

    asyncio.run(scenario())


# ────────────────────────────────────────────────────────────────────────────
# Gap 6: diff_memory_commits is a read — it must work on a replica
# ────────────────────────────────────────────────────────────────────────────


def test_diff_memory_commits_not_in_write_tools():
    from mnemos.mcp.tools import _WRITE_TOOLS

    assert "diff_memory_commits" not in _WRITE_TOOLS, (
        "diff_memory_commits only reads two commit rows and diffs them; "
        "blocking it on a replica violates 'keep reads fully working'"
    )


def test_dispatcher_serves_diff_tool_on_replica(monkeypatch):
    from mnemos.mcp import tools as mcp_tools

    _set_replica(monkeypatch, True)
    called = []

    async def _fake_diff(**kwargs):
        called.append(kwargs)
        return {"success": True, "diff": ""}

    entry = dict(mcp_tools.TOOL_REGISTRY["diff_memory_commits"])
    entry["handler"] = _fake_diff
    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY, "diff_memory_commits", entry)

    result = asyncio.run(
        mcp_tools.execute_tool(
            "diff_memory_commits",
            {"memory_id": "m1", "commit_a": "a", "commit_b": "b"},
        )
    )
    assert result.get("success") is True
    assert called, "read tool handler must run on a replica"


def test_dispatcher_blocks_write_tool_on_replica(monkeypatch):
    from mnemos.mcp import tools as mcp_tools

    _set_replica(monkeypatch, True)

    async def _must_not_run(**kwargs):
        raise AssertionError("write tool handler ran on a replica")

    entry = dict(mcp_tools.TOOL_REGISTRY["create_memory"])
    entry["handler"] = _must_not_run
    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY, "create_memory", entry)

    result = asyncio.run(
        mcp_tools.execute_tool("create_memory", {"content": "x"})
    )
    assert result["success"] is False
    assert result["error"] == "replica_read_only"
    assert "primary_hint" in result


# ────────────────────────────────────────────────────────────────────────────
# Review pins (r2 review, 2026-11-22): import-resolution findings
# ────────────────────────────────────────────────────────────────────────────


def test_require_write_allowed_canonical_definition_and_imports():
    """AST proof: the guard helper has exactly ONE definition —
    mnemos.api.persistence_helpers.require_write_allowed — and admin.py /
    oauth.py IMPORT it; neither defines it locally.

    Updated 2026-11-22 (r3): the previous version checked source text. The
    review diff-map misreads added CALL sites (``require_write_allowed()``)
    and function-local IMPORT lines as local ``defs``, so this pin now uses
    the AST, which distinguishes FunctionDef from ImportFrom/Call
    definitively. Strengthened, not weakened: it asserts the absence of any
    local definition (including nested) in both route modules.

    Updated 2026-11-22 (r4): both route modules now import the canonical
    helper at MODULE level (matching the admin.py convention), so the pin
    additionally asserts module-level placement and function identity for
    oauth.py. The canonical definition module (persistence_helpers) is part
    of the delivery diff, making the single-source contract visible to
    diff-based review.
    """
    import ast

    from mnemos.api import persistence_helpers
    from mnemos.api.routes import admin, oauth

    helper = persistence_helpers.require_write_allowed
    assert helper.__module__ == "mnemos.api.persistence_helpers"
    # Both route modules import the SAME function object at module level.
    assert admin.require_write_allowed is helper
    assert oauth.require_write_allowed is helper

    for mod in (admin, oauth):
        tree = ast.parse(Path(mod.__file__).read_text())
        local_defs = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "require_write_allowed"
        ]
        assert local_defs == [], (
            f"{mod.__name__} must IMPORT the canonical helper, never define it"
        )
        canonical_imports = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            and n.module == "mnemos.api.persistence_helpers"
            and any(a.name == "require_write_allowed" for a in n.names)
        ]
        assert canonical_imports, (
            f"{mod.__name__} must import require_write_allowed from "
            "mnemos.api.persistence_helpers"
        )

    # Both admin.py and oauth.py: the import is module-level (top-level
    # statement), never a function-local rebind.
    for mod in (admin, oauth):
        mod_tree = ast.parse(Path(mod.__file__).read_text())
        assert any(
            isinstance(n, ast.ImportFrom)
            and n.module == "mnemos.api.persistence_helpers"
            and any(a.name == "require_write_allowed" for a in n.names)
            for n in mod_tree.body
        ), f"{mod.__name__} must import the guard at module level"
    # oauth.py: the guard CALL precedes the catch-all Try inside the
    # callback (a 503 must not flatten into 502).
    oauth_tree = ast.parse(Path(oauth.__file__).read_text())
    callback = next(
        n
        for n in oauth_tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "oauth_callback"
    )
    guard_idx = try_idx = None
    for i, stmt in enumerate(callback.body):
        if guard_idx is None and isinstance(stmt, ast.Expr) and isinstance(
            stmt.value, ast.Call
        ) and isinstance(stmt.value.func, ast.Name) and (
            stmt.value.func.id == "require_write_allowed"
        ):
            guard_idx = i
        if try_idx is None and isinstance(stmt, ast.Try):
            try_idx = i
    assert guard_idx is not None, "oauth_callback must call require_write_allowed()"
    assert try_idx is not None, "oauth_callback has a catch-all try block"
    assert guard_idx < try_idx, (
        "the replica guard must run BEFORE the catch-all try block"
    )


def test_lifecycle_replica_mode_enabled_import_is_explicit():
    """AST proof: lifecycle.py imports replica_mode_enabled explicitly at
    module level from mnemos.core.replica_mode and never defines it locally.

    Updated 2026-11-22 (r3): source-text pin replaced by AST assertions for
    the same reason as the guard pin above — the review diff-map records
    added ``replica_mode_enabled()`` CALL sites as local defs. The AST walk
    proves no FunctionDef with that name exists anywhere in the module, and
    the resolved identity is checked at runtime.
    """
    import ast

    from mnemos.core import lifecycle, replica_mode

    assert lifecycle.replica_mode_enabled is replica_mode.replica_mode_enabled
    tree = ast.parse(Path(lifecycle.__file__).read_text())
    local_defs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "replica_mode_enabled"
    ]
    assert local_defs == [], "lifecycle.py must not define replica_mode_enabled locally"
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "mnemos.core.replica_mode"
        and any(a.name == "replica_mode_enabled" for a in n.names)
        for n in tree.body
    ), "lifecycle.py must import replica_mode_enabled at module level"


def test_embedder_uses_canonical_replica_predicate_live(monkeypatch, tmp_path):
    """r5 review: the embedder must use mnemos.core.replica_mode.is_replica,
    not a private cached flag. Pin: module-level canonical import, no local
    FunctionDef (is_replica or the old _replica_active wrapper), function
    identity, and LIVE state reads — a canonical replica-state flip (what a
    health probe does on promotion) is honoured immediately by the embedder
    without rebuilding it."""
    import ast

    from mnemos.core import replica_mode
    from mnemos.runtime import embedder

    monkeypatch.setenv("MNEMOS_REPLICA_MODE", "off")
    assert embedder.is_replica is replica_mode.is_replica

    src = Path(embedder.__file__).read_text()
    assert "_replica_active" not in src, "no private replica wrapper/flag allowed"
    tree = ast.parse(src)
    assert not [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name in ("is_replica", "_replica_active")
    ], "embedder must not define the replica predicate locally"
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "mnemos.core.replica_mode"
        and any(a.name == "is_replica" for a in n.names)
        for n in tree.body
    ), "embedder must import is_replica at module level from the canonical module"

    # Live reads: flip the canonical state and the embedder follows at once
    # (same mechanism the suite's other tests use; is_replica() reads the
    # module global on every call, so no rebuild is needed).
    _set_replica(monkeypatch, True)
    assert embedder.is_replica() is True
    _no_local_load(monkeypatch)
    e = embedder.InProcessEmbedder(
        backend="llamacpp",
        http_url="",
        http_url_fallback="",
        model_path=str(tmp_path / "missing.gguf"),
    )
    assert asyncio.run(e.embed("hello world")) == []  # lexical degrade
    assert e.backend_name == "http"  # canonical predicate forced HTTP
    _set_replica(monkeypatch, False)
    assert embedder.is_replica() is False


# ────────────────────────────────────────────────────────────────────────────
# Startup + health probes: zero writes against a read-only fake
# ────────────────────────────────────────────────────────────────────────────


def test_open_and_health_probes_attempt_no_writes(monkeypatch):
    """backend.open() (detection + schema verify) and the health-probe
    refresh + lag read must all be pure reads on a read-only standby."""
    from tests.test_replica_mode import _make_backend, _require_tables

    backend = _make_backend(recovery=True, readonly_conn=True)
    conn = backend._pool.conn
    _require_tables(conn)
    conn.add_extension("vector")

    async def scenario():
        await backend.open()
        assert replica_mode.is_replica() is True
        await replica_mode.health_probe_refresh(backend)
        replica_mode._lag_cache = (0.0, None)
        await replica_mode.replay_lag_seconds(backend)

    asyncio.run(scenario())
    assert conn.write_statements == [], (
        f"startup/health probes must not write on a replica; found: {conn.write_statements}"
    )
    assert backend._schema_ensured is True
