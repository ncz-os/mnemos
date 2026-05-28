"""Optional native accelerator loading helpers."""

from __future__ import annotations

import importlib
from logging import Logger
from types import ModuleType


def load_hot_rs(logger: Logger, component: str) -> ModuleType | None:
    """Load the opt-in Rust accelerator under either supported module name.

    ``mnemos_hot`` is the legacy accelerator name used by several hot
    paths. ``mnemos_native_search`` is the current PyO3 extension in this
    tree. Treat both as optional implementations of the same small cosine
    API so deployments with the newer wheel still get acceleration.
    """
    errors: list[str] = []
    for module_name in ("mnemos_hot", "mnemos_native_search"):
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            errors.append(f"{module_name}: {exc}")
            continue
        logger.info(
            "%s Rust accelerator enabled (%s %s)",
            component,
            module_name,
            getattr(module, "__version__", "?"),
        )
        return module
    logger.warning(
        "MNEMOS_HOT_RS_ENABLED=1 but no Rust accelerator is importable (%s). "
        "Falling back to Python implementation.",
        "; ".join(errors),
    )
    return None
