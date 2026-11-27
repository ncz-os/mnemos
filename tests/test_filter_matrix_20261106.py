"""The memory listing filter matrix: alone, in pairs/triples, with paging, and when bad.

``list_memories`` (mnemos/persistence/sqlite.py:1870, ``COUNT(*)`` before paging at
``sqlite.py:1915-1921``) builds one predicate from ``category``, ``subcategory``, ``tags`` (ANY),
``include_archived``, ``exclude_superseded``, ``metadata_eq`` and the visibility filter that
carries owner/namespace, then runs ``COUNT(*)`` over that predicate and a ``LIMIT/OFFSET`` page.
These tests exercise the real sqlite backend over a seeded corpus.

Pinned:

* each filter alone returns its set, and the documented pairs/triples compose (AND across keys, ANY
  within one tag list);
* ``total`` is the count before paging, so a page never reports itself as the total;
* an empty result is ``([], 0)`` — distinct from a malformed filter, which raises;
* an unknown filter name is a bare ``TypeError`` from the keyword-only signature. The brief's
  structured error for an unknown key is NOT implemented (neither here nor at the route, where
  FastAPI ignores unknown query params); that clause is a skipped sentinel.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from mnemos.persistence import SqliteBackend
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _bypass(namespace: str | None = None) -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=namespace,
    )


async def _seed(
    backend: SqliteBackend,
    memory_id: str,
    *,
    category: str,
    subcategory: str | None = None,
    tags: tuple[str, ...] = (),
    archived: bool = False,
    owner: str = "root",
    namespace: str = "default",
    metadata: dict | None = None,
    superseded_by: str | None = None,
) -> None:
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=f"{memory_id} body",
            category=category,
            subcategory=subcategory,
            metadata_json=json.dumps(metadata or {"phase": "matrix"}),
            quality_rating=75,
            owner_id=owner,
            namespace=namespace,
            permission_mode=700,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=f"{memory_id} body",
            created=None,
            updated=None,
        )
        if tags:
            await backend.memories.replace_memory_tags(tx, memory_id, list(tags))
        if archived:
            await tx.conn.execute("UPDATE memories SET archived_at = 1.0 WHERE id = ?", (memory_id,))
        if superseded_by is not None:
            await tx.conn.execute("UPDATE memories SET consolidated_into = ? WHERE id = ?", (superseded_by, memory_id))


async def _corpus(backend: SqliteBackend) -> None:
    await _seed(backend, "mem_a", category="facts", subcategory="alpha", tags=("red",))
    await _seed(backend, "mem_b", category="facts", subcategory="beta", tags=("red", "blue"), archived=True)
    await _seed(backend, "mem_c", category="solutions", subcategory="alpha", tags=("blue",))
    await _seed(backend, "mem_d", category="facts", subcategory="alpha")
    await _seed(backend, "mem_e", category="solutions", subcategory="beta", namespace="team-b")
    await _seed(
        backend,
        "mem_f",
        category="facts",
        subcategory="alpha",
        tags=("red",),
        metadata={"phase": "matrix", "tier": "gold"},
        superseded_by="mem_a",
    )


async def _ids(backend: SqliteBackend, **filters) -> tuple[list[str], int]:
    async with backend.transactional() as tx:
        rows, total = await backend.memories.list_memories(
            tx, visibility=_bypass(filters.pop("namespace", None)), **filters
        )
    return [row["id"] for row in rows], total


def _run(tmp_path, body):
    async def _go():
        backend = SqliteBackend(tmp_path / f"matrix-{uuid.uuid4().hex}.db", SimpleNamespace())
        await backend.open()
        try:
            await _corpus(backend)
            return await body(backend)
        finally:
            await backend.close()

    return asyncio.run(_go())


def test_category_alone(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, category="facts"))
    # mem_b is archived so the default listing hides it; mem_f is superseded
    # but still listed until exclude_superseded=True
    assert set(ids) == {"mem_a", "mem_d", "mem_f"}
    assert total == 3


def test_superseded_rows_are_shown_unless_excluded(tmp_path):
    shown, shown_total = _run(tmp_path, lambda b: _ids(b, category="facts"))
    assert "mem_f" in shown and shown_total == 3
    hidden, hidden_total = _run(tmp_path, lambda b: _ids(b, category="facts", exclude_superseded=True))
    assert "mem_f" not in hidden and hidden_total == 2
    # excluding superseded drops exactly mem_f from the archived-inclusive set
    # (mem_b is archived but not superseded, so it stays)
    all_live, all_total = _run(
        tmp_path,
        lambda b: _ids(b, category="facts", include_archived=True, exclude_superseded=True),
    )
    assert set(all_live) == {"mem_a", "mem_b", "mem_d"} and all_total == 3


def test_metadata_eq_alone(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, metadata_eq={"tier": "gold"}))
    assert ids == ["mem_f"] and total == 1
    none, none_total = _run(tmp_path, lambda b: _ids(b, metadata_eq={"tier": "nope"}))
    assert none == [] and none_total == 0


def test_owner_visibility_scopes_to_the_owner(tmp_path):
    async def body(backend):
        from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

        async with backend.transactional() as tx:
            rows, total = await backend.memories.list_memories(
                tx,
                visibility=VisibilityFilter(
                    scope=VisibilityScope.OWN_ONLY,
                    user_id="root",
                    group_ids=(),
                    namespace="default",
                ),
            )
        return [row["id"] for row in rows], total

    ids, total = _run(tmp_path, body)
    # mem_e lives in team-b and mem_b is archived-hidden; the rest are root-owned
    assert set(ids) == {"mem_a", "mem_c", "mem_d", "mem_f"}
    assert total == 4


def test_subcategory_alone(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, subcategory="alpha"))
    assert set(ids) == {"mem_a", "mem_c", "mem_d", "mem_f"}
    assert total == 4


def test_tags_use_any_semantics(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, tags=["blue"]))
    # mem_b carries blue too but is archived and therefore hidden by default
    assert ids == ["mem_c"] and total == 1


def test_archived_rows_are_hidden_unless_requested(tmp_path):
    hidden, hidden_total = _run(tmp_path, lambda b: _ids(b, category="facts"))
    assert "mem_b" not in hidden and hidden_total == 3
    shown, shown_total = _run(tmp_path, lambda b: _ids(b, category="facts", include_archived=True))
    assert "mem_b" in shown and shown_total == 4


def test_namespace_filter_isolates_tenants(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, namespace="team-b"))
    assert ids == ["mem_e"] and total == 1


def test_category_and_subcategory_compose(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, category="facts", subcategory="alpha"))
    assert set(ids) == {"mem_a", "mem_d", "mem_f"} and total == 3


def test_category_and_tags_compose(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, category="facts", tags=["red"]))
    # mem_b matches both keys but is archived and therefore hidden by default
    assert set(ids) == {"mem_a", "mem_f"} and total == 2


def test_category_subcategory_and_tags_compose(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, category="facts", subcategory="alpha", tags=["red"]))
    assert set(ids) == {"mem_a", "mem_f"} and total == 2
    # the same triple with a tag no row carries is empty, not an error
    none, none_total = _run(tmp_path, lambda b: _ids(b, category="facts", subcategory="alpha", tags=["green"]))
    assert none == [] and none_total == 0


def test_paging_composes_with_filters_and_total_is_pre_page(tmp_path):
    async def body(backend):
        first, total = await _ids(backend, category="facts", include_archived=True, limit=2, offset=0)
        second, total2 = await _ids(backend, category="facts", include_archived=True, limit=2, offset=2)
        return first, second, total, total2

    first, second, total, total2 = _run(tmp_path, body)
    assert total == 4 and total2 == 4, "total must not shrink to the page size"
    assert len(first) == 2 and len(second) == 2
    assert set(first).isdisjoint(second), "pages of one predicate must not overlap"


def test_an_empty_result_is_zero_rows_and_zero_total(tmp_path):
    ids, total = _run(tmp_path, lambda b: _ids(b, category="no-such-category"))
    assert ids == [] and total == 0


def test_a_malformed_filter_key_raises(tmp_path):
    with pytest.raises((ValueError, KeyError)):
        _run(tmp_path, lambda b: _ids(b, metadata_eq={"not a json path": "x"}))


def test_an_unknown_filter_key_is_a_bare_type_error(tmp_path):
    with pytest.raises(TypeError):
        _run(tmp_path, lambda b: _ids(b, include_deleted=True))


@pytest.mark.skip(
    reason=(
        "DIVERGENCE: an unknown filter key is a TypeError from the keyword-only repository "
        "signature (and is silently ignored by FastAPI's query parsing), not a structured error. "
        "Adding one is production code and this lane is tests-only."
    )
)
def test_an_unknown_filter_key_is_a_structured_error(tmp_path):
    with pytest.raises(Exception) as caught:
        _run(tmp_path, lambda b: _ids(b, include_deleted=True))
    assert "filter" in str(caught.value).lower()
