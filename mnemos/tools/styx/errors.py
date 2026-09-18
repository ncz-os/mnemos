"""STYX failure taxonomy.

Every one of these is fatal to a run by design. STYX has exactly one
success condition and no partial one: a backup that "mostly worked" is the
failure mode this tool was built to eliminate.
"""

from __future__ import annotations


class StyxError(Exception):
    """Base class for every STYX failure."""


class StyxConfigError(StyxError):
    """Configuration is missing or unusable.

    Raised before any work starts. STYX refuses to run half-configured
    rather than producing an artifact nobody can decrypt or find.
    """


class StyxIntegrityError(StyxError):
    """An integrity gate rejected the run.

    This is the class that would have caught the five-week silent NFS
    failure: an empty export, a zero-byte artifact, a file on Drive whose
    bytes do not match what was sent.
    """


class StyxUploadError(StyxError):
    """The Drive transfer itself failed."""
