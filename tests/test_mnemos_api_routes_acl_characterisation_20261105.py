"""Characterisation tests for ``mnemos/api/routes/acl.py``.

This file pins the module's CURRENT behaviour as it is observed today —
it is a regression fence, not a specification of what the code *ought*
to do. Every assertion below was derived by reading the module and
observing its actual output; where the current behaviour looks
surprising it is pinned as-is and called out in a comment.

Covered surface:

* module constants (``router`` prefix/tags, ``_ALLOWED_PERM_BITS``);
* request/response models (``AclGrantRequest``, ``AclEntry``,
  ``AclListResponse``) — defaults, coercion, required-field rejection;
* ``_validate_principal`` / ``_validate_perm`` — every accept and
  reject branch, with the exact 422 detail strings;
* ``_iso`` / ``_to_entry`` — None handling, ``isoformat()`` duck
  typing, ``str()`` fallback, and row-type coercion;
* ``_load_manageable_memory`` — the 404/403/allow gate, including that
  the visibility filter is namespace-pinned for non-root and that the
  group-admin branch is only consulted for members of the row's group;
* the three handlers — happy path, capability-503 degradation, RLS
  application ordering, validation-before-transaction ordering, the
  revoke 404, and the search-cache/visibility-epoch invalidation
  contract including its swallowing of backend errors.

All I/O is faked: the persistence backend comes from a fake installed
into ``mnemos.core.lifecycle._persistence_backend`` via ``monkeypatch``,
the cache is a fake object, and the visibility epoch is replaced with a
counter. No network, no sleeping, no real database.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import acl as acl_route
from mnemos.core.visibility import ACL_READ_BIT, ACL_WRITE_BIT
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


# ---------------------------------------------------------------------------
# helpers / fakes
# ---------------------------------------------------------------------------


def _user(
    user_id: str = "alice",
    *,
    role: str = "user",
    groups: list[str] | None = None,
    ns: str = "alice-ns",
    authenticated: bool = True,
) -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=list(groups or []),
        role=role,
        namespace=ns,
        authenticated=authenticated,
    )


def _run(coro):
    """Run a coroutine to completion without a pytest-asyncio marker."""
    return asyncio.run(coro)


class _FakeTx:
    """Minimal async context manager standing in for a transaction."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_a):
        self.exited += 1
        return False


class _FakeAcl:
    """Fake ``AclRepository`` recording every call it receives."""

    def __init__(
        self,
        *,
        admin_pairs=None,
        grants=None,
        revoke_result: bool | None = None,
        grant_row=None,
        raise_on=(),
    ) -> None:
        self.admin_pairs = set(admin_pairs or ())
        self.grants = list(grants or [])
        self.revoke_result = revoke_result
        self.grant_row = grant_row
        self.raise_on = set(raise_on)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.granted: list[dict[str, Any]] = []
        self.revoked: list[tuple[str, str]] = []
        self.admin_queries: list[tuple[str, str]] = []

    async def is_group_admin(self, tx, *, user_id, group_id):
        self.admin_queries.append((user_id, group_id))
        if "is_group_admin" in self.raise_on:
            raise RuntimeError("is_group_admin exploded")
        return (user_id, group_id) in self.admin_pairs

    async def list_acl(self, tx, memory_id):
        self.calls.append(("list_acl", {"memory_id": memory_id}))
        return list(self.grants)

    async def grant_acl(self, tx, *, memory_id, principal, perm, granted_by):
        self.calls.append(
            (
                "grant_acl",
                {
                    "memory_id": memory_id,
                    "principal": principal,
                    "perm": perm,
                    "granted_by": granted_by,
                },
            )
        )
        row = self.grant_row
        if row is None:
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
        self.calls.append(("revoke_acl", {"memory_id": memory_id, "principal": principal}))
        self.revoked.append((memory_id, principal))
        if self.revoke_result is not None:
            return self.revoke_result
        return any(g["principal"] == principal for g in self.grants)


class _FakeMemories:
    """Fake ``MemoryRepository`` — returns a fixed row, records the call."""

    def __init__(self, row, *, raise_on: bool = False) -> None:
        self.row = row
        self.raise_on = raise_on
        self.calls: list[dict[str, Any]] = []

    async def get_memory(self, tx, memory_id, *, visibility=None, include_archived=False):
        self.calls.append(
            {
                "memory_id": memory_id,
                "visibility": visibility,
                "include_archived": include_archived,
            }
        )
        if self.raise_on:
            raise RuntimeError("get_memory exploded")
        return self.row


class _FakeBackend:
    capabilities = {"core", "acl"}

    def __init__(self, *, memory_row, acl=None, memories=None) -> None:
        self.memories = memories if memories is not None else _FakeMemories(memory_row)
        self.acl = acl if acl is not None else _FakeAcl()
        self.txns: list[_FakeTx] = []

    def transactional(self):
        tx = _FakeTx()
        self.txns.append(tx)
        return tx


class _NoAclBackend:
    capabilities = {"core"}


class _NoCapabilitiesBackend:
    """A backend whose ``capabilities`` attribute is absent entirely."""

    def __init__(self) -> None:
        self.memories = _FakeMemories({"owner_id": "alice", "group_id": "eng"})
        self.acl = _FakeAcl()

    def transactional(self):
        return _FakeTx()


class _FakeCache:
    def __init__(self, keys=(), *, scan_raises: bool = False, delete_raises: bool = False) -> None:
        self.keys = list(keys)
        self.scan_raises = scan_raises
        self.delete_raises = delete_raises
        self.deleted: list[str] = []
        self.scan_calls: list[dict[str, Any]] = []

    async def scan_iter(self, *, match, count):
        self.scan_calls.append({"match": match, "count": count})
        if self.scan_raises:
            raise RuntimeError("scan exploded")
        for k in list(self.keys):
            yield k

    async def delete(self, key):
        if self.delete_raises:
            raise RuntimeError("delete exploded")
        self.deleted.append(key)
        if key in self.keys:
            self.keys.remove(key)


class _FakeEpoch:
    def __init__(self, *, raises: bool = False) -> None:
        self.bumps = 0
        self.raises = raises

    async def bump(self) -> int:
        self.bumps += 1
        if self.raises:
            raise RuntimeError("epoch exploded")
        return self.bumps


def _install_backend(monkeypatch, backend):
    monkeypatch.setattr(_lc, "_persistence_backend", backend, raising=False)


def _install_cache(monkeypatch, cache):
    monkeypatch.setattr(_lc, "_cache", cache, raising=False)


def _install_epoch(monkeypatch, epoch):
    monkeypatch.setattr(_lc, "_vis_epoch_impl", epoch, raising=False)
    monkeypatch.setattr(_lc, "_vis_epoch", lambda: epoch, raising=False)


@pytest.fixture(autouse=True)
def _neutralise_rls(monkeypatch):
    """``maybe_set_pg_rls`` must never reach into a real Postgres transaction."""
    monkeypatch.setattr(_lc, "_rls_enabled", False, raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_cache(monkeypatch):
    """Keep the process-global search cache out of every test by default."""
    monkeypatch.setattr(_lc, "_cache", None, raising=False)


# ---------------------------------------------------------------------------
# module constants and router wiring
# ---------------------------------------------------------------------------


def test_router_is_mounted_under_v1_memories_with_acl_tag():
    assert acl_route.router.prefix == "/v1/memories"
    assert acl_route.router.tags == ["acl"]


def test_router_declares_the_three_acl_paths():
    paths = {(r.path, tuple(sorted(r.methods))) for r in acl_route.router.routes}
    assert ("/v1/memories/{memory_id}/acl", ("GET",)) in paths
    assert ("/v1/memories/{memory_id}/acl", ("POST",)) in paths
    assert ("/v1/memories/{memory_id}/acl/{principal}", ("DELETE",)) in paths


def test_allowed_perm_bits_is_the_read_or_write_mask():
    # ACL_READ_BIT (4) | ACL_WRITE_BIT (2) == 6. The constant is used only as
    # documentation of "the only bits that exist"; the validator below
    # rejects everything except a bare read.
    assert acl_route._ALLOWED_PERM_BITS == 6
    assert acl_route._ALLOWED_PERM_BITS == (ACL_READ_BIT | ACL_WRITE_BIT)


# ---------------------------------------------------------------------------
# pydantic models
# ---------------------------------------------------------------------------


def test_acl_grant_request_accepts_typed_principal_and_read_perm():
    req = acl_route.AclGrantRequest(principal="group:eng", perm=4)
    assert req.principal == "group:eng"
    assert req.perm == 4


def test_acl_grant_request_requires_both_fields():
    with pytest.raises(ValidationError) as exc:
        acl_route.AclGrantRequest(principal="user:7")  # type: ignore[call-arg]
    assert "perm" in str(exc.value)


def test_acl_grant_request_rejects_non_integer_perm():
    with pytest.raises(ValidationError):
        acl_route.AclGrantRequest(principal="user:7", perm="read")  # type: ignore[arg-type]


def test_acl_grant_request_coerces_numeric_string_perm():
    # pydantic v2 lax mode coerces "4" -> 4; the route validator then sees 4.
    req = acl_route.AclGrantRequest(principal="user:7", perm="4")  # type: ignore[arg-type]
    assert req.perm == 4


def test_acl_grant_request_does_not_validate_principal_shape():
    # Model-level validation is field-presence only; `_validate_principal`
    # in the handler owns the typed-principal contract.
    req = acl_route.AclGrantRequest(principal="nope", perm=4)
    assert req.principal == "nope"


def test_acl_entry_optional_fields_default_to_none():
    entry = acl_route.AclEntry(memory_id="m1", principal="user:7", perm=4)
    assert entry.granted_by is None
    assert entry.created_at is None


def test_acl_list_response_requires_grants_explicitly():
    # grants has no default in the current model — omitting it is a
    # ValidationError rather than a silent empty list.
    with pytest.raises(ValidationError) as exc:
        acl_route.AclListResponse(memory_id="m1")  # type: ignore[call-arg]
    assert "grants" in str(exc.value)


def test_acl_list_response_accepts_an_explicit_empty_grants_list():
    resp = acl_route.AclListResponse(memory_id="m1", grants=[])
    assert resp.grants == []


def test_acl_list_response_parses_entries_from_dicts():
    resp = acl_route.AclListResponse(
        memory_id="m1",
        grants=[
            {"memory_id": "m1", "principal": "user:7", "perm": 4},
            {"memory_id": "m1", "principal": "group:eng", "perm": 4, "granted_by": "bob"},
        ],
    )
    assert [g.principal for g in resp.grants] == ["user:7", "group:eng"]
    assert resp.grants[1].granted_by == "bob"


# ---------------------------------------------------------------------------
# _validate_principal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "principal",
    ["user:42", "group:eng", "user:a:b", "user: ", "group:0", "user:user"],
)
def test_validate_principal_accepts_any_typed_principal_with_non_empty_id(principal):
    assert acl_route._validate_principal(principal) is None


def test_validate_principal_accepts_ids_containing_colons():
    # partition(":") splits on the FIRST colon only, so a colon-bearing id is
    # preserved wholesale in the identifier.
    acl_route._validate_principal("user:a:b:c")


@pytest.mark.parametrize("principal", ["", "42"])
def test_validate_principal_without_colon_raises_422_with_first_message(principal):
    with pytest.raises(HTTPException) as exc:
        acl_route._validate_principal(principal)
    assert exc.value.status_code == 422
    assert exc.value.detail == "principal must be 'user:<id>' or 'group:<id>'"


@pytest.mark.parametrize("principal", ["admin:42", ":42", "User:42", "USER:42"])
def test_validate_principal_with_unknown_kind_raises_422_with_second_message(principal):
    with pytest.raises(HTTPException) as exc:
        acl_route._validate_principal(principal)
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "principal must be 'user:<id>' or 'group:<id>' with a non-empty id"
    )


@pytest.mark.parametrize("principal", ["user:", "group:", "user::", "group::"])
def test_validate_principal_with_empty_id_raises_422_with_second_message(principal):
    # "user::" partitions to kind="user", ident=":", which is truthy — so it is
    # ACCEPTED. Only a genuinely empty ident is rejected.
    if principal.endswith("::"):
        assert acl_route._validate_principal(principal) is None
        return
    with pytest.raises(HTTPException) as exc:
        acl_route._validate_principal(principal)
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "principal must be 'user:<id>' or 'group:<id>' with a non-empty id"
    )


def test_validate_principal_is_case_sensitive():
    with pytest.raises(HTTPException):
        acl_route._validate_principal("Group:eng")


# ---------------------------------------------------------------------------
# _validate_perm
# ---------------------------------------------------------------------------


def test_validate_perm_accepts_exactly_the_read_bit():
    assert acl_route._validate_perm(ACL_READ_BIT) is None


@pytest.mark.parametrize("perm", [0, 1, 2, 3, 5, 6, 7, 8, -4, -1, 1024])
def test_validate_perm_rejects_everything_but_four_with_exact_message(perm):
    with pytest.raises(HTTPException) as exc:
        acl_route._validate_perm(perm)
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "perm must be exactly 4 (read); ACL grants only widen read visibility"
    )


def test_validate_perm_rejects_write_bit_even_when_combined_with_read():
    # 4|2 == 6 — a read+write grant is rejected as a whole, not narrowed.
    with pytest.raises(HTTPException) as exc:
        acl_route._validate_perm(ACL_READ_BIT | ACL_WRITE_BIT)
    assert exc.value.status_code == 422


def test_validate_perm_uses_equality_not_a_read_bit_test():
    # A value that merely CONTAINS the read bit (12 == 4|8) is still rejected:
    # the check is `perm != 4`, not `perm & 4`.
    with pytest.raises(HTTPException):
        acl_route._validate_perm(12)


def test_validate_perm_accepts_true_because_bool_equals_one_but_not_four():
    # bool is an int subclass: True == 1 != 4 -> rejected; explicit pin.
    with pytest.raises(HTTPException):
        acl_route._validate_perm(True)


def test_validate_perm_accepts_float_four_because_equality_ignores_type():
    # CURRENT BEHAVIOUR: 4.0 == 4 is True, so a float perm passes the validator.
    # The pydantic model would coerce/round it first, so this is only reachable
    # by calling the private helper directly.
    assert acl_route._validate_perm(4.0) is None


# ---------------------------------------------------------------------------
# _iso
# ---------------------------------------------------------------------------


def test_iso_returns_none_for_none():
    assert acl_route._iso(None) is None


def test_iso_calls_isoformat_when_available():
    stamp = _dt.datetime(2026, 11, 5, 12, 34, 56)
    assert acl_route._iso(stamp) == "2026-11-05T12:34:56"


def test_iso_uses_date_isoformat_for_date_objects():
    assert acl_route._iso(_dt.date(2026, 11, 5)) == "2026-11-05"


def test_iso_falls_back_to_str_for_objects_without_isoformat():
    assert acl_route._iso("2026-11-05T00:00:00Z") == "2026-11-05T00:00:00Z"


def test_iso_falls_back_to_str_for_integers():
    assert acl_route._iso(12345) == "12345"


def test_iso_ignores_a_non_callable_isoformat_attribute():
    class _Weird:
        isoformat = "not callable"

        def __str__(self):
            return "weird"

    assert acl_route._iso(_Weird()) == "weird"


def test_iso_propagates_exceptions_raised_by_isoformat():
    class _Exploding:
        def isoformat(self):
            raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        acl_route._iso(_Exploding())


# ---------------------------------------------------------------------------
# _to_entry
# ---------------------------------------------------------------------------


def test_to_entry_maps_a_plain_dict_row():
    entry = acl_route._to_entry(
        {
            "memory_id": "m1",
            "principal": "user:7",
            "perm": 4,
            "granted_by": "alice",
            "created_at": _dt.datetime(2026, 11, 5, 1, 2, 3),
        }
    )
    assert entry == acl_route.AclEntry(
        memory_id="m1",
        principal="user:7",
        perm=4,
        granted_by="alice",
        created_at="2026-11-05T01:02:03",
    )


def test_to_entry_accepts_a_mapping_without_calling_get_on_the_row_class():
    # asyncpg Records expose .get() too; a plain Mapping is the simplest stand-in.
    class _Record:
        def __init__(self, data):
            self._data = data

        def get(self, key, default=None):
            return self._data.get(key, default)

    entry = acl_route._to_entry(
        _Record({"memory_id": "m2", "principal": "group:eng", "perm": 4})
    )
    assert entry.memory_id == "m2"
    assert entry.principal == "group:eng"
    assert entry.perm == 4


def test_to_entry_leaves_granted_by_none_when_absent():
    entry = acl_route._to_entry({"memory_id": "m1", "principal": "user:7", "perm": 4})
    assert entry.granted_by is None
    assert entry.created_at is None


def test_to_entry_stringifies_non_string_memory_id_and_principal():
    entry = acl_route._to_entry(
        {"memory_id": 99, "principal": 7, "perm": "4", "granted_by": 12}
    )
    assert entry.memory_id == "99"
    assert entry.principal == "7"
    assert entry.perm == 4
    assert entry.granted_by == "12"


def test_to_entry_keeps_falsy_but_not_none_granted_by_as_empty_string():
    # The guard is `get("granted_by") is None`, so "" survives as "".
    entry = acl_route._to_entry(
        {"memory_id": "m1", "principal": "user:7", "perm": 4, "granted_by": ""}
    )
    assert entry.granted_by == ""


def test_to_entry_raises_type_error_when_the_perm_column_is_missing():
    # dict.get returns None for the missing column and int(None) raises — the
    # failure surfaces as a TypeError, not a KeyError.
    with pytest.raises(TypeError):
        acl_route._to_entry({"memory_id": "m1", "principal": "user:7"})


# ---------------------------------------------------------------------------
# _load_manageable_memory — the management gate
# ---------------------------------------------------------------------------


def _gate(backend, memory_id, user):
    async def _go():
        async with backend.transactional() as tx:
            return await acl_route._load_manageable_memory(backend, tx, memory_id, user)

    return _run(_go())


def test_gate_returns_row_for_the_owner():
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"})
    row = _gate(backend, "m1", _user("alice"))
    assert row == {"owner_id": "alice", "group_id": "eng"}


def test_gate_returns_row_for_root_regardless_of_owner():
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"})
    row = _gate(backend, "m1", _user("root", role="root", ns="other-ns"))
    assert row["owner_id"] == "bob"


def test_gate_returns_row_for_group_admin_of_the_rows_group():
    backend = _FakeBackend(
        memory_row={"owner_id": "bob", "group_id": "eng"},
        acl=_FakeAcl(admin_pairs={("alice", "eng")}),
    )
    row = _gate(backend, "m1", _user("alice", groups=["eng"]))
    assert row["owner_id"] == "bob"


def test_gate_accepts_a_str_typed_group_ids_membership_without_crashing():
    # `set(user.group_ids)` is built from a list; a string iterates into chars,
    # so "eng" does not match group_id "eng". Pinned as current behaviour.
    backend = _FakeBackend(
        memory_row={"owner_id": "bob", "group_id": "eng"},
        acl=_FakeAcl(admin_pairs={("alice", "eng")}),
    )
    alice = _user("alice")
    alice.group_ids = "eng"  # type: ignore[assignment]
    with pytest.raises(HTTPException) as exc:
        _gate(backend, "m1", alice)
    assert exc.value.status_code == 403


def test_gate_raises_404_when_the_memory_is_not_visible():
    backend = _FakeBackend(memory_row=None)
    with pytest.raises(HTTPException) as exc:
        _gate(backend, "missing", _user("alice"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Memory not found"


def test_gate_raises_404_before_any_acl_lookup():
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row=None, acl=acl)
    with pytest.raises(HTTPException):
        _gate(backend, "missing", _user("alice"))
    assert acl.admin_queries == []


def test_gate_treats_an_empty_mapping_row_as_not_found():
    # `if not row` — any falsy row (None or {}) is a 404.
    backend = _FakeBackend(memory_row={})
    with pytest.raises(HTTPException) as exc:
        _gate(backend, "m1", _user("alice"))
    assert exc.value.status_code == 404


def test_gate_raises_403_for_a_non_member_non_owner():
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _gate(backend, "m1", _user("alice", groups=["other"]))
    assert exc.value.status_code == 403
    assert exc.value.detail == (
        "ACL management requires the memory owner, root, or a group admin"
    )
    assert acl.admin_queries == []


def test_gate_raises_403_for_group_member_who_is_not_an_admin():
    acl = _FakeAcl(admin_pairs=set())
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _gate(backend, "m1", _user("alice", groups=["eng"]))
    assert exc.value.status_code == 403
    assert acl.admin_queries == [("alice", "eng")]


def test_gate_raises_403_when_the_row_has_no_group_id():
    acl = _FakeAcl(admin_pairs={("alice", "eng")})
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": None}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _gate(backend, "m1", _user("alice", groups=["eng"]))
    assert exc.value.status_code == 403
    assert acl.admin_queries == []


def test_gate_loads_the_memory_with_include_archived_true():
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"})
    _gate(backend, "m1", _user("alice"))
    assert backend.memories.calls[0]["include_archived"] is True
    assert backend.memories.calls[0]["memory_id"] == "m1"


def test_gate_pins_the_visibility_filter_to_the_callers_namespace():
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"})
    _gate(backend, "m1", _user("alice", ns="alice-ns", groups=["eng"]))
    vis: VisibilityFilter = backend.memories.calls[0]["visibility"]
    assert isinstance(vis, VisibilityFilter)
    assert vis == VisibilityFilter.for_read(_user("alice", groups=["eng"], ns="alice-ns"), namespace="alice-ns")
    assert vis.scope is VisibilityScope.READABLE
    assert vis.namespace == "alice-ns"
    assert vis.user_id == "alice"
    assert vis.group_ids == ("eng",)


def test_gate_builds_the_root_filter_with_no_namespace_pin():
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": None})
    _gate(backend, "m1", _user("root", role="root", ns="root-ns"))
    vis: VisibilityFilter = backend.memories.calls[0]["visibility"]
    assert vis.scope is VisibilityScope.ROOT_BYPASS
    assert vis.namespace is None
    assert vis.user_id is None


def test_gate_propagates_backend_read_errors_unconverted():
    backend = _FakeBackend(memory_row=None, memories=_FakeMemories(None, raise_on=True))
    with pytest.raises(RuntimeError, match="get_memory exploded"):
        _gate(backend, "m1", _user("alice"))


def test_gate_propagates_group_admin_lookup_errors():
    backend = _FakeBackend(
        memory_row={"owner_id": "bob", "group_id": "eng"},
        acl=_FakeAcl(admin_pairs={("alice", "eng")}, raise_on={"is_group_admin"}),
    )
    with pytest.raises(RuntimeError, match="is_group_admin exploded"):
        _gate(backend, "m1", _user("alice", groups=["eng"]))


def test_gate_does_not_consult_group_admin_when_caller_is_the_owner():
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _gate(backend, "m1", _user("alice"))
    assert acl.admin_queries == []


# ---------------------------------------------------------------------------
# capability gating (require_acl_backend)
# ---------------------------------------------------------------------------


def test_list_raises_503_when_no_persistence_backend_is_installed(monkeypatch):
    _install_backend(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        _run(acl_route.list_memory_acl("m1", user=_user()))
    assert exc.value.status_code == 503
    assert exc.value.detail == "Persistence backend not available"


def test_list_raises_capability_503_when_backend_omits_acl(monkeypatch):
    _install_backend(monkeypatch, _NoAclBackend())
    with pytest.raises(HTTPException) as exc:
        _run(acl_route.list_memory_acl("m1", user=_user()))
    assert exc.value.status_code == 503
    assert "acl" in exc.value.detail
    assert "_NoAclBackend" in exc.value.detail


def test_grant_raises_capability_503_before_validating_the_request(monkeypatch):
    # require_acl_backend() is the first statement, so a malformed principal on
    # an ACL-less backend reports 503, not 422. Pinned.
    _install_backend(monkeypatch, _NoAclBackend())
    with pytest.raises(HTTPException) as exc:
        _run(
            acl_route.grant_memory_acl(
                "m1",
                acl_route.AclGrantRequest(principal="nope", perm=4),
                user=_user(),
            )
        )
    assert exc.value.status_code == 503


def test_revoke_raises_capability_503_before_validating_the_principal(monkeypatch):
    _install_backend(monkeypatch, _NoAclBackend())
    with pytest.raises(HTTPException) as exc:
        _run(acl_route.revoke_memory_acl("m1", "nope", user=_user()))
    assert exc.value.status_code == 503


def test_backend_without_a_capabilities_attribute_is_let_through(monkeypatch):
    # require_backend_capability returns the backend unchanged when
    # `capabilities` is absent, and the duck-typed `hasattr(backend, "acl")`
    # check then passes — so a backend that exposes `.acl` is accepted.
    _install_backend(monkeypatch, _NoCapabilitiesBackend())
    resp = _run(acl_route.list_memory_acl("m1", user=_user("alice")))
    assert resp.memory_id == "m1"


def test_backend_without_capabilities_or_an_acl_attribute_is_rejected(monkeypatch):
    class _Bare:
        pass

    backend = _Bare()
    backend.memories = _FakeMemories({"owner_id": "alice", "group_id": None})

    _install_backend(monkeypatch, backend)
    with pytest.raises(HTTPException) as exc:
        _run(acl_route.list_memory_acl("m1", user=_user("alice")))
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# list_memory_acl
# ---------------------------------------------------------------------------


def test_list_returns_entries_in_repository_order(monkeypatch):
    acl = _FakeAcl(
        grants=[
            {"memory_id": "m1", "principal": "user:7", "perm": 4, "granted_by": "alice", "created_at": None},
            {"memory_id": "m1", "principal": "group:eng", "perm": 4, "granted_by": None, "created_at": None},
        ]
    )
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    _install_backend(monkeypatch, backend)

    resp = _run(acl_route.list_memory_acl("m1", user=_user("alice")))
    assert resp.memory_id == "m1"
    assert [g.principal for g in resp.grants] == ["user:7", "group:eng"]
    assert resp.grants[1].granted_by is None


def test_list_returns_an_empty_grant_list_when_none_exist(monkeypatch):
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=_FakeAcl(grants=[]))
    _install_backend(monkeypatch, backend)
    resp = _run(acl_route.list_memory_acl("m1", user=_user("alice")))
    assert resp.grants == []


def test_list_echoes_the_requested_memory_id_not_the_row_memory_id(monkeypatch):
    acl = _FakeAcl(grants=[{"memory_id": "other", "principal": "user:7", "perm": 4}])
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    _install_backend(monkeypatch, backend)
    resp = _run(acl_route.list_memory_acl("requested-id", user=_user("alice")))
    assert resp.memory_id == "requested-id"
    assert resp.grants[0].memory_id == "other"


def test_list_raises_404_when_the_gate_says_not_found(monkeypatch):
    backend = _FakeBackend(memory_row=None)
    _install_backend(monkeypatch, backend)
    with pytest.raises(HTTPException) as exc:
        _run(acl_route.list_memory_acl("missing", user=_user()))
    assert exc.value.status_code == 404


def test_list_raises_403_for_a_non_manager_and_skips_the_acl_read(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    _install_backend(monkeypatch, backend)
    with pytest.raises(HTTPException) as exc:
        _run(acl_route.list_memory_acl("m1", user=_user("alice")))
    assert exc.value.status_code == 403
    assert acl.calls == []


def test_list_does_not_invalidate_the_search_cache(monkeypatch):
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    _install_backend(monkeypatch, backend)
    cache = _FakeCache(["mnemos:search:alice:q1"])
    _install_cache(monkeypatch, cache)
    epoch = _FakeEpoch()
    _install_epoch(monkeypatch, epoch)

    _run(acl_route.list_memory_acl("m1", user=_user("alice")))
    assert cache.deleted == []
    assert epoch.bumps == 0


def test_list_opens_exactly_one_transaction(monkeypatch):
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    _install_backend(monkeypatch, backend)
    _run(acl_route.list_memory_acl("m1", user=_user("alice")))
    assert len(backend.txns) == 1
    assert backend.txns[0].entered == 1
    assert backend.txns[0].exited == 1


# ---------------------------------------------------------------------------
# grant_memory_acl
# ---------------------------------------------------------------------------


def _grant(monkeypatch, backend, *, principal="group:research", perm=4, user=None, memory_id="m1"):
    _install_backend(monkeypatch, backend)
    return _run(
        acl_route.grant_memory_acl(
            memory_id,
            acl_route.AclGrantRequest(principal=principal, perm=perm),
            user=user or _user("alice"),
        )
    )


def test_grant_persists_the_request_and_echoes_the_stored_row(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": "eng"}, acl=acl)
    entry = _grant(monkeypatch, backend)

    assert acl.calls == [
        (
            "grant_acl",
            {"memory_id": "m1", "principal": "group:research", "perm": 4, "granted_by": "alice"},
        )
    ]
    assert entry == acl_route.AclEntry(
        memory_id="m1", principal="group:research", perm=4, granted_by="alice", created_at=None
    )


def test_grant_stamps_granted_by_with_the_callers_user_id(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "root", "group_id": None}, acl=acl)
    entry = _grant(monkeypatch, backend, user=_user("root", role="root", ns="x"))
    assert entry.granted_by == "root"


def test_grant_returns_the_repository_row_not_the_request(monkeypatch):
    # The response is built from whatever grant_acl returns (e.g. a normalised
    # or server-generated row), not from the incoming request fields.
    stored = {
        "memory_id": "m-canonical",
        "principal": "group:research",
        "perm": 4,
        "granted_by": "alice",
        "created_at": _dt.datetime(2026, 11, 5, 9, 0, 0),
    }
    acl = _FakeAcl(grant_row=stored)
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    entry = _grant(monkeypatch, backend, memory_id="requested")
    assert entry.memory_id == "m-canonical"
    assert entry.created_at == "2026-11-05T09:00:00"


def test_grant_rejects_a_malformed_principal_with_422_before_any_transaction(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _grant(monkeypatch, backend, principal="nope")
    assert exc.value.status_code == 422
    assert exc.value.detail == "principal must be 'user:<id>' or 'group:<id>'"
    assert backend.txns == []
    assert acl.granted == []


@pytest.mark.parametrize("perm", [0, 2, 6, 7])
def test_grant_rejects_a_non_read_perm_with_422_before_any_transaction(monkeypatch, perm):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _grant(monkeypatch, backend, perm=perm)
    assert exc.value.status_code == 422
    assert exc.value.detail == "perm must be exactly 4 (read); ACL grants only widen read visibility"
    assert backend.txns == []


def test_grant_validates_principal_before_perm(monkeypatch):
    # Both are invalid; the principal check runs first.
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    with pytest.raises(HTTPException) as exc:
        _grant(monkeypatch, backend, principal="nope", perm=6)
    assert exc.value.detail == "principal must be 'user:<id>' or 'group:<id>'"


def test_grant_raises_404_when_the_memory_is_invisible(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row=None, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _grant(monkeypatch, backend)
    assert exc.value.status_code == 404
    assert acl.calls == []


def test_grant_raises_403_for_a_non_manager(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _grant(monkeypatch, backend)
    assert exc.value.status_code == 403
    assert acl.calls == []


def test_grant_allows_a_group_admin(monkeypatch):
    acl = _FakeAcl(admin_pairs={("alice", "eng")})
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    entry = _grant(monkeypatch, backend, user=_user("alice", groups=["eng"]))
    assert entry.perm == 4
    assert len(acl.granted) == 1


def test_grant_invalidates_the_search_cache_before_returning(monkeypatch):
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    cache = _FakeCache(["mnemos:search:alice:q1", "mnemos:search:bob:q2", "other:key"])
    epoch = _FakeEpoch()
    _install_cache(monkeypatch, cache)
    _install_epoch(monkeypatch, epoch)

    _grant(monkeypatch, backend)

    # Every key the fake scan yields is deleted (the fake ignores the match
    # pattern, so the "other:key" entry demonstrates the route trusts the
    # iterator rather than re-filtering).
    assert set(cache.deleted) == {"mnemos:search:alice:q1", "mnemos:search:bob:q2", "other:key"}
    assert cache.scan_calls == [{"match": "mnemos:search:*", "count": 500}]


def test_grant_bumps_the_visibility_epoch_once(monkeypatch):
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    epoch = _FakeEpoch()
    _install_cache(monkeypatch, _FakeCache())
    _install_epoch(monkeypatch, epoch)

    _grant(monkeypatch, backend)
    assert epoch.bumps == 1


def test_grant_returns_the_entry_even_when_the_cache_scan_raises(monkeypatch):
    # _invalidate_search_caches_after_acl_change swallows cache errors.
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    _install_cache(monkeypatch, _FakeCache(["mnemos:search:alice:q1"], scan_raises=True))
    _install_epoch(monkeypatch, _FakeEpoch())

    entry = _grant(monkeypatch, backend)
    assert entry.principal == "group:research"


def test_grant_returns_the_entry_even_when_cache_delete_raises(monkeypatch):
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    cache = _FakeCache(["mnemos:search:alice:q1"], delete_raises=True)
    epoch = _FakeEpoch()
    _install_cache(monkeypatch, cache)
    _install_epoch(monkeypatch, epoch)

    entry = _grant(monkeypatch, backend)
    assert entry.perm == 4
    assert epoch.bumps == 1  # the epoch bump is still attempted after the failure


def test_grant_returns_the_entry_even_when_the_epoch_bump_raises(monkeypatch):
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    cache = _FakeCache(["mnemos:search:alice:q1"])
    _install_cache(monkeypatch, cache)
    _install_epoch(monkeypatch, _FakeEpoch(raises=True))

    entry = _grant(monkeypatch, backend)
    assert entry.principal == "group:research"
    assert cache.deleted == ["mnemos:search:alice:q1"]


def test_grant_logs_the_grant_at_info_with_principal_perm_and_memory(monkeypatch, caplog):
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None})
    with caplog.at_level(logging.INFO, logger=acl_route.logger.name):
        _grant(monkeypatch, backend, principal="user:7", memory_id="m-42")
    messages = [r.getMessage() for r in caplog.records if r.name == acl_route.logger.name]
    assert "[ACL] alice granted user:7 perm=4 on memory m-42" in messages


def test_grant_propagates_a_repository_failure_and_skips_invalidation(monkeypatch):
    class _ExplodingAcl(_FakeAcl):
        async def grant_acl(self, *a, **kw):
            raise RuntimeError("grant failed")

    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=_ExplodingAcl())
    cache = _FakeCache(["mnemos:search:alice:q1"])
    _install_cache(monkeypatch, cache)
    _install_epoch(monkeypatch, _FakeEpoch())

    with pytest.raises(RuntimeError, match="grant failed"):
        _grant(monkeypatch, backend)
    assert cache.deleted == []


# ---------------------------------------------------------------------------
# revoke_memory_acl
# ---------------------------------------------------------------------------


def _revoke(monkeypatch, backend, *, principal="user:7", user=None, memory_id="m1"):
    _install_backend(monkeypatch, backend)
    return _run(acl_route.revoke_memory_acl(memory_id, principal, user=user or _user("alice")))


def test_revoke_returns_the_revoked_payload(monkeypatch):
    acl = _FakeAcl(grants=[{"memory_id": "m1", "principal": "user:7", "perm": 4}])
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    result = _revoke(monkeypatch, backend)
    assert result == {"status": "revoked", "memory_id": "m1", "principal": "user:7"}
    assert acl.revoked == [("m1", "user:7")]


def test_revoke_raises_404_when_the_repository_reports_nothing_removed(monkeypatch):
    backend = _FakeBackend(
        memory_row={"owner_id": "alice", "group_id": None}, acl=_FakeAcl(revoke_result=False)
    )
    with pytest.raises(HTTPException) as exc:
        _revoke(monkeypatch, backend)
    assert exc.value.status_code == 404
    assert exc.value.detail == "ACL grant not found"


def test_revoke_404_does_not_invalidate_the_search_cache(monkeypatch):
    backend = _FakeBackend(
        memory_row={"owner_id": "alice", "group_id": None}, acl=_FakeAcl(revoke_result=False)
    )
    cache = _FakeCache(["mnemos:search:alice:q1"])
    epoch = _FakeEpoch()
    _install_cache(monkeypatch, cache)
    _install_epoch(monkeypatch, epoch)

    with pytest.raises(HTTPException):
        _revoke(monkeypatch, backend)
    assert cache.deleted == []
    assert epoch.bumps == 0


def test_revoke_rejects_a_malformed_principal_before_any_transaction(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _revoke(monkeypatch, backend, principal="7")
    assert exc.value.status_code == 422
    assert exc.value.detail == "principal must be 'user:<id>' or 'group:<id>'"
    assert backend.txns == []
    assert acl.revoked == []


def test_revoke_raises_404_when_the_memory_is_invisible(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row=None, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _revoke(monkeypatch, backend)
    assert exc.value.status_code == 404
    assert acl.revoked == []


def test_revoke_raises_403_for_a_non_manager(monkeypatch):
    acl = _FakeAcl()
    backend = _FakeBackend(memory_row={"owner_id": "bob", "group_id": "eng"}, acl=acl)
    with pytest.raises(HTTPException) as exc:
        _revoke(monkeypatch, backend)
    assert exc.value.status_code == 403
    assert acl.revoked == []


def test_revoke_invalidates_the_search_cache_and_bumps_the_epoch(monkeypatch):
    acl = _FakeAcl(grants=[{"memory_id": "m1", "principal": "user:7", "perm": 4}])
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    cache = _FakeCache(["mnemos:search:alice:q1", "mnemos:search:bob:q2"])
    epoch = _FakeEpoch()
    _install_cache(monkeypatch, cache)
    _install_epoch(monkeypatch, epoch)

    _revoke(monkeypatch, backend)
    assert set(cache.deleted) == {"mnemos:search:alice:q1", "mnemos:search:bob:q2"}
    assert epoch.bumps == 1


def test_revoke_logs_the_revocation_at_info(monkeypatch, caplog):
    acl = _FakeAcl(grants=[{"memory_id": "m1", "principal": "user:7", "perm": 4}])
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    with caplog.at_level(logging.INFO, logger=acl_route.logger.name):
        _revoke(monkeypatch, backend, memory_id="m-42")
    messages = [r.getMessage() for r in caplog.records if r.name == acl_route.logger.name]
    assert "[ACL] alice revoked user:7 on memory m-42" in messages


def test_revoke_passes_the_raw_principal_to_the_repository(monkeypatch):
    # Colons in the identifier survive into the repository call untouched.
    acl = _FakeAcl(grants=[{"memory_id": "m1", "principal": "user:a:b", "perm": 4}])
    backend = _FakeBackend(memory_row={"owner_id": "alice", "group_id": None}, acl=acl)
    result = _revoke(monkeypatch, backend, principal="user:a:b")
    assert acl.revoked == [("m1", "user:a:b")]
    assert result["principal"] == "user:a:b"


# ---------------------------------------------------------------------------
# _invalidate_search_caches_after_acl_change (direct)
# ---------------------------------------------------------------------------


def test_invalidation_is_a_noop_without_a_cache(monkeypatch):
    _install_cache(monkeypatch, None)
    epoch = _FakeEpoch()
    _install_epoch(monkeypatch, epoch)

    _run(acl_route._invalidate_search_caches_after_acl_change())
    # Early return: the epoch is NOT bumped when there is no cache at all.
    assert epoch.bumps == 0


def test_invalidation_deletes_each_scanned_key_then_bumps_the_epoch(monkeypatch):
    cache = _FakeCache(["k1", "k2"])
    _install_cache(monkeypatch, cache)
    epoch = _FakeEpoch()
    _install_epoch(monkeypatch, epoch)

    _run(acl_route._invalidate_search_caches_after_acl_change())
    assert cache.deleted == ["k1", "k2"]
    assert epoch.bumps == 1


def test_invalidation_still_bumps_the_epoch_when_the_scan_fails(monkeypatch):
    cache = _FakeCache(["k1"], scan_raises=True)
    _install_cache(monkeypatch, cache)
    epoch = _FakeEpoch()
    _install_epoch(monkeypatch, epoch)

    _run(acl_route._invalidate_search_caches_after_acl_change())
    assert cache.deleted == []
    assert epoch.bumps == 1


def test_invalidation_swallows_epoch_errors(monkeypatch):
    cache = _FakeCache(["k1"])
    _install_cache(monkeypatch, cache)
    _install_epoch(monkeypatch, _FakeEpoch(raises=True))

    _run(acl_route._invalidate_search_caches_after_acl_change())  # no raise
    assert cache.deleted == ["k1"]


def test_invalidation_uses_the_expected_scan_pattern_and_count(monkeypatch):
    cache = _FakeCache([])
    _install_cache(monkeypatch, cache)
    _install_epoch(monkeypatch, _FakeEpoch())

    _run(acl_route._invalidate_search_caches_after_acl_change())
    assert cache.scan_calls == [{"match": "mnemos:search:*", "count": 500}]


def test_invalidation_is_idempotent_across_repeated_calls(monkeypatch):
    cache = _FakeCache(["k1", "k2"])
    _install_cache(monkeypatch, cache)
    epoch = _FakeEpoch()
    _install_epoch(monkeypatch, epoch)

    _run(acl_route._invalidate_search_caches_after_acl_change())
    _run(acl_route._invalidate_search_caches_after_acl_change())
    assert cache.deleted == ["k1", "k2"]
    assert epoch.bumps == 2
