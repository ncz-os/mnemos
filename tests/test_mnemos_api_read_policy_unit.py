from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from mnemos.api.read_policy import UserContext, VAULT_NAMESPACE, VisibilityFilter, VisibilityScope, is_root


@pytest.fixture
def root():
    return UserContext("root", ["admins"], "root", "home", True)


@pytest.fixture
def member():
    return UserContext("alice", ["team", "staff"], "member", "home", True)


@pytest.mark.parametrize("role, expected", [("root", True), ("admin", False), ("Root", False), ("", False)])
def test_is_root_requires_exact_role(role, expected):
    user = UserContext("alice", [], role, "home", True)
    assert is_root(user) is expected


@pytest.mark.parametrize(
    "namespace, options, excluded",
    [
        (None, {}, True),
        ("home", {}, True),
        (VAULT_NAMESPACE, {}, False),
        (None, {"include_secrets": True}, False),
        (None, {"include_vault": True}, False),
    ],
)
def test_root_read_visibility(root, namespace, options, excluded):
    result = VisibilityFilter.for_read(root, namespace=namespace, **options)
    assert result == VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=namespace,
        exclude_namespaces=(VAULT_NAMESPACE,) if excluded else (),
    )


@pytest.mark.parametrize(
    "namespace, options",
    [
        ("home", {}),
        (VAULT_NAMESPACE, {}),
        ("home", {"include_secrets": True}),
        ("home", {"include_vault": True}),
        (VAULT_NAMESPACE, {"include_secrets": True, "include_vault": True}),
    ],
)
def test_member_read_visibility_always_excludes_vault(member, namespace, options):
    result = VisibilityFilter.for_read(member, namespace=namespace, **options)
    assert result == VisibilityFilter(
        scope=VisibilityScope.READABLE,
        user_id="alice",
        group_ids=("team", "staff"),
        namespace=namespace,
        exclude_namespaces=(VAULT_NAMESPACE,),
    )
    member.group_ids.append("later")
    assert result.group_ids == ("team", "staff")


@pytest.mark.parametrize("factory", [VisibilityFilter.for_read, VisibilityFilter.for_mutation])
def test_member_visibility_requires_namespace(member, factory):
    with pytest.raises(ValueError, match="non-root .* visibility requires a namespace"):
        factory(member, namespace=None)


def test_mutation_visibility_is_owner_only_for_member(member):
    assert VisibilityFilter.for_mutation(member, namespace="home") == VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY, user_id="alice", group_ids=(), namespace="home"
    )


@pytest.mark.parametrize("namespace", [None, "home", VAULT_NAMESPACE])
def test_mutation_visibility_root_bypasses_ownership(root, namespace):
    assert VisibilityFilter.for_mutation(root, namespace=namespace) == VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=namespace
    )


@pytest.mark.parametrize("include_vault", [False, True])
def test_system_visibility_matches_root_read(root, include_vault):
    assert VisibilityFilter.for_system(include_vault=include_vault) == VisibilityFilter.for_read(
        root, namespace=None, include_vault=include_vault
    )


def test_caller_visibility_resolves_namespace(root, member):
    assert VisibilityFilter.for_caller(root) == VisibilityFilter.for_read(root, namespace=None)
    assert VisibilityFilter.for_caller(member) == VisibilityFilter.for_read(member, namespace="home")
    assert VisibilityFilter.for_caller(member, namespace="other") == VisibilityFilter.for_read(
        member, namespace="other"
    )
    assert VisibilityFilter.for_caller(SimpleNamespace(), namespace="ignored") == VisibilityFilter.for_system()


def test_caller_visibility_defaults_missing_namespace():
    user = SimpleNamespace(user_id="alice", group_ids=[], role="member")
    assert VisibilityFilter.for_caller(user) == VisibilityFilter.for_read(user, namespace="default")


def test_visibility_filter_is_immutable(member):
    result = VisibilityFilter.for_read(member, namespace="home")
    with pytest.raises(FrozenInstanceError):
        result.namespace = "other"
