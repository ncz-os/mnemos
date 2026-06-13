"""Backend-neutral visibility scope for repository read/write paths.

Persistence-layer counterpart to ``mnemos.core.visibility``. The core
module emits Postgres-flavored SQL fragments because it predates the
repository abstraction and is consumed by handlers building inline
queries. This module centralizes the *policy* — "what does this user
see / what can they mutate" — so each backend implementation can render
it into its own dialect inside the repository, instead of having the
SQL leak through the public repository signature.

GRAEAE consultation 2cfd0786 (2026-04-29) selected this shape: handlers
ask "memories visible to this user"; the repository owns the predicate.
Postgres implementations may rely on RLS as primary enforcement and
treat the predicate as defense-in-depth; SQLite implementations expand
it inline into the WHERE clause because SQLite has no RLS.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mnemos.core.auth_context import UserContext
from mnemos.core.security import is_root

from mnemos.core.secret_detection import VAULT_NAMESPACE

# Namespaces hidden from the DEFAULT read path (FTS + semantic). The
# secret vault is excluded so credential-class memories never surface
# in normal/public/phone search. Fleet agents opt in explicitly (see
# VisibilityFilter.for_read include_secrets / namespace=vault).
DEFAULT_EXCLUDED_NAMESPACES: tuple[str, ...] = (VAULT_NAMESPACE,)


class VisibilityScope(Enum):
    """Read/write visibility envelope for a repository call.

    ``ROOT_BYPASS``
        No filter. Cross-tenant audit reads. Only callers passing
        ``is_root(user) is True`` should resolve to this.

    ``READABLE``
        The full v1_multiuser read set: own rows + federation pulls +
        world-readable rows + group-readable rows where the caller is in
        the row's ``group_id``. Mirrors ``mnemos.core.visibility``.

    ``OWN_ONLY``
        Strict owner scoping. Mutation paths (PATCH, DELETE) use this
        so a non-owner cannot edit a row they merely have read access
        to via group/world bits.
    """

    ROOT_BYPASS = "root_bypass"
    READABLE = "readable"
    OWN_ONLY = "own_only"


@dataclass(frozen=True)
class VisibilityFilter:
    """Backend-neutral description of read/write visibility for a user.

    Repository methods accept this instead of raw SQL fragments. Each
    backend renders it into its dialect — Postgres uses ``$N`` params
    plus ``ANY($k::text[])``; SQLite expands group membership into an
    ``IN (?, ?, ...)`` list because SQLite has no array type.

    ``namespace`` carries the resolved namespace for the call. Non-root
    callers cannot cross namespaces; the handler enforces that with a
    403 before even building the filter. Repository methods trust the
    namespace they receive.
    """

    scope: VisibilityScope
    user_id: str | None
    group_ids: tuple[str, ...]
    namespace: str | None
    # Namespaces to subtract from the result set REGARDLESS of scope --
    # applied even for ROOT_BYPASS. This is how the secret vault stays
    # hidden from the default read path: root callers search with
    # namespace=None (no pin) but still must not see vault rows.
    exclude_namespaces: tuple[str, ...] = ()

    @classmethod
    def for_read(
        cls,
        user: UserContext,
        *,
        namespace: str | None,
        include_secrets: bool = False,
    ) -> "VisibilityFilter":
        """Build the read-path filter for a user.

        Root callers bypass the predicate entirely. Non-root callers get
        the full ``READABLE`` envelope pinned to ``namespace``.

        ``include_secrets`` / explicit vault targeting controls the
        secret-vault subtraction. Access control is enforced HERE in the
        factory (correct-by-construction), not merely by a route check:
        the vault escape hatches (``include_secrets`` and explicit
        ``namespace == VAULT_NAMESPACE`` targeting) are ROOT-ONLY.

        * Default (``include_secrets=False``) AND not pinned to the
          vault namespace -> the vault namespace is subtracted from the
          result set even for root. Credential-class memories never
          surface on the default FTS/semantic path.
        * ROOT caller with ``include_secrets=True`` OR
          ``namespace == VAULT_NAMESPACE`` -> no subtraction; fleet
          agents fetch credentials explicitly.
        * NON-ROOT caller -> the vault is ALWAYS subtracted, regardless
          of ``include_secrets`` or an explicit ``namespace="vault"``
          request. A non-root caller cannot enumerate or target the
          vault even by naming it. (Routes additionally reject these
          requests up front, but the factory does not depend on that.)

        Release-blocking hardening 2026-06-13: previously the vault
        exclusion was cleared whenever the caller targeted
        ``namespace="vault"`` for ANY user, letting a non-root caller
        enumerate the vault by explicitly requesting it. The escape
        hatches are now gated on ``is_root`` inside the factory.
        """
        caller_is_root = is_root(user)
        # Vault escape hatches are root-only: a non-root caller never
        # clears the vault subtraction, even when targeting the vault
        # namespace or passing include_secrets.
        unmask_vault = caller_is_root and (include_secrets or namespace == VAULT_NAMESPACE)
        exclude = () if unmask_vault else DEFAULT_EXCLUDED_NAMESPACES
        if caller_is_root:
            return cls(
                scope=VisibilityScope.ROOT_BYPASS,
                user_id=None,
                group_ids=(),
                namespace=namespace,
                exclude_namespaces=exclude,
            )
        if namespace is None:
            raise ValueError("non-root read visibility requires a namespace")
        return cls(
            scope=VisibilityScope.READABLE,
            user_id=user.user_id,
            group_ids=tuple(user.group_ids),
            namespace=namespace,
            exclude_namespaces=exclude,
        )

    @classmethod
    def for_mutation(cls, user: UserContext, *, namespace: str | None) -> "VisibilityFilter":
        """Build the mutation-path filter for a user.

        Root callers bypass; non-root callers are pinned to their own
        ``user_id`` (no group/world widening for writes).
        """
        if is_root(user):
            return cls(
                scope=VisibilityScope.ROOT_BYPASS,
                user_id=None,
                group_ids=(),
                namespace=namespace,
            )
        if namespace is None:
            raise ValueError("non-root mutation visibility requires a namespace")
        return cls(
            scope=VisibilityScope.OWN_ONLY,
            user_id=user.user_id,
            group_ids=(),
            namespace=namespace,
        )
