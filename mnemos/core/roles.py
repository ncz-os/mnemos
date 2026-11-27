"""Shared user role vocabulary and transport policy."""

KNOWN_ROLES = frozenset({"user", "root", "federation", "monitor", "operator"})
MCP_ALLOWED_ROLES = KNOWN_ROLES - {"monitor"}
OPERATOR_ROLES = frozenset({"root", "operator"})
