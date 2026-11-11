"""Characterisation of ``mnemos/tools/refresh_pantheon_catalog.py`` (FB1128).

The module is a compatibility entrypoint: ``main()`` imports the PANTHEON
add-on's pricing CLI inside the function and turns an import failure into a
repair message plus exit 2. That add-on (``mnemos.domain.pantheon``) is not
part of this repository, so the import-failure branch is the only one a source
checkout can reach; the success branch is pinned by injecting a stand-in
module. Behaviour that looks wrong is pinned and named as a divergence.
"""

from __future__ import annotations

import builtins
import inspect
import sys
from types import ModuleType

import pytest

from mnemos.tools import refresh_pantheon_catalog as tool

_ADDON_PACKAGE = "mnemos.domain.pantheon"
_ADDON_MODULE = "mnemos.domain.pantheon.pricing"


@pytest.fixture
def no_addon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the add-on genuinely unimportable for the test.

    Drops any cached module objects and blocks the import machinery: if the
    add-on is genuinely installed in the environment, the block still forces
    the ``ImportError`` branch instead of letting a cached or path-resolvable
    module slip through.
    """
    monkeypatch.delitem(sys.modules, _ADDON_MODULE, raising=False)
    monkeypatch.delitem(sys.modules, _ADDON_PACKAGE, raising=False)

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        # The wrapper imports exactly these two names; match them exactly so
        # an unrelated sibling import is never intercepted.
        if name in (_ADDON_PACKAGE, _ADDON_MODULE):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)


def _install_addon(monkeypatch: pytest.MonkeyPatch, func, calls: list | None = None) -> None:
    package = ModuleType(_ADDON_PACKAGE)
    package.__path__ = []  # type: ignore[attr-defined]
    pricing = ModuleType(_ADDON_MODULE)

    def _main():
        if calls is not None:
            calls.append(True)
        return func()

    pricing.main = _main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _ADDON_PACKAGE, package)
    monkeypatch.setitem(sys.modules, _ADDON_MODULE, pricing)


# ── Shape ──────────────────────────────────────────────────────────────────


def test_main_takes_no_arguments_and_returns_an_exit_code() -> None:
    assert inspect.signature(tool.main).parameters == {}


def test_the_module_is_import_only_it_has_no_cli_arguments() -> None:
    # No argparse, no sys.argv: the wrapper forwards nothing to the add-on.
    source = inspect.getsource(tool)

    assert "argv" not in source
    assert "argparse" not in source


# ── The missing add-on (the branch a source checkout always takes) ─────────


def test_a_source_checkout_cannot_import_the_add_on(no_addon: None) -> None:
    with pytest.raises(ImportError):
        __import__(_ADDON_MODULE)


def test_missing_add_on_reports_the_package_and_repair_hint(no_addon: None, capsys) -> None:
    assert tool.main() == 2

    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err.startswith(
        "refresh-pantheon-catalog requires the mnemos-pantheon add-on package and its dependencies."
    )
    # The package is the first import tried, so its name is the missing one
    # reported here; when it resolves but a dependency is broken the pricing
    # module (or the failing dependency) is named instead.
    assert "could not be imported (mnemos.domain.pantheon" in printed.err
    assert "pip install mnemos-pantheon and its dependencies." in printed.err


def test_the_missing_add_on_exits_two_not_one(no_addon: None, capsys) -> None:
    # 2 is also argparse's usage-failure code; here it means "add-on absent".
    assert tool.main() == 2
    capsys.readouterr()


def test_an_import_error_without_a_name_is_labelled_unknown(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        # Exact match only: a sibling like 'mnemos.domain.pantheon_extra' is
        # not the pricing module and must not be intercepted.
        if name in (_ADDON_PACKAGE, _ADDON_MODULE):
            raise ImportError("kaboom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    assert tool.main() == 2
    # An ImportError raised inside the add-on's own body carries no .name.
    assert "(<unknown>: kaboom)" in capsys.readouterr().err


# ── The add-on present ─────────────────────────────────────────────────────


def test_a_successful_run_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _install_addon(monkeypatch, lambda: 0, calls)

    assert tool.main() == 0
    assert calls == [True]  # the add-on entrypoint ran exactly once


def test_a_none_return_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_addon(monkeypatch, lambda: None)

    assert tool.main() == 0


@pytest.mark.parametrize("code", [0, 1, 3, 42])
def test_an_integer_return_passes_through(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    _install_addon(monkeypatch, lambda: code)

    assert tool.main() == code


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5", 5), (2.9, 2), (True, 1), (-1, -1)],
)
def test_truthy_junk_is_int_coerced(monkeypatch: pytest.MonkeyPatch, value, expected: int) -> None:
    # ``int(pantheon_main() or 0)``: the wrapper trusts the add-on's return
    # value completely. Pinned as observed rather than as intended. Each case
    # gets a fresh monkeypatch so no state leaks between values.
    _install_addon(monkeypatch, lambda v=value: v)

    assert tool.main() == expected


@pytest.mark.parametrize("value", [0, "", [], None])
def test_a_falsy_return_is_zero_via_or(monkeypatch: pytest.MonkeyPatch, value) -> None:
    # The falsy cases take the ``or 0`` short-circuit, not the ``int(...)``
    # coercion path pinned above: they are documented here separately so the
    # two behaviours stay named correctly.
    _install_addon(monkeypatch, lambda v=value: v)

    assert tool.main() == 0


def test_only_import_failures_are_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise RuntimeError("pricing vendor exploded")

    _install_addon(monkeypatch, _boom)

    with pytest.raises(RuntimeError, match="pricing vendor exploded"):
        tool.main()


def test_a_failing_dependency_is_reported_as_a_missing_add_on(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # Any ImportError from inside the add-on — a broken httpx pin, a missing
    # transitive package — is reported with the "install the add-on" hint,
    # even though the add-on itself is installed. Pinned as observed.
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        # The wrapper imports exactly these two names; match them exactly so
        # an unrelated sibling import is never intercepted.
        if name in (_ADDON_PACKAGE, _ADDON_MODULE):
            raise ModuleNotFoundError("No module named 'httpx_socks'", name="httpx_socks")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    assert tool.main() == 2
    message = capsys.readouterr().err
    assert "(httpx_socks: No module named 'httpx_socks')" in message
    assert "pip install mnemos-pantheon" in message
