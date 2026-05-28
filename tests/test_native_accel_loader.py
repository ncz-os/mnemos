from __future__ import annotations

import importlib
import logging
import types


def test_load_hot_rs_prefers_legacy_module(monkeypatch):
    from mnemos.core.native_accel import load_hot_rs

    legacy = types.SimpleNamespace(__version__="legacy")
    native = types.SimpleNamespace(__version__="native")

    def fake_import(name: str):
        if name == "mnemos_hot":
            return legacy
        if name == "mnemos_native_search":
            return native
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    assert load_hot_rs(logging.getLogger("test"), "test component") is legacy


def test_load_hot_rs_uses_native_search_when_legacy_missing(monkeypatch):
    from mnemos.core.native_accel import load_hot_rs

    native = types.SimpleNamespace(__version__="native")

    def fake_import(name: str):
        if name == "mnemos_hot":
            raise ImportError("legacy missing")
        if name == "mnemos_native_search":
            return native
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    assert load_hot_rs(logging.getLogger("test"), "test component") is native


def test_load_hot_rs_returns_none_when_both_missing(monkeypatch, caplog):
    from mnemos.core.native_accel import load_hot_rs

    def fake_import(name: str):
        raise ImportError(f"{name} missing")

    monkeypatch.setattr(importlib, "import_module", fake_import)
    caplog.set_level(logging.WARNING)

    assert load_hot_rs(logging.getLogger("test"), "test component") is None
    assert "Falling back" in caplog.text
