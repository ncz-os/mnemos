"""Machine-readable rejection error for replica (read-only standby) mode.

Every mutating REST endpoint and MCP tool on a replica returns HTTP 503
with this body::

    {"error": "replica_read_only", "primary_hint": "<MNEMOS_PRIMARY_URL or null>"}

The status is 503 (service unavailable for writes), not 405: MNEMOS does not
use 405 for read-only surfaces today, and the mutation is genuinely
unavailable on this server — the primary (named by ``primary_hint`` when
``MNEMOS_PRIMARY_URL`` is set) is the only place it can succeed.
"""

from __future__ import annotations

from fastapi import HTTPException

from mnemos.core.replica_mode import is_replica, primary_hint


class ReplicaReadOnlyError(HTTPException):
    """Raised inside routes to reject a mutation on a read-only standby."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail={
                "error": "replica_read_only",
                "primary_hint": primary_hint(),
            },
        )


def replica_read_only() -> ReplicaReadOnlyError:
    """Build the standard replica-rejection exception."""
    return ReplicaReadOnlyError()


def mcp_replica_read_only() -> dict:
    """Machine-readable failure dict for MCP tools.

    Returns ``{"success": False, "error": "replica_read_only",
    "primary_hint": ...}`` — the MCP vocabulary for a handler-level
    failure (the audit log records it as outcome='failure').
    """
    return {
        "success": False,
        "error": "replica_read_only",
        "primary_hint": primary_hint(),
    }


def check_replica_write_allowed() -> None:
    """Route-guard entry point: raise :class:`ReplicaReadOnlyError` when the
    process is serving in replica mode. Call it at the top of every
    mutating route handler.
    """
    if is_replica():
        raise replica_read_only()
