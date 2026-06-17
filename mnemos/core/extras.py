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
    "charon": "mnemos-charon",
}

EXTERNAL_EXTRA_IMPORT_PROBES: dict[str, str] = {
    "pantheon": "mnemos.domain.pantheon",
    "knemon": "mnemos.domain.knemon.router",
    "graeae": "mnemos.domain.graeae.engine",
    "charon": "mnemos.domain.portability.schemas",
}

UNAVAILABLE_EXTRAS: dict[str, str] = {
    "hive": "HIVE is on the separate ncz-os/hive build-fabric track; not installable as a mnemos-core extra in this split.",
}

FEATURE_BUNDLES: dict[str, tuple[str, ...]] = {
    "edge": ("edge",),
    "server": ("nats", "persephone", "pantheon", "knemon", "graeae", "charon"),
    "ml": ("morpheus", "kronos", "apollo", "artemis", "hot"),
    "interop": ("knossos",),
    "full": (
        "morpheus",
        "persephone",
        "pantheon",
        "knemon",
        "graeae",
        "charon",
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

    External add-ons are checked metadata-first because their install contract is
    the distribution; editable or partial installs may lack metadata, so fall
    back to importing a stable add-on module before reporting the extra missing.
    """
    dist_name = EXTERNAL_EXTRA_DISTS.get(name)
    if dist_name is not None:
        try:
            version(dist_name)
        except PackageNotFoundError:
            probe = EXTERNAL_EXTRA_IMPORT_PROBES[name]
            try:
                import_module(probe)
            except ImportError:
                return False
        return True

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
