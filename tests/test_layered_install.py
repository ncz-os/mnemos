"""Layered-install behaviour (GRAEAE de8f4b2b layering, 2026-06-01).

Covers the two orthogonal axes — feature layers (core/graeae/hive) and storage
backend support — plus the dependency-direction guard and KNEMON model-affinity
emission. See docs/LAYERED_INSTALL.md.
"""

from __future__ import annotations

import pytest

from mnemos.persistence.base import (
    LAYER_REQUIRED_CAPABILITIES,
    assert_backend_supports_layers,
    backend_supported_layers,
)


class _Backend:
    def __init__(self, caps: set[str]) -> None:
        self.capabilities = caps


# ── Feature-layer flags + dependency direction ───────────────────────────────


def test_layer_flags_default_on_and_active_layers() -> None:
    from mnemos.core.config import _LayerSettings

    layers = _LayerSettings()
    assert layers.enable_graeae is True
    assert layers.enable_hive is True
    assert layers.active_layers == {"core", "graeae", "hive"}


def test_active_layers_core_only_when_disabled() -> None:
    from mnemos.core.config import _LayerSettings

    layers = _LayerSettings(enable_graeae=False, enable_hive=False)
    assert layers.active_layers == {"core"}


def test_hive_requires_graeae(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from mnemos.core import config as cfg

    monkeypatch.setenv("MNEMOS_ENABLE_GRAEAE", "0")
    monkeypatch.setenv("MNEMOS_ENABLE_HIVE", "1")
    with pytest.raises(ValidationError, match="requires MNEMOS_ENABLE_GRAEAE"):
        cfg._build_settings()


def test_graeae_alone_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.core import config as cfg

    monkeypatch.setenv("MNEMOS_ENABLE_GRAEAE", "1")
    monkeypatch.setenv("MNEMOS_ENABLE_HIVE", "0")
    settings = cfg._build_settings()
    assert settings.layers.active_layers == {"core", "graeae"}


# ── Honest backend × layer gating ────────────────────────────────────────────


def test_backend_supported_layers_full_vs_bare() -> None:
    full = _Backend({"core", "consultations", "federation", "audit"})
    bare = _Backend({"core"})
    assert backend_supported_layers(full) == {"core", "graeae", "hive"}
    # bare lacks 'consultations' -> graeae unsupported; hive needs no extra cap.
    assert "graeae" not in backend_supported_layers(bare)
    assert "core" in backend_supported_layers(bare)


def test_fail_fast_when_layer_unsupported() -> None:
    bare = _Backend({"core"})
    with pytest.raises(NotImplementedError, match="does not support enabled layer"):
        assert_backend_supports_layers(bare, {"core", "graeae"})


def test_fail_fast_passes_when_supported() -> None:
    full = _Backend({"core", "consultations"})
    # Should not raise.
    assert_backend_supports_layers(full, {"core", "graeae", "hive"})


def test_layer_matrix_graeae_requires_consultations() -> None:
    assert LAYER_REQUIRED_CAPABILITIES["core"] == set()
    assert LAYER_REQUIRED_CAPABILITIES["graeae"] == {"consultations"}
    assert LAYER_REQUIRED_CAPABILITIES["hive"] == set()

