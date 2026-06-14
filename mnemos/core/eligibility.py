"""Canonical memory eligibility predicates for processing and visibility."""

from __future__ import annotations

from mnemos.core.secret_detection import VAULT_NAMESPACE

MEMORY_ELIGIBILITY_PREDICATE = "deleted_at IS NULL AND archived_at IS NULL AND consolidated_into IS NULL"


def qualify_memory_predicate(predicate: str, alias: str = "m") -> str:
    """Qualify an unaliased memory predicate with the SQL table alias."""
    prefix = f"{alias}." if alias else ""
    qualified = predicate
    for column in ("deleted_at", "archived_at", "consolidated_into"):
        qualified = qualified.replace(column, f"{prefix}{column}")
    return qualified


def eligible_memory_predicate(alias: str = "m") -> str:
    return qualify_memory_predicate(MEMORY_ELIGIBILITY_PREDICATE, alias)


def eligible_for_morpheus(alias: str = "m") -> str:
    return eligible_memory_predicate(alias)


def eligible_for_compression(alias: str = "m", *, reject_private_parent: bool = False) -> str:
    predicate = eligible_memory_predicate(alias)
    prefix = f"{alias}." if alias else ""
    vault_literal = VAULT_NAMESPACE.replace("'", "''")
    predicate = f"{predicate} AND ({prefix}namespace IS NULL OR {prefix}namespace <> '{vault_literal}')"
    if reject_private_parent:
        predicate = f"{predicate} AND {prefix}permission_mode <> 400"
    return predicate


def eligible_for_federation(alias: str = "m") -> str:
    prefix = f"{alias}." if alias else ""
    vault_literal = VAULT_NAMESPACE.replace("'", "''")
    return (
        f"{prefix}federation_source IS NULL "
        f"AND ({prefix}permission_mode % 10) >= 4 "
        f"AND {eligible_memory_predicate(alias)} "
        f"AND ({prefix}namespace IS NULL OR {prefix}namespace <> '{vault_literal}')"
    )


def eligible_for_federation_tombstone(alias: str = "m") -> str:
    prefix = f"{alias}." if alias else ""
    vault_literal = VAULT_NAMESPACE.replace("'", "''")
    return (
        f"{prefix}federation_source IS NULL "
        f"AND ({prefix}permission_mode % 10) >= 4 "
        f"AND {prefix}deleted_at IS NULL "
        f"AND {prefix}archived_at IS NULL "
        f"AND {prefix}consolidated_into IS NOT NULL "
        f"AND ({prefix}namespace IS NULL OR {prefix}namespace <> '{vault_literal}')"
    )


__all__ = [
    "MEMORY_ELIGIBILITY_PREDICATE",
    "eligible_for_compression",
    "eligible_for_federation",
    "eligible_for_federation_tombstone",
    "eligible_for_morpheus",
    "eligible_memory_predicate",
    "qualify_memory_predicate",
]
