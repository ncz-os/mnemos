from __future__ import annotations

from mnemos.core.eligibility import (
    MEMORY_ELIGIBILITY_PREDICATE,
    eligible_for_compression,
    eligible_for_federation,
    eligible_for_federation_tombstone,
    eligible_for_morpheus,
    qualify_memory_predicate,
)


ROWS = [
    {
        "id": "active",
        "deleted_at": None,
        "archived_at": None,
        "consolidated_into": None,
        "permission_mode": 644,
        "namespace": "default",
    },
    {
        "id": "deleted",
        "deleted_at": "ts",
        "archived_at": None,
        "consolidated_into": None,
        "permission_mode": 644,
        "namespace": "default",
    },
    {
        "id": "archived",
        "deleted_at": None,
        "archived_at": "ts",
        "consolidated_into": None,
        "permission_mode": 644,
        "namespace": "default",
    },
    {
        "id": "consolidated",
        "deleted_at": None,
        "archived_at": None,
        "consolidated_into": "active",
        "permission_mode": 644,
        "namespace": "default",
    },
    {
        "id": "private_parent",
        "deleted_at": None,
        "archived_at": None,
        "consolidated_into": None,
        "permission_mode": 400,
        "namespace": "default",
    },
    {
        "id": "vault",
        "deleted_at": None,
        "archived_at": None,
        "consolidated_into": None,
        "permission_mode": 644,
        "namespace": "vault",
    },
]


def _canonical_ids(rows=ROWS) -> list[str]:
    return [
        row["id"]
        for row in rows
        if row["deleted_at"] is None and row["archived_at"] is None and row["consolidated_into"] is None
    ]


def test_canonical_memory_eligibility_predicate_filters_universal_exclusions():
    assert MEMORY_ELIGIBILITY_PREDICATE == ("deleted_at IS NULL AND archived_at IS NULL AND consolidated_into IS NULL")
    assert _canonical_ids() == ["active", "private_parent", "vault"]
    assert eligible_for_morpheus("m") == (
        "m.deleted_at IS NULL AND m.archived_at IS NULL AND m.consolidated_into IS NULL"
    )


def test_qualify_memory_predicate_applies_alias_to_all_canonical_columns():
    assert qualify_memory_predicate(MEMORY_ELIGIBILITY_PREDICATE, "row") == (
        "row.deleted_at IS NULL AND row.archived_at IS NULL AND row.consolidated_into IS NULL"
    )
    assert qualify_memory_predicate(MEMORY_ELIGIBILITY_PREDICATE, "") == MEMORY_ELIGIBILITY_PREDICATE


def test_compression_eligibility_rejects_private_consolidation_parent():
    selected = [
        row["id"]
        for row in ROWS
        if row["id"] in _canonical_ids() and row["permission_mode"] != 400 and row["namespace"] != "vault"
    ]

    assert selected == ["active"]
    predicate = eligible_for_compression("m", reject_private_parent=True)
    assert "(m.namespace IS NULL OR m.namespace <> 'vault')" in predicate
    assert predicate.endswith("AND m.permission_mode <> 400")


def test_federation_eligibility_rejects_archived_consolidated_private_and_vault_rows():
    selected = [
        row["id"]
        for row in ROWS
        if row["id"] in _canonical_ids() and (row["permission_mode"] % 10) >= 4 and row["namespace"] != "vault"
    ]

    assert selected == ["active"]
    predicate = eligible_for_federation("m", include_private=False)
    assert "m.archived_at IS NULL" in predicate
    assert "m.consolidated_into IS NULL" in predicate
    assert "(m.permission_mode % 10) >= 4" in predicate
    assert "(m.namespace IS NULL OR m.namespace <> 'vault')" in predicate


def test_federation_tombstone_eligibility_keeps_visibility_gates():
    predicate = eligible_for_federation_tombstone("m", include_private=False)

    assert "m.federation_source IS NULL" in predicate
    assert "(m.permission_mode % 10) >= 4" in predicate
    assert "m.deleted_at IS NULL" in predicate
    assert "m.archived_at IS NULL" in predicate
    assert "m.consolidated_into IS NOT NULL" in predicate
    assert "(m.namespace IS NULL OR m.namespace <> 'vault')" in predicate


def test_callsite_queries_reference_shared_predicates():
    from mnemos.api.routes.federation import _federation_visibility_filters
    from mnemos.domain.compression.contest_store import _FETCH_SOURCE_MAIN_HEAD_SQL
    from mnemos.domain.compression.worker_contest import _MEMORY_CONTENT_SQL

    assert _federation_visibility_filters() == [eligible_for_federation("m")]
    assert eligible_for_compression("", reject_private_parent=True) in _MEMORY_CONTENT_SQL
    assert eligible_for_compression("m", reject_private_parent=True) in _FETCH_SOURCE_MAIN_HEAD_SQL
