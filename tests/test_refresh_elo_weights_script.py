from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(("flag", "expected_force"), [(None, False), ("--force", True)])
def test_refresh_elo_script_uses_current_public_refresh_api(monkeypatch, tmp_path, flag, expected_force):
    calls: list[bool] = []
    elo_sync = types.ModuleType("mnemos.domain.graeae.elo_sync")

    def get_elo_weights(*, force_refresh=False):
        calls.append(force_refresh)
        return {"openai": 1.0}

    elo_sync.get_elo_weights = get_elo_weights
    graeae = types.ModuleType("mnemos.domain.graeae")
    graeae.__path__ = []
    monkeypatch.setitem(sys.modules, "mnemos.domain.graeae", graeae)
    monkeypatch.setitem(sys.modules, "mnemos.domain.graeae.elo_sync", elo_sync)

    from mnemos.core import config

    registry = tmp_path / "elo.json"
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(graeae=SimpleNamespace(elo_registry=registry)),
    )
    monkeypatch.setattr(sys, "argv", ["refresh_elo_weights.py"] + ([flag] if flag else []))

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "refresh_elo_weights.py"
    spec = importlib.util.spec_from_file_location("refresh_elo_weights_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.main() == 0
    assert calls == [expected_force]

