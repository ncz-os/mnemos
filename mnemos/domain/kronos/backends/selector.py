"""Runtime compute backend selector for KRONOS."""

from __future__ import annotations

import importlib
from types import ModuleType

from mnemos.core.config import get_settings


_BACKEND_ENV = "MNEMOS_KRONOS_BACKEND"


def get_backend() -> ModuleType:
    """Return the active KRONOS compute backend module."""
    requested = get_settings().kronos.backend.strip().lower()
    if requested == "cpu":
        return importlib.import_module("mnemos.domain.kronos.backends.cpu")
    if requested == "gpu":
        return importlib.import_module("mnemos.domain.kronos.backends.gpu")
    if requested == "auto":
        try:
            importlib.import_module("cupy")
        except ImportError:
            return importlib.import_module("mnemos.domain.kronos.backends.cpu")
        return importlib.import_module("mnemos.domain.kronos.backends.gpu")
    raise ValueError(f"{_BACKEND_ENV} must be one of: auto, cpu, gpu")
