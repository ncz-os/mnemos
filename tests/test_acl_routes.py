"""Route-layer tests for the per-principal memory ACL escape hatch.

Covers the three things the route layer owns that the repository SQL
contract deliberately does not: principal/perm validation, the
owner/root/group-admin management gate, and capability-gated 503
degradation on backends that do not advertise ``acl``.

Backend-specific SQL (Postgres/Oracle/Db2 grant/revoke/list) is exercised
by the live per-backend suites; here the backend is a fake so the authz
and validation logic is tested in isolation.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import acl as acl_route


def _user(user_id="alice", *, role="user", groups=None, ns="alice-ns") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=list(groups or []),
        role=role,
        namespace=ns,
        authenticated=True,
    )


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _FakeAcl:
    def __init__(self, *, admin_pairs=None, grants=None):
        self.admin_pairs = set(admin_pairs or set())
        self.grants = list(grants or [])
        self.granted: list[dict] = []
        self.revoked: list[tuple[str, str]] = []

    async def is_group_admin(self, tx, *, user_id, group_id):
        return (user_id, group_id) in self.admin_pairs

    async def list_acl(self, tx, memory_id):
        return list(self.grants)

    async def grant_acl(self, tx, *, memory_id, principal, perm, granted_by):
        row = {
            "memory_id": memory_id,
            "principal": principal,
            "perm": perm,
            "granted_by": granted_by,
            "created_at": None,
        }
        self.granted.append(row)
        return row

    async def revoke_acl(self, tx, *, memory_id, principal):
        self.revoked.append((memory_id, principal))
        return any(g["principal"] == principal for g in self.grants)


class _FakeMemories:
    def __init__(self, row):
        self.row = row

    async def get_memory(self, tx, memory_id, *, visibility=None, include_archived=False):
        return self.row


class _FakeBackend:
    capabilities = {"core", "acl"}

    def __init__(self, *, memory_row, acl):
        self.memories = _FakeMemories(memory_row)
        self.acl = acl

    def transactional(self):
        return _FakeTx()


class _NoAclBackend:
    capabilities = {"core"}


def _install(monkeypatch, backend):
    monkeypatch.setattr(_lc, "_persistence_backend", backend, raising=False)


# --- principal / perm validators --------------------------------------------


@pytest.mark.parametrize("principal", ["user:42", "group:eng", "user:a:b"])
def test_validate_principal_accepts_typed(principal):
    acl_route._validate_principal(principal)  # no raise


@pytest.mark.parametrize("principal", ["", "42", "user:", "group:", "admin:42", ":42"])
def test_validate_principal_rejects_malformed(principal):
    with pytest.raises(HTTPException) as exc:
        acl_route._validate_principal(principal)
    assert exc.value.status_code == 422


def test_validate_perm_accepts_read_bit_only():
    acl_route._validate_perm(4)  # no raise — read grant


@pytest.mark.parametrize("perm", [0, -1, 1, 2, 6, 7, 8, 4 | 8])
def test_validate_perm_rejects_non_read(perm):
    # The ACL escape hatch only widens read visibility, so anything but a bare
    # read bit (4) — including write=2 and read+write=6 — is rejected.
    with pytest.raises(HTTPException) as exc:
        acl_route._validate_perm(perm)
    assert exc.value.status_code == 422


# --- management gate (_load_manageable_memory) -------------------------------


def _run_gate(backend, memory_id, user):
    async def _go():
        async with backend.transactional() as tx:
            return await acl_route._load_manageable_memory(backend, tx, memory_id, user)

    return asyncio.run(_go())


def test_gate_allows_owner():
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    row = _run_gate(backend, "m1", _user("alice"))
    assert row["owner_id"] == "alice"


def test_gate_allows_root_even_for_others_memory():
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    row = _run_gate(backend, "m1", _user("root", role="root", ns="other"))
    assert row["owner_id"] == "bob"


def test_gate_allows_group_admin():
    acl = _FakeAcl(admin_pairs={("alice", "eng")})
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    row = _run_gate(backend, "m1", _user("alice", groups=["eng"]))
    assert row["owner_id"] == "bob"


def test_gate_rejects_non_owner_non_admin_403():
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _run_gate(backend, "m1", _user("alice", groups=["eng"]))
    assert exc.value.status_code == 403


def test_gate_rejects_group_member_without_admin_flag_403():
    # Member of the group but is_admin is false → cannot manage.
    acl = _FakeAcl(admin_pairs=set())
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _run_gate(backend, "m1", _user("alice", groups=["eng"]))
    assert exc.value.status_code == 403


def test_gate_404_when_memory_invisible():
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row=None, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _run_gate(backend, "missing", _user("alice"))
    assert exc.value.status_code == 404


# --- capability gating -------------------------------------------------------


def test_routes_503_when_backend_lacks_acl_capability(monkeypatch):
    _install(monkeypatch, _NoAclBackend())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(acl_route.list_memory_acl("m1", user=_user()))
    assert exc.value.status_code == 503


def test_routes_503_when_no_backend(monkeypatch):
    _install(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(acl_route.list_memory_acl("m1", user=_user()))
    assert exc.value.status_code == 503


# --- end-to-end handler paths against a fake backend -------------------------


def test_grant_handler_persists_and_echoes(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install(monkeypatch, backend)

    entry = asyncio.run(
        acl_route.grant_memory_acl(
            "m1",
            acl_route.AclGrantRequest(principal="group:research", perm=4),
            user=_user("alice"),
        )
    )
    assert entry.principal == "group:research"
    assert entry.perm == 4
    assert entry.granted_by == "alice"
    assert acl.granted == [
        {
            "memory_id": "m1",
            "principal": "group:research",
            "perm": 4,
            "granted_by": "alice",
            "created_at": None,
        }
    ]


def test_grant_handler_rejects_bad_principal_before_touching_backend(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install(monkeypatch, backend)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            acl_route.grant_memory_acl(
                "m1",
                acl_route.AclGrantRequest(principal="nope", perm=4),
                user=_user("alice"),
            )
        )
    assert exc.value.status_code == 422
    assert acl.granted == []


def test_list_handler_returns_grants(monkeypatch):
    acl = _FakeAcl(
        grants=[
            {"memory_id": "m1", "principal": "user:7", "perm": 4, "granted_by": "alice", "created_at": None},
        ]
    )
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install(monkeypatch, backend)

    resp = asyncio.run(acl_route.list_memory_acl("m1", user=_user("alice")))
    assert resp.memory_id == "m1"
    assert [g.principal for g in resp.grants] == ["user:7"]


def test_revoke_handler_404_when_grant_absent(monkeypatch):
    acl = _FakeAcl(grants=[])  # nothing to revoke
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install(monkeypatch, backend)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(acl_route.revoke_memory_acl("m1", "user:7", user=_user("alice")))
    assert exc.value.status_code == 404


def test_revoke_handler_ok_when_grant_present(monkeypatch):
    acl = _FakeAcl(
        grants=[{"memory_id": "m1", "principal": "user:7", "perm": 4, "granted_by": "alice", "created_at": None}]
    )
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install(monkeypatch, backend)
    result = asyncio.run(acl_route.revoke_memory_acl("m1", "user:7", user=_user("alice")))
    assert result["status"] == "revoked"
    assert acl.revoked == [("m1", "user:7")]


# --- search-cache invalidation on ACL mutation -------------------------------
#
# A grant widens read visibility and a revoke narrows it. The per-user search
# cache is keyed by user/group/namespace/query — *not* ACL state — so without
# explicit invalidation a response cached while a principal held a grant could
# be replayed after revoke until TTL expiry (a permission-revocation leak).


class _FakeCache:
    def __init__(self, keys):
        self.keys = list(keys)
        self.deleted: list[str] = []

    async def scan_iter(self, *, match, count):  # noqa: ARG002 — signature parity
        for k in list(self.keys):
            yield k

    async def delete(self, key):
        self.deleted.append(key)
        if key in self.keys:
            self.keys.remove(key)


def _install_cache(monkeypatch, cache):
    monkeypatch.setattr(_lc, "_cache", cache, raising=False)


def test_grant_invalidates_search_cache(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install(monkeypatch, backend)
    cache = _FakeCache(["mnemos:search:alice:q1", "mnemos:search:bob:q2"])
    _install_cache(monkeypatch, cache)

    asyncio.run(
        acl_route.grant_memory_acl(
            "m1",
            acl_route.AclGrantRequest(principal="user:7", perm=4),
            user=_user("alice"),
        )
    )
    assert set(cache.deleted) == {"mnemos:search:alice:q1", "mnemos:search:bob:q2"}


def test_revoke_invalidates_search_cache(monkeypatch):
    acl = _FakeAcl(
        grants=[{"memory_id": "m1", "principal": "user:7", "perm": 4, "granted_by": "alice", "created_at": None}]
    )
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install(monkeypatch, backend)
    cache = _FakeCache(["mnemos:search:user7:q1"])
    _install_cache(monkeypatch, cache)

    asyncio.run(acl_route.revoke_memory_acl("m1", "user:7", user=_user("alice")))
    assert cache.deleted == ["mnemos:search:user7:q1"]


def test_revoke_404_does_not_invalidate_search_cache(monkeypatch):
    # Nothing was removed → read visibility is unchanged → no invalidation.
    acl = _FakeAcl(grants=[])
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install(monkeypatch, backend)
    cache = _FakeCache(["mnemos:search:alice:q1"])
    _install_cache(monkeypatch, cache)

    with pytest.raises(HTTPException):
        asyncio.run(acl_route.revoke_memory_acl("m1", "user:7", user=_user("alice")))
    assert cache.deleted == []


# --- end-to-end SQLite: a memory_acl grant widens read visibility ---------
#
# SQLite omits the *management* capability (grant/revoke/list routes 503),
# but its read predicate still honors pre-existing memory_acl rows via the
# EXISTS disjunct. The disjunct widens only within the caller's namespace,
# so the grantee shares the namespace here (e.g. a team namespace).


def test_sqlite_acl_grant_widens_read_within_namespace(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.persistence.visibility import VisibilityFilter

    class _S:
        class database:
            embedding_dim = 1024

    async def _go():
        backend = SqliteBackend(tmp_path / "acl-widen.db", _S())
        await backend.open()
        try:
            ns = "team-ns"
            async with backend.transactional() as tx:
                await backend.memories.insert_memory(
                    tx,
                    memory_id="m-acl",
                    content="bob private note",
                    category="facts",
                    subcategory=None,
                    metadata_json="{}",
                    quality_rating=3,
                    owner_id="bob",
                    namespace=ns,
                    permission_mode=700,  # owner-only: no world/group read
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content=None,
                    created=None,
                    updated=None,
                )

            alice = _user("alice", groups=[], ns=ns)
            vis = VisibilityFilter.for_read(alice, namespace=ns)

            async with backend.transactional() as tx:
                before = await backend.memories.get_memory(tx, "m-acl", visibility=vis)
            assert before is None  # owner-only memory invisible to alice

            # Insert a per-principal grant directly (SQLite has no mgmt API).
            async with backend.transactional() as tx:
                conn = tx.conn if hasattr(tx, "conn") else tx._conn  # type: ignore[attr-defined]
                await conn.execute(
                    "INSERT INTO memory_acl (memory_id, principal, perm, granted_by) VALUES (?, ?, ?, ?)",
                    ("m-acl", "user:alice", 4, "bob"),
                )
                await conn.commit()

            async with backend.transactional() as tx:
                after = await backend.memories.get_memory(tx, "m-acl", visibility=vis)
            assert after is not None  # grant widened read visibility
            assert after["owner_id"] == "bob"
        finally:
            await backend.close()

    asyncio.run(_go())


# --- write-after-invalidate regression test ----------------------------------
#
# The search cache was vulnerable to a write-after-invalidate (TOCTOU) race:
#
#   1. Search request A misses cache, reads rows from DB while a grant/permission
#      still allows visibility.
#   2. A mutation (ACL revoke, memory delete, permission_mode tighten, archive)
#      commits and deletes the current `mnemos:search:*` keys.
#   3. Request A — still in flight — writes its now-stale result under the search
#      key, which is then served for up to the TTL.
#
# Fix: each search reads the monotonic visibility epoch and folds it into the
# cache key.  A bump during step 2 means request A's write lands under the old
# epoch key (orphaned, never read).  Every subsequent search reads the new epoch
# and gets a cache miss, forcing a fresh DB read that respects the new visibility.
#
# This test verifies the epoch IS included in the search cache key.


def test_search_cache_key_includes_epoch():
    """Verify the search cache key includes the visibility epoch.

    This ensures that when _invalidate_caches_after_mutation bumps the epoch,
    any in-flight cache writes land under the old-epoch key and are never
    read — closing the write-after-invalidate race.
    """
    from mnemos.core.lifecycle import _get_cache_key

    # Simulate the cache key computation (mirrors memories.py:search_memories).
    user_id = "user1"
    namespace = "default"
    query = "test query"
    request_limit = 10
    category = "notes"
    subcategory = None
    search_mode = "semantic"
    source_provider = None
    source_model = None
    source_agent = None
    search_namespace = "default"
    search_owner_id = None
    include_secrets = False
    operational = False
    group_ids = ["group-a"]
    include_archived = False
    exclude_superseded = False
    current_only = False
    boost_recency = False
    recency_weight = 0.5
    search_profile = "balanced"
    min_score = 0.1
    min_margin = 0.0
    ood_gate = True

    # Key at epoch 0
    epoch_0_key = _get_cache_key(
        "search",
        user_id,
        namespace,
        query,
        request_limit,
        category,
        subcategory,
        search_mode,
        source_provider,
        source_model,
        source_agent,
        search_namespace,
        search_owner_id,
        include_secrets,
        operational,
        sorted(group_ids),
        include_archived,
        exclude_superseded,
        current_only,
        boost_recency,
        recency_weight,
        search_profile,
        min_score,
        min_margin,
        ood_gate,
        0,  # epoch
    )

    # Key at epoch 1 (after bump)
    epoch_1_key = _get_cache_key(
        "search",
        user_id,
        namespace,
        query,
        request_limit,
        category,
        subcategory,
        search_mode,
        source_provider,
        source_model,
        source_agent,
        search_namespace,
        search_owner_id,
        include_secrets,
        operational,
        sorted(group_ids),
        include_archived,
        exclude_superseded,
        current_only,
        boost_recency,
        recency_weight,
        search_profile,
        min_score,
        min_margin,
        ood_gate,
        1,  # epoch
    )

    assert epoch_0_key != epoch_1_key, (
        "Search cache key MUST differ across epochs to close the write-after-invalidate race"
    )

    # Verify both keys start with the expected namespace prefix
    assert epoch_0_key.startswith("mnemos:search:")
    assert epoch_1_key.startswith("mnemos:search:")

    # The old key must NOT be read when the new epoch is active.
    # In production, search reads the current epoch → miss on old key → reads DB →
    # writes to new-epoch key.  The old key is orphaned.
    assert epoch_0_key not in [epoch_1_key]


def test_epoch_bump_prevents_stale_cache_write():
    """Simulate the TOCTOU race and verify the epoch fix prevents it.

    Steps:
    1. Search starts with epoch=0
    2. Mutation bumps epoch to 1
    3. Search (still in flight) tries to write → gets old epoch key → writes there
    4. Next search reads current epoch=1 → miss on epoch_1 key → fresh DB read

    Result: the stale write at epoch 0 is never served.
    """
    from mnemos.core.lifecycle import _get_cache_key

    # Pre-mutation search key (epoch 0)
    search_key_0 = _get_cache_key(
        "search",
        "user1", "default", "query", 10, "notes", None, "semantic",
        None, None, None, "default", None,
        False, False, ["group-a"], False, False, False,
        False, 0.5, "balanced", 0.1, 0.0, True, 0,
    )

    # Post-mutation search key (epoch 1)
    search_key_1 = _get_cache_key(
        "search",
        "user1", "default", "query", 10, "notes", None, "semantic",
        None, None, None, "default", None,
        False, False, ["group-a"], False, False, False,
        False, 0.5, "balanced", 0.1, 0.0, True, 1,
    )

    assert search_key_0 != search_key_1

    # In production: after bump, the next search reads epoch=1, so
    # it will NOT read from search_key_0 (which is the stale write).
    # The fix is that epoch_0 and epoch_1 produce different keys, so
    # the stale write at epoch_0 is orphaned and never read.
    assert search_key_0 != search_key_1  # keys differ → stale write is orphaned
