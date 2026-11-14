"""FB1001: ``OracleBranchRepository`` contract, pinned statically.

The class implements the four ``BranchRepository`` abstract coroutines against oracledb.
These tests pin the base relationship, the concrete overrides, parameter parity, the inert
constructor, and the two documented empty short-circuits, without opening a connection.

DIVERGENCE (brief premise vs code): the class docstring claims it "supports upsert head,
stubs others", but all four abstract methods are implemented and the module contains no
``NotImplementedError`` outside the unused ``_stub_method`` factory.
"""

from __future__ import annotations

import inspect
import sys

import pytest

from mnemos.persistence.base import BranchRepository
from mnemos.persistence.oracle import OracleBranchRepository

METHODS = (
    "create_memory_branch",
    "delete_memory_branches_for_memories",
    "fetch_memory_branch_heads",
    "upsert_memory_branch_head",
)
STUB_MARKER = "NotImplementedError"


def _params(func: object) -> list[tuple[str, object, object]]:
    return [
        (param.name, param.kind, param.default)
        for param in inspect.signature(func).parameters.values()
    ]


def test_subclasses_the_documented_base() -> None:
    assert issubclass(OracleBranchRepository, BranchRepository)
    assert OracleBranchRepository.__bases__ == (BranchRepository,)


def test_base_declares_exactly_the_documented_abstract_methods() -> None:
    assert BranchRepository.__abstractmethods__ == frozenset(METHODS)
    for name in METHODS:
        assert getattr(BranchRepository, name).__isabstractmethod__ is True


def test_repository_is_concrete_and_constructs_without_arguments() -> None:
    assert inspect.isabstract(OracleBranchRepository) is False
    assert OracleBranchRepository.__abstractmethods__ == frozenset()

    repo = OracleBranchRepository()
    assert type(repo) is OracleBranchRepository


def test_construction_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "oracledb", raising=False)

    repo = OracleBranchRepository()

    assert "oracledb" not in sys.modules
    assert vars(repo) == {}


def test_documented_methods_are_defined_concrete_and_async() -> None:
    for name in METHODS:
        assert name in OracleBranchRepository.__dict__, name
        method = OracleBranchRepository.__dict__[name]
        assert inspect.iscoroutinefunction(method), name
        assert getattr(method, "__isabstractmethod__", False) is False, name


def test_no_method_is_a_generated_stub() -> None:
    module_source = inspect.getsource(sys.modules[OracleBranchRepository.__module__])
    assert module_source.count("_stub_method(") == 1  # definition only, zero call sites

    for name in METHODS:
        source = inspect.getsource(OracleBranchRepository.__dict__[name])
        assert STUB_MARKER not in source, name


def test_parameter_contracts_match_the_base_declaration() -> None:
    for name in METHODS:
        assert _params(OracleBranchRepository.__dict__[name]) == _params(
            getattr(BranchRepository, name)
        ), name


def test_unknown_attribute_raises_instead_of_returning_empty() -> None:
    repo = OracleBranchRepository()

    with pytest.raises(AttributeError):
        repo.merge_branches
    with pytest.raises(AttributeError):
        getattr(repo, "fetch_memory_branch_unknown")


def test_no_attribute_fallback_is_defined() -> None:
    assert "__getattr__" not in OracleBranchRepository.__dict__
    assert "__getattribute__" not in OracleBranchRepository.__dict__


def test_instances_share_no_state() -> None:
    first = OracleBranchRepository()
    second = OracleBranchRepository()

    assert first is not second
    assert vars(first) == vars(second) == {}


@pytest.mark.asyncio
async def test_empty_ids_short_circuit_without_a_transaction() -> None:
    repo = OracleBranchRepository()

    assert await repo.fetch_memory_branch_heads(None, []) == []
    assert await repo.delete_memory_branches_for_memories(None, []) is None


def test_docstring_claims_stubs_that_do_not_exist() -> None:
    """DIVERGENCE: "stubs others" is not true — no method raises NotImplementedError."""
    doc = inspect.getdoc(OracleBranchRepository) or ""

    assert "stubs others" in doc
    for name in METHODS:
        source = inspect.getsource(OracleBranchRepository.__dict__[name])
        assert STUB_MARKER not in source, name
