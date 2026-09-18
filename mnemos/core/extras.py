"""Optional subsystem availability probes.

Python packaging does not expose "which extra was selected" at runtime.
MNEMOS therefore treats in-core extras as available when the modules they
need can be imported. Carved domain extras are separate distributions, so
their install contract is the distribution metadata rather than a deep
module path that may drift independently.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

EXTRA_PROBES: dict[str, tuple[str, ...]] = {
    "morpheus": ("numpy",),
    "persephone": ("zstandard",),
    "kronos": ("numpy",),
    "knossos": (),
    "apollo": (),
    "artemis": (),
    "nats": ("nats",),
    "hot": ("mnemos_hot",),
}

EXTERNAL_EXTRA_DISTS: dict[str, str] = {
    "pantheon": "mnemos-pantheon",
    "knemon": "mnemos-knemon",
    "graeae": "mnemos-graeae",
}

EXTERNAL_EXTRA_IMPORT_PROBES: dict[str, str] = {
    "pantheon": "mnemos.domain.pantheon",
    "knemon": "mnemos.domain.knemon.router",
    "graeae": "mnemos.domain.graeae.engine",
}

UNAVAILABLE_EXTRAS: dict[str, str] = {
    "hive": "HIVE (GRAEAE Hive Mind) is carved into the standalone STIPHOS distribution (mnemos-stiphos) and the separate ncz-os/hive build-fabric track; it deploys as its own service and is intentionally not part of the mnemos-core umbrella.",
}

FEATURE_BUNDLES: dict[str, tuple[str, ...]] = {
    "edge": ("edge",),
    "server": ("nats", "persephone", "pantheon", "knemon", "graeae"),
    "ml": ("morpheus", "kronos", "apollo", "artemis", "hot"),
    "interop": ("knossos",),
    "full": (
        "morpheus",
        "persephone",
        "pantheon",
        "knemon",
        "graeae",
        "kronos",
        "knossos",
        "apollo",
        "artemis",
        "nats",
        "hot",
        "edge",
    ),
}


def is_extra_installed(name: str) -> bool:
    """Check if optional extra ``name`` is available by probing deps.

    External add-ons are checked by BOTH the distribution metadata AND a
    stable add-on module import — metadata alone is insufficient because
    ``[tool.uv.sources]`` may resolve a name-only stub (see the
    ``mnemos-stubs/<name>`` entries in pyproject.toml) that satisfies the
    resolver without providing any actual add-on code. Without the import
    probe, PANTHEON/KNEMON/GRAEAE routes could mount at 503-or-broken instead of
    cleanly returning 503 with the install hint. In production with the
    real wheels (which DO contain ``mnemos.domain.pantheon``, ``mnemos.domain.knemon.router``,
    ``mnemos.domain.graeae.engine``, ``mnemos_hot``) both probes succeed
    and the extra is reported as installed.

    The fallback order matters: editable or partial installs may lack
    metadata for a real wheel, so if ``version()`` fails we still try
    the import probe before reporting the extra missing (preserves the
    pre-F01 contract). Conversely, when metadata IS present we now ALSO
    require the import probe to succeed — this catches the stub-resolves-
    to-metadata-only case introduced by the F01 ``[tool.uv.sources]``
    fallbacks.
    """
    dist_name = EXTERNAL_EXTRA_DISTS.get(name)
    if dist_name is not None:
        try:
            version(dist_name)
            metadata_ok = True
        except PackageNotFoundError:
            metadata_ok = False
        probe = EXTERNAL_EXTRA_IMPORT_PROBES.get(name)
        # When metadata IS present, BOTH probes must succeed — this rejects
        # name-only stubs from ``[tool.uv.sources]`` so add-on routes
        # return 503 instead of mounting with no real code behind them.
        # When metadata is MISSING (editable / partial installs without
        # metadata), the import probe is sufficient — that's the pre-F01
        # contract preserved here.
        if metadata_ok:
            if probe is None:
                return True
            try:
                import_module(probe)
            except ImportError:
                return False
            return True
        # metadata missing — fall back to import probe (pre-F01 behaviour)
        if probe is not None:
            try:
                import_module(probe)
            except ImportError:
                return False
            return True
        return False

    probes = EXTRA_PROBES.get(name)
    if probes is None:
        return False
    for module in probes:
        try:
            __import__(module)
        except ImportError:
            return False
    return True


def install_hint(name: str) -> str:
    if name in UNAVAILABLE_EXTRAS:
        return UNAVAILABLE_EXTRAS[name]
    return f"pip install mnemos-core[{name}]  (or [server]/[ml]/[full] bundle)"


def missing_extra_detail(name: str, *, label: str | None = None) -> dict[str, str]:
    display = (label or name).upper()
    return {
        "error": f"{display} not installed",
        "install": install_hint(name),
    }


def require_extra(name: str) -> None:
    """Raise RuntimeError with install instruction if extra is missing."""
    if not is_extra_installed(name):
        raise RuntimeError(f"{name} subsystem not installed. Install via: {install_hint(name)}")


def bundle_status(members: Iterable[str]) -> tuple[list[str], list[str]]:
    """Return ``(have, missing)`` for a bundle member list."""
    have: list[str] = []
    missing: list[str] = []
    for member in members:
        target = "edge" if member == "edge" else member
        if target == "edge":
            try:
                __import__("aiosqlite")
                __import__("sqlite_vec")
            except ImportError:
                missing.append(member)
            else:
                have.append(member)
            continue
        if is_extra_installed(target):
            have.append(member)
        else:
            missing.append(member)
    return have, missing
