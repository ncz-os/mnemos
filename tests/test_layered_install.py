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
    # hive defaults OFF in the split: it is not an installable mnemos-core extra
    # (separate ncz-os/hive track), so a default-ON flag for a non-installable
    # component is incorrect. enable_hive is opt-in.
    assert layers.enable_hive is False
    assert layers.active_layers == {"core", "graeae"}


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


def test_unsupported_layers_are_disabled_rather_than_blocking_startup():
    """A backend missing a layer capability must not fail every start.

    The layer flags default to ON, and MySQL/MariaDB advertise no
    'consultations' capability, which GRAEAE requires. Hard-failing there means
    the documented MySQL deployment cannot start at all. strict_layers is the
    setting that asks for the hard failure; without it the unsupported layer is
    dropped loudly and the backend serves what it can.
    """
    from mnemos.persistence.base import backend_supported_layers

    bare = _Backend({"core"})
    supported = backend_supported_layers(bare)
    assert "graeae" not in supported, "precondition: this backend cannot serve graeae"

    active = {"core", "graeae"}
    unsupported = active - supported
    assert unsupported == {"graeae"}

    # strict_layers still refuses, so the fail-fast contract is intact.
    with pytest.raises(NotImplementedError, match="does not support enabled layer"):
        assert_backend_supports_layers(bare, active)


# ── Per-backend layer overrides (settings build-time) ────────────────────────


def test_mysql_backend_disables_graeae_by_default(monkeypatch):
    """MySQL has no consultations capability. The documented MySQL
    enterprise-image command sets only ``MNEMOS_PERSISTENCE_BACKEND=mysql``;
    GRAEAE must not be on by default or the lifecycle layer-check
    refuses to start (with strict_layers) or logs a degraded-boot
    warning (without). The settings builder applies a per-backend
    default that flips ``enable_graeae`` to False for MySQL / MariaDB
    so the image boots clean out of the box.
    """
    from mnemos.core import config as cfg

    monkeypatch.delenv("MNEMOS_ENABLE_GRAEAE", raising=False)
    monkeypatch.delenv("MNEMOS_STRICT_LAYERS", raising=False)
    monkeypatch.setenv("MNEMOS_PERSISTENCE_BACKEND", "mysql")
    settings = cfg._build_settings()
    assert settings.layers.enable_graeae is False
    assert "graeae" not in settings.layers.active_layers


def test_mariadb_backend_disables_graeae_by_default(monkeypatch):
    from mnemos.core import config as cfg

    monkeypatch.delenv("MNEMOS_ENABLE_GRAEAE", raising=False)
    monkeypatch.setenv("MNEMOS_PERSISTENCE_BACKEND", "mariadb")
    settings = cfg._build_settings()
    assert settings.layers.enable_graeae is False


def test_postgres_backend_keeps_graeae_default(monkeypatch):
    """Postgres advertises consultations, so the per-backend override
    must not flip GRAEAE off.
    """
    from mnemos.core import config as cfg

    monkeypatch.delenv("MNEMOS_ENABLE_GRAEAE", raising=False)
    monkeypatch.setenv("MNEMOS_PERSISTENCE_BACKEND", "postgres")
    settings = cfg._build_settings()
    assert settings.layers.enable_graeae is True


def test_operator_explicit_enable_graeae_on_mysql_is_respected(monkeypatch):
    """The per-backend override must NOT silently suppress operator
    intent: setting ``MNEMOS_ENABLE_GRAEAE=true`` on a MySQL deployment
    keeps the flag on (the operator has explicitly opted in, presumably
    to test the layer gap themselves or because they shipped a custom
    consultations implementation).
    """
    from mnemos.core import config as cfg

    monkeypatch.setenv("MNEMOS_PERSISTENCE_BACKEND", "mysql")
    monkeypatch.setenv("MNEMOS_ENABLE_GRAEAE", "true")
    settings = cfg._build_settings()
    assert settings.layers.enable_graeae is True
